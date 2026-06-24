# Plan: 修复端点/订阅端点保存白屏 + 订阅端点 UI 美化

**日期**: 2026-06-25
**范围**: `web/routes/endpoints.py`、`web/routes/subscriptions.py`、`web/templates/base.html`、`web/templates/subscriptions.html`、`web/templates/endpoints.html`、`tests/test_web_toast_headers.py`
**作者**: @explorer (writing-plans 委托)
**状态**: draft（待 @oracle review）

---

## 1. 背景与根因（已调查，本 plan 不重新调研）

### 1.1 Bug A — 白屏

5 个路由（endpoints 3 个 + subscriptions endpoint add/remove 2 个）当前用
`HTMLResponse(content="", headers={"HX-Trigger": ...})` 模式返回空 body + toast
触发头。这些表单都标了 `hx-target="body"`。

HTMX 行为：POST 200 + 空 body + `hx-target="body"` → 整个 `<body>` 被替换为空
字符串 → **整页白屏**。toast 即使正确显示，用户也已经看不到任何 UI 了。

参考实现：`subscriptions_add` / `subscriptions_remove`（subscriptions.py:55-77）
已经用 `RedirectResponse(url="/subscriptions?msg=...&type=...", status_code=303)`
模式，HTMX 跟随 303 整页刷新 → 正确。

### 1.2 Bug B — UI 不一致

`web/templates/subscriptions.html` line 50-90 区间，每个订阅博主下方有：

- 已绑定端点的 chip（带 × 移除按钮）
- 一个"未分配端点下拉 + + 按钮"的内联表单
- 一个"删除订阅"按钮

当前样式偏简陋：chip 是直接写在模板里的 `bg-blue-50`，加端点的 `<select>`+`+` 按钮
没有和整站 Apple-style card（rounded-[14px] / backdrop-blur / CSS 变量）对齐。
endpoints.html 的卡片样式是基线参考。

**任务 B 必须在任务 A 之后做**：因为加/移端点表单仍走 303 重定向模式才不会白屏。

---

## 2. 关键决策（已锁定，理由附后）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | URL toast param 命名 | `?toast_key=<key>&type=<success\|error>` | 与现有 `TOAST_KEY_MAP`（base.html:152-161）契约一致；避免把本地化中文塞进 URL（既丑又有 URL-encoding 风险） |
| D2 | 是否兼容现有 `?msg=...` | **是**，base.html URL-flash JS 同时支持 `msg`（直接显示）和 `toast_key`（查 map） | `subscriptions_add/remove` 当前还在用 `msg`；本 plan 范围不含改造它们。双轨并存，后续可统一清理 |
| D3 | 4 个回归测试如何处理 | **改写为新契约**（验证 303 + Location header + query param），原 HX-Trigger 断言删除 | HX-Trigger 已经不是这些路由的契约；保留旧测试会失败 |
| D4 | `hx-target="body"` 是否清理 | **不清理**（5 个表单） | 303 模式下整页刷新，HTMX 不再 swap body，保留无害；改动越多风险越大。在 plan 末尾的"后续可选清理"列出 |
| D5 | base.html URL-flash JS 放在哪 | 直接放 base.html 全局 script 块（line 104-194），不要新文件 | subscriptions.html 已有页面级实现（line 124-134），把它抽到 base.html 让 endpoints/subscriptions 都用一份。任务 A 完成后 subscriptions.html 的本地脚本可删 |
| D6 | 是否新增 `TOAST_KEY_MAP` key | **复用全部已有 key**，无需新增 | 现有 key 覆盖：`endpoint.saved` / `endpoint.deleted` / `endpoint.name_exists` / `endpoint.not_found` / `subscription.endpoint_added` / `subscription.endpoint_removed` / `subscription.not_found` 全够用 |
| D7 | 任务 B 是否新增 macro | **可能**新增 `_macros.html` 中的 `endpoint_chip` macro（可选，若 subscriptions.html inline chip 重复足够多次） | 任务 B 实施时判断。当前只有 1 处 chip，inline 也 OK |
| D8 | 不改动范围 | `settings.py`（仍 HX-Trigger，但 settings 表单不是 `hx-target=body`）、`subscriptions_add/remove`（已经是 303，但用 `msg` 不是 `toast_key`） | 最小改动原则 |

---

## 3. 文件清单（增/改/删行数估计）

| 文件 | 操作 | 改动 | 估计行数 |
|---|---|---|---|
| `web/routes/endpoints.py` | 改 | import 加 `RedirectResponse`；3 个路由返回值改 303；删除所有 HX-Trigger 路径 | -20 / +15 |
| `web/routes/subscriptions.py` | 改 | `subscription_endpoint_add` / `subscription_endpoint_remove` 改返回 `RedirectResponse` | -15 / +10 |
| `web/templates/base.html` | 改 | 全局 script 块新增 URL-flash → toast 解析（支持 `msg` 和 `toast_key`） | +20 |
| `web/templates/subscriptions.html` | 改 | 删除 line 123-134 的页面级 URL-flash 脚本（已上提到 base.html）；任务 B 重写 line 50-90 chip + add form 区块 | -12 / 任务A: 0；任务B: -25 / +40 |
| `web/templates/endpoints.html` | 改 | 任务 A 不改；任务 B 不强制改（其卡片已是基线） | 0 |
| `web/templates/_macros.html` | 改（可选） | 任务 B 可选新增 `endpoint_chip` macro | +15 |
| `tests/test_web_toast_headers.py` | 改 | 4 个测试改写为新契约；模块 docstring 更新 | -50 / +60 |
| `tests/test_web_subscriptions.py` | 读+可能补 | 确认是否已有 303 重定向回归；若无，加 1-2 条端到端 | +10 |

**总估计**：净改动 ~150 行代码 + ~80 行测试。

---

## 4. 任务依赖图

```
A1 (base.html URL-flash JS)  ← 必须先做，否则 303 后无 toast
   │
   ├── A2 (endpoints.py 3 路由)         ← 独立
   ├── A3 (subscriptions.py 2 路由)     ← 独立
   └── A4 (subscriptions.html 删本地脚本) ← 依赖 A1
         │
         └── A5 (测试改写)              ← 依赖 A2 + A3
               │
               └── B1 (subscriptions.html UI 重写) ← 依赖 A 全部完成
```

---

## 5. 任务 A — 修复白屏（TDD）

### Task A1: 在 base.html 增加全局 URL-flash → toast 解析

**锚点**: `web/templates/base.html` line 173 之后（`htmx:afterOnLoad` listener
之后，`toggleSidebar` 之前）。

**改前**（line 173-175 之间）:
```js
    });

    // ── Mobile sidebar toggle ───────────────────────────────────
```

**改后**:
```js
    });

    // ── URL flash → toast on page load ─────────────────────────
    // Supports two query-string contracts:
    //   ?msg=<text>&type=<success|error>           → show text as-is
    //   ?toast_key=<key>&type=<success|error>      → look up TOAST_KEY_MAP
    (function() {
      var params = new URLSearchParams(window.location.search);
      var type = params.get('type');
      var msg = params.get('msg');
      if (!msg) {
        var key = params.get('toast_key');
        if (key) msg = TOAST_KEY_MAP[key] || '';
      }
      if (msg) {
        showToast(msg, type || 'info');
        // Clean URL so refresh/toast doesn't replay
        window.history.replaceState({}, '', window.location.pathname);
      }
    })();

    // ── Mobile sidebar toggle ───────────────────────────────────
```

**注意**:
- 引用全局 `TOAST_KEY_MAP`（已在 line 152 定义），不用重复声明。
- `?msg=` 路径保持兼容 `subscriptions_add/remove`。
- `history.replaceState` 清掉 query string 防止刷新重复弹 toast。

**无独立测试**（前端 JS，靠 task A5 的 e2e 测试间接覆盖）。

---

### Task A2: 改 `web/routes/endpoints.py` 3 个路由

**改前 import (line 9)**:
```python
from fastapi.responses import HTMLResponse
```

**改后**:
```python
from fastapi.responses import HTMLResponse, RedirectResponse
```

> `HTMLResponse` 仍被 `endpoints_page` 使用，保留。

#### A2.1 `endpoint_add` (line 57-77)

**改前** (line 65-77):
```python
    endpoints = await _load_endpoints()
    if any(ep.name == name for ep in endpoints):
        return HTMLResponse(
            content="",
            status_code=400,
            headers={"HX-Trigger": '{"toast":{"key":"endpoint.name_exists","type":"error"}}'},
        )
    endpoints.append(EndpointConfig(name=name, url=url, token=token, priority=priority, kind=kind))
    _save_endpoints(endpoints)
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"key":"endpoint.saved","type":"success"}}'},
    )
```

**改后**:
```python
    endpoints = await _load_endpoints()
    if any(ep.name == name for ep in endpoints):
        return RedirectResponse(
            url="/endpoints?toast_key=endpoint.name_exists&type=error",
            status_code=303,
        )
    endpoints.append(EndpointConfig(name=name, url=url, token=token, priority=priority, kind=kind))
    _save_endpoints(endpoints)
    return RedirectResponse(
        url="/endpoints?toast_key=endpoint.saved&type=success",
        status_code=303,
    )
```

**注意**: 错误路径也返回 303（不是 400）。这是有意为之——浏览器/HTMX 跟随 303
到 GET /endpoints 后由前端 toast 显示错误。`test_web_toast_headers.py` 旧测试断言
`resp.status_code == 400` 必须同步改。

返回类型注解 `-> HTMLResponse` 改为 `-> RedirectResponse`。

#### A2.2 `endpoint_edit` (line 80-106)

**改前** (line 88-106): 含 404 + 200 两条 HX-Trigger 路径。

**改后**:
```python
    endpoints = await _load_endpoints()
    for ep in endpoints:
        if ep.name == name:
            ep.url = url
            ep.token = token
            ep.priority = priority
            ep.enabled = enabled
            break
    else:
        return RedirectResponse(
            url="/endpoints?toast_key=endpoint.not_found&type=error",
            status_code=303,
        )
    _save_endpoints(endpoints)
    return RedirectResponse(
        url="/endpoints?toast_key=endpoint.saved&type=success",
        status_code=303,
    )
```

返回类型注解改为 `-> RedirectResponse`。

#### A2.3 `endpoint_delete` (line 109-117)

**改前** (line 111-117):
```python
    endpoints = await _load_endpoints()
    endpoints = [ep for ep in endpoints if ep.name != name]
    _save_endpoints(endpoints)
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"key":"endpoint.deleted","type":"success"}}'},
    )
```

**改后**:
```python
    endpoints = await _load_endpoints()
    endpoints = [ep for ep in endpoints if ep.name != name]
    _save_endpoints(endpoints)
    return RedirectResponse(
        url="/endpoints?toast_key=endpoint.deleted&type=success",
        status_code=303,
    )
```

返回类型注解改为 `-> RedirectResponse`。

---

### Task A3: 改 `web/routes/subscriptions.py` 2 个路由

#### A3.1 `subscription_endpoint_add` (line 101-144)

返回类型 line 106 `-> HTMLResponse` 改为 `-> RedirectResponse`。

**改前** success 路径 (line 140-144):
```python
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"key":"subscription.endpoint_added","type":"success"}}'},
    )
```

**改后**:
```python
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return RedirectResponse(
        url="/subscriptions?toast_key=subscription.endpoint_added&type=success",
        status_code=303,
    )
```

**改前** not-found 路径 (line 134-139):
```python
    if not found:
        return HTMLResponse(
            content="",
            status_code=404,
            headers={"HX-Trigger": '{"toast":{"key":"subscription.not_found","type":"error"}}'},
        )
```

**改后**:
```python
    if not found:
        return RedirectResponse(
            url="/subscriptions?toast_key=subscription.not_found&type=error",
            status_code=303,
        )
```

**改前** file-not-exist 路径 (line 108-110):
```python
    if not p.exists():
        return HTMLResponse(content="", status_code=404)
```

**改后**（保持 404 但用 redirect 实现，仍走 error toast）:
```python
    if not p.exists():
        return RedirectResponse(
            url="/subscriptions?toast_key=subscription.not_found&type=error",
            status_code=303,
        )
```

#### A3.2 `subscription_endpoint_remove` (line 147-188)

返回类型 line 152 `-> HTMLResponse` 改 `-> RedirectResponse`。

完全对称改造：
- file-not-exist 路径 (line 154-156) → 303 + `subscription.not_found`
- not-found 路径 (line 178-183) → 303 + `subscription.not_found`
- success 路径 (line 184-188) → 303 + `subscription.endpoint_removed`

**改后 success**:
```python
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return RedirectResponse(
        url="/subscriptions?toast_key=subscription.endpoint_removed&type=success",
        status_code=303,
    )
```

---

### Task A4: 清理 `web/templates/subscriptions.html` 本地脚本

**锚点**: `subscriptions.html` line 123-134（页面级 URL-flash IIFE）。

**删除整段**:
```html
<script>
  // URL flash → toast on page load
  (function() {
    var params = new URLSearchParams(window.location.search);
    var msg = params.get('msg');
    var type = params.get('type');
    if (msg) {
      showToast(msg, type || 'info');
      window.history.replaceState({}, '', window.location.pathname);
    }
  })();
</script>
```

理由：A1 已经把同样逻辑（且更通用，支持 `toast_key`）提到 base.html，本段重复。

**依赖**: A1 必须先合并，否则 `?msg=` 路径丢失（subscriptions_add/remove 仍发 `msg`）。

---

### Task A5: 改写 `tests/test_web_toast_headers.py`（TDD — 先改测试看它失败，再实现 A2/A3）

> **TDD 顺序说明**：本任务是"行为契约变更"，测试是契约的体现。先改测试 → 跑测试
> 看红 → 实施 A2/A3 → 跑测试看绿。但 A1 必须在手动验证之前完成（否则无 toast
> 显示）。单元测试层面 A1 不影响断言（只测 HTTP 层）。

**改写策略**：
1. **模块 docstring 改写**：原 docstring 讲的是"HX-Trigger 必须 ASCII"的回归。
   新 docstring 讲的是"POST 写操作 → 303 重定向 + Location 携带 toast_key"。
2. **文件名保留**（不改名，避免 git history 噪音），但内部测试类重命名：
   - `TestEndpointAddToastHeader` → `TestEndpointAddRedirect`
   - `TestEndpointEditToastHeader` → `TestEndpointEditRedirect`
   - `TestSubscriptionEndpointAddToastHeader` → `TestSubscriptionEndpointAddRedirect`
3. **新断言模板**:
   ```python
   # 写操作成功
   assert resp.status_code == 303
   assert resp.headers["location"].startswith("/endpoints")
   assert "toast_key=endpoint.saved" in resp.headers["location"]
   assert "type=success" in resp.headers["location"]
   # follow_redirects=False 才能拿到 303；测试 fixture 已是这样
   ```

#### 改写后的 4 个测试（具体代码骨架）

**Test 1: `endpoint_add` 重复名 → 重定向到错误 toast**

```python
class TestEndpointAddRedirect:
    """After fix: endpoint_add with duplicate name redirects to /endpoints
    with error toast_key, so HTMX does full-page refresh and body stays
    intact (no white screen)."""

    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_duplicate_name_redirects_with_error_toast_key(
        self, mock_save, mock_load, client: AsyncClient
    ) -> None:
        from shared.config import EndpointConfig
        mock_load.return_value = [EndpointConfig(name="ops", url="https://g", token="t")]
        resp = await client.post(
            "/endpoints/add",
            data={"name": "ops", "url": "https://x", "token": "tok"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/endpoints")
        assert "toast_key=endpoint.name_exists" in loc
        assert "type=error" in loc
        mock_save.assert_not_called()
```

**Test 2: `endpoint_edit` 不存在 → 重定向到错误 toast**

类似结构，断言：
- `resp.status_code == 303`
- location 含 `toast_key=endpoint.not_found` + `type=error`

**Test 3: `subscription_endpoint_add` 成功 → 重定向到 success toast**

完全沿用原 `test_add_endpoint_success_returns_ascii_key_toast` 的 mock setup
（tomlkit fake doc），但断言改为：
- `resp.status_code == 303`
- location 含 `toast_key=subscription.endpoint_added` + `type=success`

**Test 4: `subscription_endpoint_add` 订阅不存在 → 重定向到 error toast**

沿用原 `test_add_endpoint_subscription_not_found_returns_ascii_key_toast`
的 mock setup，断言改为：
- `resp.status_code == 303`
- location 含 `toast_key=subscription.not_found` + `type=error`

**可选新增 Test 5**: 补一个 happy path 的 `endpoint_add` 成功 → 303 + `endpoint.saved`
（原测试只覆盖了 error 路径），保证对称。

#### Task A5 验证

```bash
uv run pytest tests/test_web_toast_headers.py -v
# 期望：4 (or 5) passed
```

**注意 fixtures**：原 fixture（line 26-35）登录后 `follow_redirects=False` 隐含
由 `AsyncClient` 默认 False。新测试必须显式 `follow_redirects=False`（已经在
骨架里写了），因为 client fixture 本身没设这个默认。

---

### Task A 验证（手动）

```bash
# 1. 启动 dev server
uv run uvicorn web.app:create_app --factory --reload --port 8000 &

# 2. 浏览器（或 curl）逐项验证
#   - POST /endpoints/add 成功 → 浏览器跳转 /endpoints，看到 toast "端点已保存"
#   - POST /endpoints/add 重名 → toast "端点名称已存在"，页面不白屏
#   - POST /endpoints/{name}/edit → toast "端点已保存"
#   - POST /endpoints/{name}/delete → toast "端点已删除"，列表里该项消失
#   - POST /subscriptions/{plat}/{id}/endpoints/add → toast "端点已添加"
#   - POST /subscriptions/{plat}/{id}/endpoints/remove → toast "端点已移除"
# 3. URL 刷新后无 toast 残留（验证 history.replaceState）
```

---

## 6. 任务 B — 订阅端点 UI 美化

**前置**: 任务 A 全部完成（5 路由走 303 + toast 显示正常）。

### Task B1: 重写 `subscriptions.html` line 44-95 的订阅 item + 端点区块

**当前结构问题**（line 47-90）：
- chip 样式硬编码（line 57），不和 `_macros.html badge` 一致
- "添加端点"的 `<select>` + `+` 按钮样式像 form 控件，不像 chip 风格
- 删除订阅按钮 hover 才显示，移动端不可用
- 整个 item row 在窄屏会挤压

**设计目标**（与 endpoints.html 一致的 Apple style）:
- 每个 subscription 是一个 sub-card（不用单独 rounded-[14px]，因为已经在
  platform card 内；但用 `bg-gray-50/50 dark:bg-gray-800/30 rounded-[10px]`
  分层）
- 已绑定端点 chip 用 `badge` macro 或 inline 但加 `border` 与整站 token 对齐
- "添加端点"控件改为：`+ 添加端点` 文字按钮触发内联下拉，或者直接显示下拉
  但样式统一为 chip-like（`rounded-[8px]` `border` `bg-white dark:bg-gray-800`）
- 删除订阅按钮：从 hover-only 改为常驻但弱化（`text-[var(--text-tertiary)]`
  + hover 变红），保证移动端可用

#### B1.1 具体 Jinja 改写

**锚点**: `subscriptions.html` line 47-90（item row 内部 + add form）。

**改后骨架**（只列改动区域，前后缩进对齐 line 47 的 `<div class="flex items-center...">`）:

```html
<div class="flex items-center justify-between py-2.5 px-3 rounded-[10px]
            hover:bg-gray-50/50 dark:hover:bg-gray-800/30 group transition-colors">
  <div class="flex items-center gap-3 min-w-0 flex-1">
    {{ status_dot("active") }}
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-2">
        <span class="text-sm font-medium truncate">{{ item.get("name", "-") }}</span>
        <span class="text-[11px] text-[var(--text-tertiary)] font-mono">
          {{ item.get("uid") or item.get("user_id", "-") }}
        </span>
      </div>
      {% set item_eps = item.get("notify_endpoints", []) %}
      {% if item_eps %}
      <div class="flex flex-wrap gap-1 mt-1.5">
        {% for ep_name in item_eps %}
        <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-[8px]
                     text-[11px] font-medium border
                     bg-blue-50/60 text-blue-700 border-blue-200/60
                     dark:bg-blue-900/20 dark:text-blue-300 dark:border-blue-800/40">
          {{ ep_name }}
          <button type="button"
            hx-post="/subscriptions/{{ p.key }}/{{ item.get('uid') or item.get('user_id', '') }}/endpoints/remove"
            hx-vals='{"endpoint_name":"{{ ep_name }}"}'
            class="ml-0.5 -mr-0.5 w-3.5 h-3.5 inline-flex items-center justify-center
                   rounded-full hover:bg-red-500/20 hover:text-red-500
                   cursor-pointer transition-colors text-[14px] leading-none"
            title="移除端点 {{ ep_name }}">&times;</button>
        </span>
        {% endfor %}
      </div>
      {% endif %}
    </div>
  </div>
  <div class="flex items-center gap-1.5 shrink-0">
    {% set unassigned = item.get("_unassigned_eps", []) %}
    {% if unassigned %}
    <form hx-post="/subscriptions/{{ p.key }}/{{ item.get('uid') or item.get('user_id', '') }}/endpoints/add"
          class="inline-flex items-center gap-1">
      <select name="endpoint_name"
        class="text-xs px-2 py-1 rounded-[8px] border border-[var(--card-border)]
               bg-[var(--card-bg)] text-[var(--text-secondary)]
               focus:ring-2 focus:ring-[var(--color-primary)]/20 outline-none max-w-[120px]">
        {% for aep in unassigned %}
        <option value="{{ aep }}">{{ aep }}</option>
        {% endfor %}
      </select>
      <button type="submit"
              class="inline-flex items-center justify-center w-6 h-6 rounded-[6px]
                     bg-apple-blue/10 text-apple-blue hover:bg-apple-blue hover:text-white
                     text-sm font-medium cursor-pointer transition-colors"
              title="添加端点">+</button>
    </form>
    {% endif %}
    <form hx-post="/subscriptions/remove"
          hx-confirm="确定删除此订阅？">
      <input type="hidden" name="platform" value="{{ p.key }}">
      <input type="hidden" name="identifier" value="{{ item.get('uid') or item.get('user_id', '') }}">
      <button type="submit"
              class="text-sm text-[var(--text-tertiary)] hover:text-red-500
                     transition-colors cursor-pointer px-1"
              title="删除订阅">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="3 6 5 6 21 6"/>
          <path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
        </svg>
      </button>
    </form>
  </div>
</div>
```

**改动要点**:
1. chip 加 border + 用 CSS 变量驱动暗色模式（对齐整站 token）
2. × 按钮变成圆形 hover 反色，鼠标区域稍大
3. 加端点 `+` 按钮改成 apple-blue tinted 方块，hover 反色
4. 删除订阅改为垃圾桶 SVG（不再 hover-only，常驻但弱化）
5. item row 加 `transition-colors`，padding 微调
6. `hx-target="body"` 保留（决策 D4）

#### B1.2 可选 — 提 macro

如果任务 B 实施中发现 chip 在多处重复（例如未来 dashboard 也要用），把上面的
`<span class="...chip...">...</span>` 抽到 `_macros.html`:

```html
{% macro endpoint_chip(name, remove_action_url) -%}
<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-[8px] text-[11px]
             font-medium border bg-blue-50/60 text-blue-700 border-blue-200/60
             dark:bg-blue-900/20 dark:text-blue-300 dark:border-blue-800/40">
  {{ name }}
  {% if remove_action_url %}
  <button type="button" hx-post="{{ remove_action_url }}"
          class="ml-0.5 -mr-0.5 w-3.5 h-3.5 inline-flex items-center justify-center
                 rounded-full hover:bg-red-500/20 hover:text-red-500
                 cursor-pointer transition-colors text-[14px] leading-none"
          title="移除端点 {{ name }}">&times;</button>
  {% endif %}
</span>
{%- endmacro %}
```

**判断标准**: 当前只有 1 处使用 → inline 即可，不强行抽 macro（最小改动）。

---

### Task B 验证

```bash
# 1. 启动 dev server（同 A）
# 2. 浏览器验证 /subscriptions:
#    - chip 在浅色/暗色模式下都清晰可读
#    - 鼠标 hover × 按钮变红，点击后 toast "端点已移除"，chip 消失
#    - 添加端点 + 按钮可点，点击后 toast "端点已添加"，下拉选项减少，新 chip 出现
#    - 删除订阅按钮常驻可见，点击有 confirm，确认后 toast "..."
#    - 移动端尺寸下（< 768px）布局不挤压，按钮都可点
# 3. 不需要新单测（纯样式改动，无逻辑契约变化）
```

---

## 7. 风险与不确定项

| 风险 | 等级 | 缓解 |
|---|---|---|
| base.html URL-flash JS 顺序：必须在 `TOAST_KEY_MAP` 声明之后才能引用 | 低 | A1 改动锚点明确（line 173 之后），且 `TOAST_KEY_MAP` 在 line 152 已声明，JS 顺序天然满足 |
| `subscriptions_add/remove` 仍用 `?msg=` 而非 `?toast_key=` | 低 | A1 的 JS 同时支持两种契约（D2）。本 plan 不改造这两条路由 |
| 错误路径返回 303 而非 4xx，调用方可能依赖 status code | 低 | 这 5 个路由只服务 HTMX form，无程序化调用方。test_web_toast_headers.py 是唯一已知断言 status 的，已在本 plan 同步改写 |
| `settings.py` 仍返回 HX-Trigger | 无 | 不在范围。settings 表单不是 `hx-target=body`（决策 D8），无白屏 bug。test_web_settings*.py 不受影响 |
| HTMX 对 303 + 空 body 的行为差异（某些版本可能不跟随） | 低 | HTMX 2.0.4（base.html:7）默认 `followRedirects=true`。subscriptions_add 已用此模式且生产可用 |
| 任务 B 的暗色模式 CSS 变量未在所有 token 上验证 | 中 | B1 实施后必须切暗色模式目视检查（macOS 系统偏好或浏览器 devtools） |
| `test_web_subscriptions.py` 可能已有 303 测试与本 plan 重复 | 低 | A5 实施前先 `grep -r "follow_redirects" tests/test_web_subscriptions.py`，避免重复 |
| `?msg=` 路径里包含中文（如 `subscriptions_add` 失败时）会导致 URL 编码 | 中（已存在，不在本 plan 范围） | A1 的 JS 用 `URLSearchParams.get` 自动 decode，无新风险。后续 cleanup task 可把 subscriptions_add/remove 也迁到 toast_key |

---

## 8. 回滚策略

每个 task 是独立 git commit（建议），按 task 粒度回滚：

- **A1 回滚**：`git revert <A1-commit>` → base.html URL-flash JS 删除。注意 A4
  依赖 A1，回滚 A1 前先回滚 A4。
- **A2 回滚**：`git revert <A2-commit>` → endpoints.py 恢复 HX-Trigger。注意 A5
  测试此时会失败，需同时回滚 A5。
- **A3 回滚**：同 A2。
- **A4 回滚**：恢复 subscriptions.html 本地脚本。
- **A5 回滚**：恢复 test_web_toast_headers.py（同时需回滚 A2/A3，否则旧测试失败）。
- **B1 回滚**：恢复 subscriptions.html line 47-90，独立。

**原子回滚方案**：若 task A 全部失败需整体回滚到 master：
```bash
git checkout master -- web/routes/endpoints.py web/routes/subscriptions.py \
                       web/templates/base.html web/templates/subscriptions.html \
                       tests/test_web_toast_headers.py
```

---

## 9. 后续可选清理（不在本 plan 范围）

1. `subscriptions_add/remove` 也迁到 `?toast_key=` 契约（统一 URL-flash 协议）
2. 删除 `web/templates/endpoints.html` 和 `subscriptions.html` 中 5 处
   `hx-target="body"`（303 模式下不需要；决策 D4）
3. `settings.py` 评估是否也走 303 模式（独立 bug 调研，本 plan 不预判）
4. 抽 `_macros.html endpoint_chip` macro（决策 B1.2）

---

## 10. 执行顺序汇总（给实施 agent）

```
1. 写 A5 新测试（红）
2. 实施 A1（base.html JS）
3. 实施 A2（endpoints.py 3 路由）+ 跑 A5（部分绿）
4. 实施 A3（subscriptions.py 2 路由）+ 跑 A5（全绿）
5. 实施 A4（删 subscriptions.html 本地脚本）
6. 跑全套验证：ruff + pyright + pytest tests/test_web_toast_headers.py + 手动
7. 实施 B1（subscriptions.html UI 重写）
8. 手动验证 B + 暗色模式检查
9. 跑 ruff + pyright + 全套 pytest（确保无回归）
```

**TDD 注**：A5 的"先写测试看红"在第 1 步，但 A1 不在 HTTP 层断言范围（前端 JS）。
第 1 步跑测试时 A2/A3 还没改，5 个测试中至少 4 个会红（断言 303 失败）。
若第 1 步希望看到全红再渐进转绿，按上述顺序即可。

---

## 用户决策（2026-06-25 拍板）

**任务 A（白屏）+ 任务 B（订阅端点 UI 美化）合并到同一个 PR**。
- 任务 B 必须基于任务 A 的 303 重定向模式（避免二次重写）
- UI 改动**必须加载 ui-ux-pro-max skill**，按 Apple-style 风格执行
- 执行顺序：先完成 A（5 路由 + base.html + 测试），再做 B（订阅端点 UI 美化）

---

## @oracle Review 反馈（2026-06-25）— 实施前必读

plan 正文经 oracle 审查，issues=5（3 major + 2 minor），**已在下方澄清，实施时按澄清执行**：

### Major 1 — 依赖图误导（§4）
原文把 A1 画成 A2/A3 的前置依赖，错误。**实际依赖**：
- A1 只与 A4 相关（A4 删 subscriptions.html 本地脚本前 A1 必须已合并）
- A1 与 A2/A3/A5 的 HTTP 契约完全解耦
- A5（HTTP 层 303+location 断言）可在 A1 之前跑红测试，不受影响

### Major 2 — file-not-exist 与 sub-not-found 语义合并（Task A3.1）
A3.1 把 `subscriptions.toml` 文件不存在（line 108-110）和 identifier 找不到（line 134-139）两条路径都映射到 `subscription.not_found`。**这是有意的语义合并**——对用户而言表现都是"订阅不存在"。实施时无需额外处理，但若加注释请说明此意图。

### Major 3 — 文件清单的 `test_web_subscriptions.py`（§3）
原文 §3 列了 `test_web_subscriptions.py | 读+可能补 | +10`，但 §5 Task A5 和 §10 执行顺序都没列为正式 task。**澄清**：
- 实施前先 `grep -n "303\|status_code" tests/test_web_subscriptions.py` 检查现有覆盖
- 若已有 303 重定向回归测试 → 跳过
- 若无 → 并入 Task A5 作子步骤 A5.6 补 1-2 条 e2e
- 不要从 §3 删除条目（保留作为 reminder）

### Minor 1 — 测试 X-Requested-With header（Task A5）
改写后的测试骨架未保留 `headers={"X-Requested-With": "XMLHttpRequest"}`。**澄清**：
- endpoints.py 3 路由**不读**此 header（已 oracle 核实）
- 可删可留，**保留以减少 diff**（与原测试输入一致更稳）

### Minor 2 — 函数完整签名歧义（Task A2/A3）
plan 改后代码块只展示 body，未展示完整函数签名。**澄清**：
- 每个路由**仅改 body**，签名（`@router.post(...)` + `def name(...: Form(...), ...) -> ...`）**不动**
- 唯一签名变化：返回类型注解 `-> HTMLResponse` 改为 `-> RedirectResponse`

**Status: approved (with above clarifications)**
