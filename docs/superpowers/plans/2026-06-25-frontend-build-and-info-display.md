# 前端构建生产化 + 仪表盘/登录页信息增强

**日期**：2026-06-25
**范围**：web 构建链路、仪表盘消息卡片、登录管理页账号显示
**当前分支**：master（实施时须 `git checkout -b feat/frontend-build-and-info-display`）

---

## 0. 关键调研结论（已通过源码确认）

> 写 plan 前对每个不确定项做了源码核对，结论锁定如下，后续任务基于此展开。

### 0.1 Tailwind 现状（任务 1）

- 3 处 CDN 引入：`web/templates/base.html:8`、`web/templates/login.html:7`、`web/templates/setup.html:7`
- 自定义配置（`base.html:9-34`，运行时 `tailwind.config = {...}`）：
  - `darkMode: 'media'`（与 `tokens.css:43` 的 `@media (prefers-color-scheme: dark)` 一致 → **保留 `'media'`**）
  - 自定义 colors：`apple.blue/blue-dark/green/green-dark/orange/orange-dark/red/red-dark`
  - 自定义 boxShadow：`stat/card/tooltip`（引用 CSS 变量）
- 全站用 `bg-[var(--card-bg)]` 等**任意值（arbitrary values）**——Tailwind JIT 必须支持（v3+ 都支持）。
- `web/static/tokens.css` 已挂在 `<link>`（`base.html:35`），含 fadeIn / blink 动画 + CSS 变量（light/dark）。
- FastAPI 静态目录已挂载：`web/app.py:212` `app.mount("/static", StaticFiles(directory=str(HERE / "static")), ...)`，`HERE / "static"` 即 `web/static/`。
- Docker base：`python:3.14-slim`（**注意：pyproject.toml 也是 `>=3.14`**，AGENTS.md 写 3.12 是过时文档；本 plan 按 3.14 走）。
- pyproject.toml 无 node/npm 依赖。

### 0.2 仪表盘现状（任务 2）

- 路由：`web/routes/dashboard.py`，`GET /` 渲染 `dashboard.html`。
- "最近消息" 表格在 `dashboard.html:82-116`，列：处理时间 / 发布时间 / 平台 / 标题 / 作者 / 阶段。
- 标题列已用 `truncate` + `title="..."`（原生 tooltip，仅显示 title 全文）。
- **已有 hover 卡片先例**：`dashboard.html:7-28` 的 `stat_tooltip` macro，用 `group-hover:block` 模式（stat 数字下方弹出消息列表）——可直接复用同款样式。
- **关键不确定项 - 正文/摘要数据源**：
  - `shared/protocols.py:276-295` 的 `MessageRecord` 字段：`msg_id / platform / content_type / phase / pubdate / title / author / created_at / updated_at / error / dynamic_text / subscription_ref`。
  - **没有 `body` 字段，没有 `summary` 字段。**
  - 实际 `data/messages.json` 持久化的字段（核对 `data/messages.json`）：仅上述基础字段，`dynamic_text` 多数条目甚至没有该 key。
  - `summary_text` 只存在于运行时 `PhaseContext`（`protocols.py:298-311`），push 后即丢弃，**未回写 MessageStore**。
  - 结论：任务 2 的"完整正文 + AI 摘要"在 MessageStore 中**当前不可得**。本 plan 任务 2 范围**调整为：hover 卡片展示 MessageRecord 现有所有字段**（title 全文 + author + phase + error + pubdate + dynamic_text 如有）；正文/摘要作为后续独立任务（见 §6）。

### 0.3 登录页现状（任务 3）

- 路由：`web/routes/auth.py`，`GET /auth` 渲染 `platform_auth.html`，HTMX 局部刷新走 `GET /auth/card/{key}` 渲染 `_auth_card.html`。
- `_get_auth_status()`（auth.py:44-59）只返回 `(status_text, expires_text, has_auth)`，**没有 nickname**。
- 平台 → nickname 获取路径：
  | 平台 | 现有方法 | 改动 |
  |------|---------|------|
  | xhs | `XhsClient.get_user_info()` 返回 `{"nickname": ...}`（`platforms/xiaohongshu/client.py:310-317`） | 0 改动，直接调 |
  | bilibili | `validate_tokens` 内调 `Credential.check_valid()`（只 bool）；`bilibili-api-python` 的 `User(uid).get_user_info()` 返回含 `name` 字段 | 需新增 helper：从 cookie `dedeuserid` 取 uid，构造 `User` 拉信息 |
  | weibo | `validate_tokens` 用 KEEPALIVE_URL 只判 200 | 需新增 helper：调 `/api/config` 或 `/my` 端点取 `data.user.screen_name` |
- **性能**：3 个平台每次访问 `/auth` 都 sync probe API 慢且可能阻塞。**采用：内存缓存 + TTL（默认 10 分钟）+ 失败降级**（拉不到就显示"—"，不影响 status 显示）。缓存放在 `web/routes/auth.py` 模块级，类似已有 `_auth_instances`。

---

## 1. 任务 1（P3）：替换 CDN Tailwind 为生产构建

### 1.1 方案对比

| 维度 | (a) node + tailwindcss npm + Dockerfile 多阶段 | (b) Tailwind CLI 独立二进制 | (c) 预编译 main.css 提交进仓库 |
|------|---|---|---|
| 镜像体积增量 | +~200MB（node + npm 缓存层，多阶段可清） | 0（CLI 二进制仅构建期用，不进运行镜像） | 0 |
| 构建期依赖 | node + npm + internet 拉 npm 包 | 拉 1 个 ~40MB 二进制（GitHub release） | 无 |
| Dockerfile 改动 | 大：新增 builder stage、`package.json`、`npm ci && npm run build` | 小：builder stage + `curl` 下载二进制 + `./tailwindcss -i ... -o ...` | 0 |
| 仓库改动 | 新增 `package.json`、`tailwind.config.js`、`web/src/input.css` | 新增 `tailwind.config.js`、`web/src/input.css` | 新增 `web/static/css/main.css`（数十 KB） |
| CI/本地开发 | 开发者需装 node | 开发者需下载二进制（脚本封装） | 0 依赖，改 class 后须手动重跑构建脚本 |
| class 漏 purge 风险 | 同等（都靠 content scanning） | 同等 | 同等 |
| 与项目"零 node"约定契合度 | 差（项目无任何 JS 工具链） | 好 | 最好 |
| 长期可维护性 | 标准 npm 生态，升级方便 | 二进制版本手动管理 | 改 class 必须记得重编译，易漏 |

**推荐：方案 (b) Tailwind CLI 独立二进制**

**理由**：
1. 项目是 Python 单语言仓库，pyproject.toml + uv 管 Python 依赖，无任何 JS 工具链。引入 node + npm + package.json + package-lock.json 是显著复杂度升级，与项目风格冲突。
2. Tailwind 官方提供 standalone CLI（`tailwindcss-linux-x64`），单二进制免运行时依赖，构建期用完即弃，运行镜像零增量。
3. 相比方案 (c) 预编译提交：CLI 方案保留"源 → 编译"链路，class 变更后 CI 能自动重生成；预编译方案靠人工记忆，长期会漂移。
4. 方案 (b) 唯一成本是 Dockerfile 加一个 builder stage + 一行 curl，与方案 (a) 的 npm 工具链相比微不足道。

**降级方案（备选）**：若 Tailwind CLI 二进制下载在 CI 受限，退回方案 (c)——把构建脚本 `scripts/build-css.sh` 留下，开发者本地跑、提交产物。本 plan 不预先实现降级路径，按方案 (b) 走。

### 1.2 文件清单

| 路径 | 操作 | 估计行数 |
|------|------|---------|
| `tailwind.config.js`（新增，仓库根） | 新增 | ~30 行 |
| `web/src/input.css`（新增） | 新增 | ~10 行（`@tailwind base/components/utilities` + `tokens.css` import 或合并说明） |
| `web/static/css/main.css`（构建产物，新增） | gitignore 但本地开发可生成 | ~数 KB（构建产物） |
| `web/templates/base.html` | 改：删 CDN `<script>` + 内联 config，加 `<link rel="stylesheet" href="/static/css/main.css">`；保留 tokens.css | 净 -28 / +2 行 |
| `web/templates/login.html` | 改：删 CDN `<script>`，继承 base 的 link | -1 行 |
| `web/templates/setup.html` | 改：删 CDN `<script>`，继承 base 的 link | -1 行 |
| `Dockerfile` | 改：加 builder stage 编译 CSS，COPY 到运行镜像 | +15 行 |
| `.gitignore` | 改：忽略 `web/static/css/main.css`（构建产物） | +1 行 |
| `.dockerignore` | 检查：确保 `web/src/` 不被排除（builder 需要） | 视现状而定 |
| `scripts/build-css.sh`（新增，开发者本地构建脚本） | 新增 | ~15 行 |

### 1.3 tailwind.config.js（content scanning 配置）

```js
// tailwind.config.js
module.exports = {
  // 与 tokens.css @media (prefers-color-scheme: dark) 一致
  darkMode: 'media',
  content: [
    // 全部 HTML 模板（含 macros / partials / 未来新增的平台模板）
    './web/templates/**/*.html',
    // 防御性扫描 Python 路由里硬编码的 HTML 片段（如果有）
    './web/routes/**/*.py',
    // JS 内联在模板里的 class（base.html 的 toast colors 等）已被 templates 扫描覆盖
  ],
  theme: {
    extend: {
      colors: {
        apple: {
          blue: '#0071e3',
          'blue-dark': '#0a84ff',
          green: '#34c759',
          'green-dark': '#30d158',
          orange: '#ff9500',
          'orange-dark': '#ff9f0a',
          red: '#ff3b30',
          'red-dark': '#ff453a',
        },
      },
      boxShadow: {
        stat: 'var(--shadow-stat)',
        card: 'var(--shadow-card)',
        tooltip: 'var(--shadow-tooltip)',
      },
    },
  },
};
```

**content 路径覆盖说明**（防 purge 漏 class）：
- 任务 2/3 新增的 hover 卡片、nickname 显示所用 class **必须**在 `web/templates/**/*.html` 内，已被 content glob 覆盖。
- 不要在 Python 路由里 `HTMLResponse("<div class='...'>")` 拼字符串带 class——如必须，记得加 `./web/routes/**/*.py`（已含）。
- 模板里动态拼接的 class（如 `dashboard.html` 的 `bg-{color}-500`）须确保**完整字符串字面量**出现在扫描范围内。`_macros.html:10-15` 的 `color_map` 已经是字面量，安全。

### 1.4 web/src/input.css

```css
/* web/src/input.css — Tailwind 入口 */
@tailwind base;
@tailwind components;
@tailwind utilities;

/* tokens.css 内容合并进这里（CSS 变量 + keyframes）
 * 原因：单一产物，避免运行时多一个 <link>。
 * 把 web/static/tokens.css 的内容原样粘贴在下方。 */
:root { /* ...原 tokens.css :root 块... */ }
@media (prefers-color-scheme: dark) { /* ... */ }
@keyframes fadeIn { /* ... */ }
@keyframes blink { /* ... */ }
.animate-fadeIn { animation: fadeIn 0.3s ease-out; }
.animate-blink { animation: blink 1s steps(2) infinite; }
```

实施步骤：把 `web/static/tokens.css` 内容**剪切**到 `web/src/input.css` 末尾，让 main.css 成为唯一产物。`base.html` 删除 `<link rel="stylesheet" href="/static/tokens.css">`。

### 1.5 Dockerfile 改动（builder stage）

在现有 `FROM python:${PYTHON_VERSION}-slim AS base`（Dockerfile:9）**之前**插入 builder：

```dockerfile
# ── CSS builder：用 Tailwind standalone CLI 编译，免 node ──
FROM alpine:3.20 AS css-builder
WORKDIR /build
# Tailwind CLI standalone（Linux x86_64；版本与 project 期望一致）
ARG TAILWIND_VERSION=3.4.13
RUN apk add --no-cache curl \
    && curl -fsSL -o /usr/local/bin/tailwindcss \
       "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss
COPY tailwind.config.js .
COPY web/src/input.css web/src/input.css
COPY web/templates/ web/templates/
RUN /usr/local/bin/tailwindcss \
    --input web/src/input.css \
    --output main.css \
    --minify
```

在现有运行镜像 `COPY . .`（Dockerfile:41）之后追加：

```dockerfile
# ── 拷入构建期生成的 CSS（覆盖任何残留）──
COPY --from=css-builder /build/main.css web/static/css/main.css
```

**架构注意**：`web/static/css/` 目录在仓库中不存在（仅有 `web/static/tokens.css`）。Dockerfile 的 `COPY --from` 会自动创建目录。本地开发用 `scripts/build-css.sh` 生成。

### 1.6 base.html 改动（diff 示意）

```diff
 <head>
   <meta charset="UTF-8">
   <meta name="viewport" content="width=device-width, initial-scale=1.0">
   <title>{% block title %}Trawler{% endblock %}</title>
   <script src="https://unpkg.com/htmx.org@2.0.4"></script>
-  <script src="https://cdn.tailwindcss.com"></script>
-  <script>
-    tailwind.config = {
-      darkMode: 'media',
-      theme: {
-        extend: {
-          colors: { apple: { ... } },
-          boxShadow: { stat: '...', card: '...', tooltip: '...' }
-        }
-      }
-    }
-  </script>
-  <link rel="stylesheet" href="/static/tokens.css">
+  <link rel="stylesheet" href="/static/css/main.css">
 </head>
```

### 1.7 login.html / setup.html 改动

这俩模板各自有 `<script src="https://cdn.tailwindcss.com"></script>`（**它们是独立的非 extends 模板**，自己有完整 `<head>`）。检查它们是否 `{% extends %}` base——若不是独立完整 HTML，需各自加 `<link rel="stylesheet" href="/static/css/main.css">` 并删 CDN script。

实施时第一步先 `grep -n "extends" web/templates/login.html web/templates/setup.html` 确认结构。

### 1.8 scripts/build-css.sh

```bash
#!/usr/bin/env bash
# 本地构建 Tailwind CSS（与 Dockerfile builder 等价）
# 用法：./scripts/build-css.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

VERSION=3.4.13
BIN="$HOME/.cache/tailwindcss-linux-x64"
mkdir -p "$(dirname "$BIN")"
if [ ! -x "$BIN" ]; then
  curl -fsSL -o "$BIN" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v${VERSION}/tailwindcss-linux-x64"
  chmod +x "$BIN"
fi

"$BIN" --input web/src/input.css --output web/static/css/main.css --minify
echo "✓ built web/static/css/main.css"
```

### 1.9 测试策略（TDD）

构建产物本身是 CSS 文本，难以单元测试。**测试聚焦"模板不再引用 CDN"**：

新增 `tests/test_web_tailwind_build.py`：
```python
class TestNoCdnTailwind:
    def test_base_html_has_no_cdn_script(self):
        text = Path("web/templates/base.html").read_text()
        assert "cdn.tailwindcss.com" not in text

    def test_login_html_has_no_cdn_script(self):
        text = Path("web/templates/login.html").read_text()
        assert "cdn.tailwindcss.com" not in text

    def test_setup_html_has_no_cdn_script(self):
        text = Path("web/templates/setup.html").read_text()
        assert "cdn.tailwindcss.com" not in text

    def test_base_html_links_compiled_css(self):
        text = Path("web/templates/base.html").read_text()
        assert '/static/css/main.css' in text
```

Docker 构建验证（手动）：
```bash
docker build -t trawler:css-test .
docker run --rm trawler:css-test cat /app/web/static/css/main.css | head -5
# 期望：压缩后的 CSS 内容，含 .bg-\[var\(--card-bg\)\] 之类
```

### 1.10 验证步骤

```bash
uv run pytest tests/test_web_tailwind_build.py -v
uv run ruff check .
uv run pyright .
docker build -t trawler:css-test .
# 浏览器手动：起服务，F12 控制台不应再有 "cdn.tailwindcss.com should not be used in production"
# 检查暗色模式切换正常（OS 级 dark/light）
# 检查 mobile sidebar / toast / stat tooltip 视觉与改造前一致
```

---

## 2. 任务 2（P5）：仪表盘最近消息 hover 卡片

### 2.1 范围（受 §0.2 约束调整后）

**做**：hover 行时弹出卡片，显示该 MessageRecord 的**所有现有字段**：
- title 全文（不截断）
- author / platform / phase（badge）/ content_type
- pubdate（绝对时间，不只 timeago）
- updated_at（处理时间）
- error（如有，红色显示）
- dynamic_text（如有，独立段落标注"动态附加内容"）

**不做**（数据源缺失，留作后续任务，见 §6）：
- 完整正文（body 不在 MessageRecord）
- AI 摘要（summary 未回写 store）

### 2.2 文件清单

| 路径 | 操作 | 估计行数 |
|------|------|---------|
| `web/templates/dashboard.html` | 改：复用现有 `stat_tooltip` 模式，给表格 `<tr>` 加 `group relative` + hover popover | +30 行 |

### 2.3 实现选择

| 方案 | 评估 |
|------|------|
| 纯 CSS `title` 属性 | 已用，但只能纯文本、样式不可控、长内容截断 → **不够** |
| 自定义 hover popover（group-hover 模式） | **推荐**——`dashboard.html:7-28` 的 `stat_tooltip` 已是同款，复用样式，零新依赖 |
| 第三方 lib（tippy.js 等） | 不推荐——增加 JS 依赖、构建复杂度，与项目"零 JS 框架"风格冲突 |

### 2.4 改动点：dashboard.html 表格行

**位置**：`dashboard.html:100-108`（`{% for msg in recent_messages %}` 内的 `<tr>`）

**改前**：
```html
<tr class="border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/30 transition-colors">
  <td class="px-5 py-3 ...">{{ msg.updated_at | timeago }}</td>
  ...
  <td class="px-5 py-3 max-w-xs truncate" title="{{ msg.title }}">{{ msg.title }}</td>
  ...
</tr>
```

**改后**（新增 macro + 行内 popover）：

在文件顶部已有 `stat_tooltip` macro 旁，新增 macro：

```jinja
{% macro msg_detail_card(msg) %}
<div class="absolute left-0 top-full hidden group-hover:block z-50 w-96 pt-2">
  <div class="bg-[var(--card-bg)] backdrop-blur-[12px] border border-[var(--card-border)] rounded-[10px] shadow-tooltip p-3 max-h-80 overflow-y-auto">
    {# 标题全文 #}
    <div class="text-sm font-medium text-[var(--text-primary)] mb-2 break-words">{{ msg.title }}</div>
    {# 元信息行 #}
    <div class="text-xs text-[var(--text-secondary)] flex flex-wrap gap-x-3 gap-y-1 mb-2">
      <span>作者: <span class="font-medium text-[var(--text-primary)]">{{ msg.author }}</span></span>
      <span>平台: <span class="font-mono">{{ msg.platform }}</span></span>
      <span>类型: <span class="font-mono">{{ msg.content_type.name }}</span></span>
    </div>
    {# 时间信息 #}
    <div class="text-xs text-[var(--text-tertiary)] mb-2">
      发布 {{ msg.pubdate | timeago }} · 更新 {{ msg.updated_at | timeago }}
    </div>
    {# 错误（如有）#}
    {% if msg.error %}
    <div class="text-xs text-red-500 bg-red-50 dark:bg-red-900/20 rounded-[6px] px-2 py-1 mb-2 break-words">
      ⚠️ {{ msg.error }}
    </div>
    {% endif %}
    {# 动态附加文字（如有）#}
    {% if msg.dynamic_text %}
    <div class="text-xs text-[var(--text-secondary)] mt-2 pt-2 border-t border-[var(--card-border)]">
      <div class="font-medium mb-1">动态附加:</div>
      <div class="break-words whitespace-pre-wrap">{{ msg.dynamic_text }}</div>
    </div>
    {% endif %}
  </div>
</div>
{% endmacro %}
```

`<tr>` 改造：

```diff
-<tr class="border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/30 transition-colors">
+<tr class="group relative border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/30 transition-colors">
   <td class="px-5 py-3 ...">{{ msg.updated_at | timeago }}</td>
   ...
-  <td class="px-5 py-3 max-w-xs truncate" title="{{ msg.title }}">{{ msg.title }}</td>
+  <td class="px-5 py-3 max-w-xs truncate">{{ msg.title }}</td>
   ...
+  <td class="px-5 py-3">
+    {{ badge(msg.phase.name, msg.phase | phase_color) }}
+    {{ msg_detail_card(msg) }}
+  </td>
 </tr>
```

**注意**：popover 放在最后一列内（`relative` 的 `<tr>` 内 `<td>` 持有 `group-hover:block` 的绝对定位卡片）。`<tr class="group relative">` + `<td>` 不需 relative，因绝对定位会相对最近的 `position: relative` 祖先——`<tr>` 本身。验证时确认浏览器渲染。

### 2.5 移动端适配

`group-hover` 在 touch 设备不可用。增加 click toggle：

`<tr>` 加 `onclick="this.classList.toggle('group-force-show')"`，CSS 加：

```css
/* 在 input.css 末尾追加（与 tokens.css 合并一起编译） */
@media (hover: none) {
  .group-force-show .group-hover\:block { display: block !important; }
}
```

实施时确认 Tailwind 能识别 `.group-hover\:block` 这种转义选择器——`@media (hover: none)` 内手写 CSS 即可，不依赖 Tailwind 生成。

### 2.6 测试策略（TDD）

新增 `tests/test_web_dashboard.py` 内追加用例：

```python
class TestDashboardMessageHover:
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_recent_msg_full_title(self, mock_load, mock_list, client):
        # 构造一条 title 很长的 MessageRecord，断言完整 title 出现在 HTML
        ...

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_error_in_hover_card(self, mock_load, mock_list, client):
        # msg.error 非空 → HTML 含 msg.error 字符串
        ...

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_dynamic_text_when_present(self, mock_load, mock_list, client):
        # msg.dynamic_text 非空 → HTML 含 "动态附加"
        ...
```

参考 `tests/test_web_dashboard.py:31-40` 现有 fixture 模式。

### 2.7 验证步骤

```bash
uv run pytest tests/test_web_dashboard.py -v
uv run ruff check .
uv run pyright .
# 浏览器手动：起服务，hover 最近消息行 → 卡片弹出，含完整 title/error
# 移动端模拟（Chrome DevTools touch mode）：tap 行 → 卡片切换显示
```

---

## 3. 任务 3（P6）：登录管理页显示账号名称

### 3.1 文件清单

| 路径 | 操作 | 估计行数 |
|------|------|---------|
| `web/routes/auth.py` | 改：新增 `_fetch_nicknames()` + 内存缓存 + TTL；扩展现有 `_get_auth_status` 与 `auth_page` / `auth_card` 数据 | +60 行 |
| `platforms/bilibili/auth.py` | 改：新增 `async def get_user_nickname(tokens) -> str \| None`（独立方法，不污染 `validate_tokens`） | +25 行 |
| `platforms/weibo/auth.py` | 改：新增 `async def get_user_nickname(tokens) -> str \| None` | +20 行 |
| `platforms/xiaohongshu/auth.py` | 改：新增 `async def get_user_nickname(tokens) -> str \| None`（包装现有 `XhsClient.get_user_info()`） | +15 行 |
| `shared/auth/base.py` | 改：`BaseAuthenticator` 加 optional 方法 `async def get_user_nickname(self, tokens) -> str \| None`（默认返回 None） | +5 行 |
| `web/templates/_auth_card.html` | 改：status 行下新增 nickname 显示（仅当 nickname 非空） | +5 行 |

### 3.2 shared/auth/base.py 改动

在 `BaseAuthenticator`（base.py:59-88）加方法：

```python
async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
    """Return the logged-in user's display name, or None if unavailable.

    Default implementation returns None; subclasses override to probe
    platform-specific user-info APIs. Used by web UI to show account names.

    MUST NOT raise — failures return None so the auth page still renders.
    """
    return None
```

### 3.3 三平台 authenticator 实现

**Bilibili**（`platforms/bilibili/auth.py` 末尾追加）：

```python
async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
    """Fetch nickname via User.get_user_info().

    Requires `dedeuserid` cookie (uid). Returns None on any failure
    (no uid / API error / network) — never raises.
    """
    import bilibili_api

    uid_str = tokens.cookies.get("dedeuserid", "")
    if not uid_str:
        return None
    try:
        uid = int(uid_str)
    except ValueError:
        return None
    cred = bilibili_api.Credential(
        sessdata=tokens.cookies.get("sessdata", ""),
        bili_jct=tokens.cookies.get("bili_jct", ""),
    )
    try:
        info = await bilibili_api.user.User(uid=uid, credential=cred).get_user_info()
        # API returns name field per SocialSisterYi/bilibili-API-collect docs
        name = info.get("name") if isinstance(info, dict) else None
        return name or None
    except Exception as e:
        logger.warning("B站 nickname 获取失败: %s", e)
        return None
```

**XHS**（`platforms/xiaohongshu/auth.py` 末尾追加）：

```python
async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
    """Fetch nickname via XhsClient.get_user_info().

    Reuses internal _client (lazy-created with token cookies).
    """
    try:
        client = await self._ensure_client(build_cookie_str(tokens.cookies))
        info = await client.get_user_info()
        nick = info.get("nickname") if isinstance(info, dict) else None
        return nick or None
    except Exception as e:
        logger.warning("XHS nickname 获取失败: %s", e)
        return None
```

**Weibo**（`platforms/weibo/auth.py` 末尾追加）：

```python
# 模块顶部已有 KEEPALIVE_URL；新增 nickname 端点
WEIBO_USER_INFO_URL = "https://m.weibo.cn/api/config"

async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
    """Fetch nickname from m.weibo.cn /api/config.

    Returns data.data.loginProfile.screen_name (or similar). Falls back
    to None on any failure.
    """
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            resp = await session.get(
                WEIBO_USER_INFO_URL,
                headers={"User-Agent": _get_user_agent(), "Cookie": cookie_str},
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            )
            try:
                if resp.status != 200:
                    return None
                data = await resp.json()
                # m.weibo.cn /api/config → {"data": {"loginStatus": true, "loginProfile": {"screen_name": ...}}}
                profile = data.get("data", {}).get("loginProfile", {}) if isinstance(data, dict) else {}
                nick = profile.get("screen_name") if isinstance(profile, dict) else None
                return nick or None
            finally:
                resp.close()
        except Exception as e:
            logger.warning("微博 nickname 获取失败: %s", e)
            return None
```

**实施前实测确认**：微博 `/api/config` 端点字段路径可能因账号/版本不同。实施时第一步用真实 cookie 跑一次 curl 确认 JSON 结构，按实际字段调整 `profile.get(...)` 路径。**这是本任务最大不确定项**。

### 3.4 web/routes/auth.py 改动

在文件顶部 import 区附近（auth.py:1-17 之后）加：

```python
# Nickname 缓存：避免每次访问 /auth 都 probe 3 个平台 API
# key: platform_key, value: (nickname: str | None, fetched_at: float)
# TTL 10 分钟——nickname 几乎不变，长 TTL 可接受
_NICKNAME_TTL_SECONDS = 600
_nickname_cache: dict[str, tuple[str | None, float]] = {}


async def _fetch_nickname(config: Config, platform_key: str) -> str | None:
    """Get cached nickname or probe platform API.

    Returns None if: platform not logged in / API failed / cached None within TTL.
    Never raises — caller treats None as "display —".
    """
    # Check cache (including None results — don't re-probe known failures)
    cached = _nickname_cache.get(platform_key)
    if cached is not None and (time.time() - cached[1]) < _NICKNAME_TTL_SECONDS:
        return cached[0]

    # Build tokens from config; None → not configured, cache None
    tokens = _build_tokens_from_config(platform_key, config)
    if tokens is None:
        _nickname_cache[platform_key] = (None, time.time())
        return None

    # Probe via authenticator
    auth = get_authenticator(platform_key)
    try:
        try:
            nick = await auth.get_user_nickname(tokens)
        except Exception as exc:
            logger.warning("🔑 %s nickname 获取异常: %s", platform_key, exc)
            nick = None
        _nickname_cache[platform_key] = (nick, time.time())
        return nick
    finally:
        # bili/weibo authenticators are stateless — safe to close every time.
        # xhs holds state but get_user_nickname reuses _client via _ensure_client;
        # closing here forces re-init next time, acceptable given 10min TTL.
        try:
            await auth.close()
        except Exception as exc:
            logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)
```

修改 `auth_page`（auth.py:62-74）：

```python
@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request) -> HTMLResponse:
    config = await load_config()
    platforms: list[dict[str, Any]] = []
    for p in PLATFORM_INFO:
        status, expires, has_auth = _get_auth_status(config, p["key"])
        nickname = await _fetch_nickname(config, p["key"]) if has_auth else None
        platforms.append({
            **p,
            "token_status": status,
            "expires": expires,
            "has_auth": has_auth,
            "nickname": nickname,
        })
    return TEMPLATES.TemplateResponse(
        request,
        "platform_auth.html",
        {"active_nav": "auth", "platforms": platforms},
    )
```

同样修改 `auth_card`（auth.py:77-90）的 `p` dict。

**登出后清理缓存**（auth_logout，auth.py:93-112）：

```python
@router.post("/auth/logout/{platform_key}")
async def auth_logout(platform_key: str) -> dict[str, Any]:
    ...
    _qr_sessions.pop(platform_key, None)
    _auth_instances.pop(platform_key, None)
    _nickname_cache.pop(platform_key, None)  # NEW
    ...
```

### 3.5 _auth_card.html 改动

在 status 行（`_auth_card.html:17`）下追加：

```diff
-<div class="text-sm text-[var(--text-primary)] mb-2 font-medium">{{ p.token_status }}</div>
+<div class="text-sm text-[var(--text-primary)] mb-2 font-medium">{{ p.token_status }}</div>
+{% if p.nickname %}
+<div class="text-xs text-[var(--text-secondary)] mb-1">账号: <span class="font-medium text-[var(--text-primary)]">{{ p.nickname }}</span></div>
+{% endif %}
```

**未登录平台的展示不受影响**：`has_auth=False` 时 `nickname=None`，`{% if p.nickname %}` 不渲染该行。

### 3.6 测试策略（TDD）

**先写测试**：

`tests/test_web_auth.py` 追加：

```python
class TestAuthNickname:
    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_shows_nickname_when_logged_in(
        self, mock_load, mock_get_auth, client
    ):
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.bilibili.auth.sessdata = "x"
        mock_load.return_value.bilibili.auth.bili_jct = "y"
        mock_load.return_value.bilibili.auth.dedeuserid = "12345"
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="测试UP主")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth
        # 清缓存（测试隔离）
        from web.routes.auth import _nickname_cache
        _nickname_cache.clear()

        resp = await client.get("/auth")
        assert resp.status_code == 200
        assert "测试UP主" in resp.text

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_hides_nickname_row_when_not_logged_in(
        self, mock_load, mock_get_auth, client
    ):
        # 全部平台 expires_at=0 → nickname 全 None → 不出现 "账号:" 文案
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        ...
        resp = await client.get("/auth")
        assert "账号:" not in resp.text

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_nickname_failure_falls_back_gracefully(
        self, mock_load, mock_get_auth, client
    ):
        # get_user_nickname 抛异常 → 不应 500，应显示 status 但无 nickname 行
        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(side_effect=RuntimeError("boom"))
        mock_auth.close = AsyncMock()
        ...
        resp = await client.get("/auth")
        assert resp.status_code == 200

    async def test_auth_logout_clears_nickname_cache(self, ...):
        # 登出后 _nickname_cache 不应再持有该平台 key
        ...
```

平台 authenticator 测试：

`tests/test_bilibili_authenticator.py`、`tests/test_xhs_authenticator.py`、`tests/test_weibo_authenticator.py` 各加一个 `test_get_user_nickname_*` 用例（参考现有 mock 模式）。

### 3.7 验证步骤

```bash
uv run pytest tests/test_web_auth.py tests/test_bilibili_authenticator.py tests/test_xhs_authenticator.py tests/test_weibo_authenticator.py -v
uv run ruff check .
uv run pyright .
# 浏览器手动：登录某平台 → /auth 看到账号名；未登录平台不显示该行
# 二次访问 /auth → 响应时间显著下降（缓存命中）
```

---

## 4. 任务依赖与执行顺序

```
任务 1（CSS 构建链路）  ← 必须先做
   │
   ├─ 任务 2（dashboard hover 卡片新增 class）
   │     └─ 这些 class 在 web/templates/dashboard.html，
   │        已被任务 1 的 tailwind.config.js content glob 覆盖 → purge 不漏
   │
   └─ 任务 3（auth card 新增 class）
         └─ 这些 class 在 web/templates/_auth_card.html，
            同样被覆盖 → purge 不漏
```

**关键约束**：任务 1 改完后，**必须重新跑 Tailwind 构建**（`scripts/build-css.sh` 或 docker build），否则任务 2/3 新增的 class 不在 main.css 里。CI 流程里 docker build 会自动重编，但本地开发要注意。

执行顺序：
1. **任务 1**（含测试）→ 提交一次
2. **任务 3** 先于任务 2（任务 3 涉及后端，更复杂，先做掉；任务 2 纯模板）
3. **任务 2**（纯模板，最简单）

或拆 3 个 PR 也可。本 plan 倾向合并为 1 个 PR（都是"前端信息展示增强"主题，见 AGENTS.md PR 粒度规则）。

---

## 5. 风险与不确定项

| # | 不确定项 | 影响 | 应对 |
|---|---------|------|------|
| R1 | 微博 `/api/config` 字段路径（`data.loginProfile.screen_name`）未经源码/实测确认 | 任务 3 微博 nickname 可能拉不到 | 实施第一步用真实 cookie curl 验证；fallback：拉不到显示"—"，不阻塞 |
| R2 | Bilibili `User.get_user_info()` 需要 buvid3 cookie 否则可能被风控 | 任务 3 B站 nickname 可能失败 | `validate_tokens` 已用 sessdata+bili_jct 两件套工作；get_user_info 同样 cred 即可；失败降级 None |
| XHS authenticator `close()` 会销毁内部 _client，频繁 probe 是否有副作用 | 任务 3 性能/资源 | `_nickname_cache` TTL 10 分钟，10 分钟内最多 probe 1 次；可接受。若 xhs 客户端重建成本高，可考虑 bili/weibo 用 stateless auth、xhs 用单例（本 plan 不预先实现，观察） |
| R4 | Tailwind CLI standalone 二进制在 ARM Mac 上需要不同版本（`tailwindcss-macos-arm64`） | 任务 1 开发者本地构建 | `scripts/build-css.sh` 检测 `uname -m` + `uname -s` 选对应二进制；Dockerfile 用 linux-x64 即可（容器内） |
| R5 | Tailwind v3 vs v4：v4 配置语法不同（无 `tailwind.config.js`，用 CSS `@theme`） | 任务 1 版本选择 | 本 plan 锁 v3.4.13（成熟、配置语法与现有 `tailwind.config = {...}` 一致）；v4 待生态稳定再迁移 |
| R6 | `dashboard.html` 把 `<tr>` 设为 `position: relative` 后，`<td>` 内 `group-hover:block` 的绝对定位卡片相对 `<tr>` 还是 `<td>`？ | 任务 2 视觉错位 | 实施时浏览器实测；若相对 `<td>` 错位，把 `relative` 移到 `<td>` 或卡片包一层 `<div class="relative">` |
| R7 | `pyproject.toml` 写 `requires-python = ">=3.14"`，Dockerfile `ARG PYTHON_VERSION=3.14`，AGENTS.md 写 3.12——三处冲突 | 文档漂移 | 本 plan 按 3.14（代码现状权威）；AGENTS.md 文档滞后属另一问题，不在本 plan 范围 |
| R8 | 任务 2 范围被 §0.2 调整（不展示正文/摘要） | 用户期望落差 | 在 PR 描述中明确说明"正文/摘要需 MessageRecord schema 扩展，留作后续任务"；见 §6 |
| R9 | login.html / setup.html 是否 extends base.html？ | 任务 1 改动范围 | 实施第一步 `grep -n "extends" login.html setup.html` 确认；若是独立完整 HTML，各自加 link；若是 extends，删 CDN script 即可（继承 base 的 link） |

---

## 6. 后续任务（不在本 plan 范围）

任务 2 用户原始诉求包含"完整正文 + AI 摘要"，但 MessageRecord 当前不持有这两类数据。完整实现需要：

1. **扩展 MessageRecord schema**（`shared/protocols.py:276-295`）：
   - 加 `body: str = ""`（正文，下载/解析阶段回写）
   - 加 `summary: str = ""`（AI 摘要，summarize 阶段回写）
2. **MessageStore 持久化**：`message_store.py:_msg_from_dict` 反序列化新字段；`add_new` 不变（默认空）；新增 `mark_body` / `mark_summary` 写入接口
3. **回写点**：
   - bilibili/xhs/weibo 各 handler 的 download 阶段回写 body
   - summarizer 阶段回写 summary（当前 `PhaseContext.summary_text` 没落盘）
4. **dashboard hover 卡片**：扩展 `msg_detail_card` macro 展示 body / summary 段落
5. **数据迁移**：现有 `messages.json` 旧条目没有这俩字段，反序列化时默认空——向后兼容

这是一个独立的中等规模任务（涉及 protocols/store/handlers/templates 四层），建议另起 plan。本 plan 任务 2 仅做"在现有数据上做最大化的 hover 展示"。

---

## 用户决策（2026-06-25 拍板）

**任务 2（悬浮卡）改为"扩展 MessageRecord 字段"独立任务**：
- 用户反馈："hover 显示已有信息没必要，表格已给出。可以评估加字段难度。"
- 当前 MessageRecord 无 body/summary 字段，hover 卡只能复述 title/author/phase → 无价值
- **结论**：任务 2 从本 plan 拆出，改为"扩展 MessageRecord + summarizer 回写 + dashboard 展示"的独立 plan（涉及 protocols/store/handlers/templates 四层，中等规模）
- 本 plan 任务 2 的占位实现作废，等独立 plan 排期

**任务 1（tailwind 生产构建）+ 任务 3（账号名）保留，继续按本 plan 执行**。
