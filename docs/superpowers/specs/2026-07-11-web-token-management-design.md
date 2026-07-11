# Web UI Token 管理 + Sub Ownership 分配界面 — 设计文档

- **Issue**: #111
- **分支**: `feat/web-token-management-111`
- **前置依赖**: #108 / #109（多租户 ownership 模型，v1.2.0 已部署）
- **创建日期**: 2026-07-11

## 1. 背景

#108 完成了 API 层的多租户权限模型（`owner_token` + `assigned_tokens` + `tokens:manage` superuser）。spec §3 明确将 Web UI token 管理列为**非目标**，挂本 issue。

**当前状态**：
- API 层权限模型已就绪（v1.2.0 已部署）
- Web UI 完全不经 ownership 层（走 session cookie，admin 登录 = 隐式 superuser）
- Web UI 无 token 管理页面（创建/列表/revoke/assign 都只能通过 `trawler api-token` CLI）
- 6 个 sub 全无 `owner_token`（孤儿，Web UI 照常可管因为 session 绕过 ownership）

**目标**：Web UI 加 token 管理 + sub ownership 分配界面，让 admin 不必 SSH 进服务器跑 CLI。

## 2. 范围

### 2.1 功能清单

1. **Token 列表页**（独立 sidebar 项「API Token」，路由 `/tokens`）
2. **创建 token**（name + 7 scope checkbox + 明文一次性 banner）
3. **Revoke token**（HTMX confirm dialog）
4. **Sub ownership 分配界面**（订阅页 badge + HTMX modal 编辑）
5. **Adopt 孤儿 sub**（modal 内选 owner token）

### 2.2 非目标

- Web UI session 本身不改（session = superuser，不走 API scope/ownership）
- API token 明文不存储、不持久化（仅创建时一次性 session flash 显示）
- 不做 token 级别 Web UI 登录（Web UI 永远走 admin session）
- 不做创建 token 表单的 `default_notify_endpoint` 字段（MVP 去掉，issue 标注「可选」）

## 3. 架构决策

### 3.1 方案选择：薄 wrapper

**选定方案**：Web 路由直接调用现有 CLI 纯函数（`create_token` / `revoke_token` / `assign_token_to_subscription` / `unassign_token_from_subscription` / `set_subscription_owner`）。

**理由**：这些纯函数已被 CLI + API v1 两处复用验证，Web 路由只需做参数校验 + 调用 + Redirect 303 反馈，与现有 `web/routes/endpoints.py` 模式一致。最大化复用，最小化改动。

**对比方案**（已否决）：
- Web service 层（`web/services/token_service.py`）— 多一层间接，重复 CLI 校验
- 内联到路由 — 重复逻辑，易漂移

### 3.2 关键决策（brainstorming 确认）

| 决策点 | 选择 | 理由 |
|---|---|---|
| Token 管理页入口 | 独立 sidebar 项「API Token」，路由 `/tokens` | token 是独立资源，不是设置的子项 |
| Sub ownership 编辑位置 | 订阅页列表 badge + HTMX modal 编辑 | 列表保持简洁，编辑操作独立，不跳页 |
| 创建后明文显示 | 创建后 inline banner | 与现有 Redirect 303 模式一致 |
| 明文传递机制 | **Session flash 存储** | 不进 URL/history/log，最安全 |
| Scope 选择 UI | 平铺 checkbox + warning | 与 CLI `--scope` 参数一一对应 |
| Adopt 孤儿 sub 的 owner | 弹 modal 选 token | owner 必须是具体 token，不能是抽象「admin」 |
| `default_notify_endpoint` 字段 | MVP 去掉 | endpoint 绑定已在订阅页，避免重复职责 |

## 4. 文件结构

### 4.1 新增文件

| 文件 | 职责 |
|---|---|
| `web/routes/tokens.py` | token CRUD 路由（list / create / revoke） |
| `web/routes/subscription_ownership.py` | sub ownership 路由（modal / assign / unassign / set_owner） |
| `web/templates/tokens.html` | token 列表 + 创建表单 + inline banner |
| `web/templates/_token_modal.html` | HTMX modal partial（sub ownership 编辑） |
| `tests/test_web_tokens.py` | token 管理路由测试 |
| `tests/test_web_subscription_ownership.py` | ownership 路由测试 |

### 4.2 修改文件

| 文件 | 改动 |
|---|---|
| `web/app.py:254-275` | 注册 token + ownership 路由 |
| `web/templates/base.html` | sidebar 加「API Token」nav item；`TOAST_KEY_MAP` 加 token 相关 key |
| `web/templates/subscriptions.html` | 每 sub 行加 owner badge + 「管理 ownership」按钮（孤儿额外显示「设为 owner」） |

## 5. 路由设计

### 5.1 Token 管理路由

| Method | Path | 功能 |
|---|---|---|
| GET | `/tokens` | token 列表 + 创建表单（session flash 有明文时显示 banner） |
| POST | `/tokens/create` | 创建 token，set session flash，Redirect 303 → `/tokens` |
| POST | `/tokens/revoke` | revoke token，Redirect 303 → `/tokens?msg=revoked` |

### 5.2 Sub Ownership 路由

| Method | Path | 功能 |
|---|---|---|
| GET | `/subscriptions/<platform>/<id>/ownership` | HTMX 返回 ownership modal HTML |
| POST | `/subscriptions/<platform>/<id>/owner` | set owner（body: `token_name`），adopt 孤儿也走此路由 |
| POST | `/subscriptions/<platform>/<id>/assign` | assign token to sub（body: `token_name`） |
| POST | `/subscriptions/<platform>/<id>/unassign` | unassign token from sub（body: `token_name`） |

## 6. 详细设计

### 6.1 Token 列表页（`/tokens`）

**布局**（参考 `endpoints.html` 的 list + add 模式）：

```
┌─────────────────────────────────────────────────┐
│ API Token 管理                                   │
│ ┌─────────────────────────────────────────────┐ │
│ │ [inline banner — 仅 session flash 有明文时]  │ │
│ │ ⚠️ Token「xxx」明文（仅此一次）：             │ │
│ │ ┌──────────────────────────┐ [复制]          │ │
│ │ │ trawler_xxxxxxxxxxxxxxxx │                │ │
│ │ └──────────────────────────┘                │ │
│ │ 刷新后丢失，请立即保存                       │ │
│ └─────────────────────────────────────────────┘ │
│                                                  │
│ ┌── 创建 Token ──────────────────────────────┐  │
│ │ Name: [______________]                      │  │
│ │ Scopes:                                     │  │
│ │   ☐ subscriptions:read   ☐ subscriptions:write │
│ │   ☐ messages:read        ☐ messages:write   │  │
│ │   ☐ check:read           ☐ check:run        │  │
│ │   ☐ tokens:manage  ⚠️ superuser 全权         │  │
│ │ [创建]                                      │  │
│ └────────────────────────────────────────────┘  │
│                                                  │
│ ┌── 已有 Token ──────────────────────────────┐  │
│ │ name    │ hash前8位 │ scopes    │ created  │  │
│ │ admin   │ a1b2c3d4 │ tokens:… │ 07-10    │  │
│ │ viewer  │ e5f6g7h8 │ subs:read│ 07-09  [revoke]│
│ └────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

**列表显示字段**：
- `name` — token 名称
- `token_hash` 前 8 位（`api/auth.py` 存储 SHA-256 hexdigest，截断显示）
- `scopes` — 用 badge 展示（参考 `_macros.html` 的 `badge` 宏）
- `created_at` — 格式化日期
- **不显示明文**

### 6.2 创建 Token 流程

```
POST /tokens/create
  form: name=<str>, scopes=<list[str]>
  → create_token(name, scopes)  # 纯函数，返回明文 token
  → request.session["created_token_name"] = name
  → request.session["created_token_plaintext"] = plaintext
  → Redirect 303 → /tokens

GET /tokens
  → 读 session["created_token_plaintext"]
  → 渲染 banner（带明文 + 复制按钮）
  → 立即 pop session["created_token_*"]  # 一次性
```

**复制按钮**：JS `navigator.clipboard.writeText(plaintext)` + toast 反馈。

### 6.3 Revoke Token

每行 revoke 按钮：
- `hx-post="/tokens/revoke"` + `hx-confirm="确定 revoke token「xxx」？"`
- form hidden field: `token_name=<name>`
- 路由调 `revoke_token(name)` → Redirect 303 → `/tokens?msg=revoked`

### 6.4 Sub Ownership Modal

**订阅页改动**（`subscriptions.html`）：

每条 sub 行加：
- **Owner** badge：显示 `owner_token` 或「孤儿」（红色 badge）
- **操作**：「管理 ownership」按钮（孤儿显示「设为 owner」）

**Modal HTML**（`_token_modal.html`，HTMX `hx-get="/subscriptions/<platform>/<id>/ownership"`）：

```
┌── 管理 Ownership ─────────────────────────────┐
│ Sub: <platform>/<id>                           │
│                                                 │
│ Owner: [dropdown: 当前 owner / 其他 token / 无] │
│         [改 owner]  (POST /owner)               │
│                                                 │
│ Assigned tokens (只读权限):                     │
│   ☑ token_a   (change → POST /assign 或 /unassign)│
│   ☐ token_b                                     │
│   ☐ token_c                                     │
│                                                 │
└─────────────────────────────────────────────────┘
```

**交互模式**（与现有 endpoint 绑定一致）：
- Owner dropdown + 「改 owner」按钮 → 单独 POST `/owner`
- Assigned 每个 checkbox 独立 submit（勾选 = POST `/assign`，取消 = POST `/unassign`）
- 无 diff 逻辑，与现有 subscriptions 页 endpoint 绑定风格一致

**Adopt 孤儿**：
- 订阅页孤儿 sub 显示「设为 owner」按钮
- 点击触发同一个 modal（owner dropdown 默认空）
- 在 modal 内选 token + 「改 owner」= adopt

### 6.5 路由调用链

```python
# web/routes/tokens.py
GET /tokens
  → load_auth_config() → 渲染 tokens.html（list + form + banner）

POST /tokens/create
  → create_token(name, scopes)  # 返回明文
  → session flash set
  → Redirect 303 → /tokens

POST /tokens/revoke
  → revoke_token(name)
  → Redirect 303 → /tokens

# web/routes/subscription_ownership.py
GET /subscriptions/<platform>/<id>/ownership
  → load sub + load all tokens
  → 渲染 _token_modal.html

POST /subscriptions/<platform>/<id>/owner
  → await set_subscription_owner(platform, id, token_name)
  → Redirect 303 → /subscriptions

POST /subscriptions/<platform>/<id>/assign
  → await assign_token_to_subscription(platform, id, token_name)
  → Redirect 303 → /subscriptions

POST /subscriptions/<platform>/<id>/unassign
  → await unassign_token_from_subscription(platform, id, token_name)
  → Redirect 303 → /subscriptions
```

## 7. Session Auth 与权限

### 7.1 核心原则

Web UI session = superuser。所有 token 管理路由走现有 session auth，**不经** `require_scopes` / `get_token_ownership`（那是 API v1 路由用的 Bearer token 校验）。

### 7.2 实现方式

- 新路由全部挂在 `web/app.py` 的 `auth_guard` middleware 之后（与 `/settings`、`/endpoints` 等同层），自动获得 session 保护
- 路由内**不调用** `api/auth.py` 的 `require_scopes` / `get_token_ownership`
- 路由直接调 CLI 纯函数，相当于「admin 在 Web 上点按钮 = admin 在 SSH 里跑 CLI」

### 7.3 CSRF

现有 `csrf_guard` middleware 对 HTMX 请求（`X-Requested-With: XMLHttpRequest`）自动豁免，`hx-post` 全自动过。无需额外处理。

### 7.4 自我 revoke 风险

Web session 走 cookie 不走 Bearer token，revoke 自己用来调 API 的 token 不影响当前 Web session。无额外风险。

## 8. 错误处理

### 8.1 Toast 反馈

沿用现有 `TOAST_KEY_MAP` 模式（`web/templates/base.html`），新增 key：

| 场景 | toast key | type |
|---|---|---|
| 创建成功 | `token_created` | success |
| 创建失败（name 空/非法） | `token_name_invalid` | error |
| Revoke 成功 | `token_revoked` | success |
| Revoke 失败（name 不存在） | `token_not_found` | error |
| Assign 成功 | `token_assigned` | success |
| Assign 失败（订阅/token 不存在） | `assign_failed` | error |
| Set owner 成功 | `owner_set` | success |
| Set owner 失败（订阅/token 不存在） | `owner_failed` | error |

### 8.2 校验逻辑

CLI 纯函数错误行为已核实（implementation plan 前置探索确认）：

| 函数 | 错误行为 | 路由处理 |
|---|---|---|
| `create_token` | 同名 token **覆盖**（先删后加），总是成功 | 路由层校验 name 非空 |
| `revoke_token` | name 不存在返回 `False` | 路由检查 bool → 映射 toast |
| `assign_token_to_subscription` | 返回 `tuple[bool, str]` | 路由检查 `[0]` → 映射 toast |
| `unassign_token_from_subscription` | 返回 `tuple[bool, str]` | 路由检查 `[0]` → 映射 toast |
| `set_subscription_owner` | 返回 `tuple[bool, str]` | 路由检查 `[0]` → 映射 toast |

**所有纯函数不抛异常**，唯一异常可能来自 `save_auth_config` / 文件 I/O（视为 500 系统错误，不映射 toast）。

## 9. 测试策略

| 层 | 测试内容 | 方式 |
|---|---|---|
| **路由层** | GET `/tokens` 返回列表、POST `/tokens/create` 成功创建、POST `/tokens/revoke` 成功 revoke、redirect 目标正确 | FastAPI TestClient，mock session |
| **路由层** | ownership modal GET 返回正确 token list、assign/unassign/set_owner POST 正确调纯函数 | TestClient |
| **模板层** | inline banner 在 session flash 存在时显示、孤儿 badge 显示「孤儿」、scope checkbox 7 个 | Jinja2 render 单测 |
| **纯函数** | **不重复测** — CLI 测试已覆盖 | 复用 `tests/test_token_tool.py` 等 |

**测试文件**：
- `tests/test_web_tokens.py`
- `tests/test_web_subscription_ownership.py`

**Fixture 复用**：现有 `tests/conftest.py` 里 Web app fixture + auth.toml temp fixture（#108 测试改造时已建立）。

## 10. 复用清单

| 要做的 | 复用什么 | 核心文件 |
|---|---|---|
| Token 列表页 | `api/token_tool.py` 的 `list_cmd` 逻辑，`endpoints.html` list 模式 | `web/auth.py:74` `load_auth_config()` |
| Token 创建 | `api/auth.py:create_token()` 纯函数 | `api/auth.py:219` |
| Token 撤销 | `api/auth.py:revoke_token()` 纯函数 | `api/auth.py:248` |
| 显示 sub owner/assigned | Web 路由是 session=superuser 等价，可直接取 `owner_token`/`assigned_tokens` | `web/routes/subscriptions.py` |
| assign/unassign UI | `core/subscription_cli.py` 的纯函数 | `core/subscription_cli.py:538, 600, 653` |
| HTML 表单模板 | `endpoints.html` / `account.html` / `settings.html` / `_macros.html` | `web/templates/` |
| CSRF 保护 | 已有 `csrf_guard` middleware | `web/app.py:175` |
| Toast 反馈 | `TOAST_KEY_MAP` 已有模式 | `web/templates/base.html:128-140` |
| Sidebar 导航项 | `base.html` 已有 8 个 nav item | `web/templates/base.html` |
| Session auth | 已有 SessionMiddleware + auth_guard | `web/app.py`, `web/auth.py` |

## 11. 验收清单

- [ ] Token 列表页能看到所有 token（不含明文）
- [ ] 创建 token 表单（name + 7 scope checkbox）
- [ ] 创建后明文一次性显示（session flash）+ 复制按钮
- [ ] Revoke 按钮带 confirm
- [ ] Sub 详情页显示 owner + assigned，可改
- [ ] Adopt 孤儿 sub 一键按钮
- [ ] 所有操作走 admin session，不经 API ownership 层
- [ ] Sidebar 加「API Token」nav item
- [ ] Toast 反馈所有成功/失败场景
- [ ] `tests/test_web_tokens.py` + `tests/test_web_subscription_ownership.py` 通过
- [ ] `uv run ruff check .` + `uv run pyright` + `uv run pytest -x` 全绿

## 12. 关联

- **#108 / #109** — 多租户 ownership 模型（本 issue 前置依赖，已合入）
- **#103 / #105** — L1 token scope（已合入）
- **#106 / #107** — L2 resource_rules（已废弃）
- spec §3 非目标 + §11 风险表「Web UI token 管理」
- `docs/superpowers/specs/2026-07-08-multi-tenant-ownership-design.md` §13 未来工作明确列出本 issue
