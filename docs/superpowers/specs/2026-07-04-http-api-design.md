# HTTP API 改造设计

**Date**: 2026-07-04
**Status**: Approved (autonomous — user waived approval gate per active goal)
**Branch**: `feat/http-api`

## 背景与动机

Trawler 当前有三条触发链路：

1. **CLI** (`run_check.py`) — cron / 手动 shell 调用，最终走 `core.pipeline.run_check_once` 与 `core.engine.PipelineEngine`。
2. **Web UI** (`web/`, FastAPI + Jinja2) — 浏览器交互，HTML 响应、form-encoded POST、Session cookie + CSRF。
3. **Push handlers** — 在 pipeline 内部 fan-out 到 gotify/telegram。

外部 bot 想要"手动触发一次检查 / 手动重跑某条消息 / 查询订阅"，但现有 Web 路由对 bot **不可用**：

- 响应是 HTML / 303 Redirect，不是 JSON
- 写操作要求 Session cookie + CSRF token（HTMX 头或同源 referer）
- `auth_guard` 中间件把未登录请求重定向到 `/login` HTML 页

**目标**：提供机器友好的 HTTP API，让 bot 能用一行 `curl`（带 token）触发核心操作，**不破坏** Web UI / CLI / cron 现有行为。

## 设计原则

1. **零重复** — API 路由薄，全部业务复用 `core/*` 与 `shared/*` 既有函数。
2. **同进程同端口** — 挂在现有 FastAPI app 的 `/api/v1` 前缀下，不新增进程/端口/部署复杂度。
3. **独立鉴权** — Token-based（`Authorization: Bearer ...`），与浏览器 session 完全隔离，CSRF 中间件豁免 `/api/*`。
4. **JSON in / JSON out** — Pydantic 模型约束请求/响应，统一错误格式。
5. **异步非阻塞** — 长任务（check run）后台 `asyncio.create_task`，立即返回 task id；状态轮询 + 可选 SSE 复用现有 `state.log_history`。
6. **不破坏兼容** — 现有 Web 路由、CLI、cron 一行不动。

## 三种备选方案

### A. 独立 FastAPI app + 独立端口（❌ 不推荐）

新建 `api_app.py`，独立 uvicorn 进程，独立端口（如 8081）。

- ✅ 鉴权/中间件完全隔离，零交叉风险
- ❌ 新进程、新端口、新部署/监控条目，docker-compose/watchtower 都要改
- ❌ 与 Web UI 共享 `app.state`（check_running 锁、SSE 订阅者）需跨进程同步，复杂度爆炸
- ❌ 违反"不能不考虑代码重用性"

### B. 同 app 同端口 + `/api/v1` 前缀（✅ **推荐**）

在 `web/app.py:create_app()` 里 `include_router(api_router, prefix="/api/v1")`。新建 `api/` 包，与 `web/` 平级。

- ✅ 复用 `app.state.check_running` 单锁（与 Web UI 互斥，避免并发跑两份 detector）
- ✅ 复用 `app.state.log_history` / `state.subscribers`（API 触发的 run 也走同一份 SSE 日志流）
- ✅ 中间件豁免 `/api/*`：`auth_guard`、`csrf_guard` 都跳过，改用 `api.auth.require_token` 依赖
- ✅ 部署零变化 — watchtower 拉同镜像，同端口
- ⚠️ 需要谨慎处理中间件顺序（见下"中间件豁免"）

### C. 把 Web 路由全改成 JSON + 双模式渲染（❌ 不推荐）

让 `/check/run` 同时支持 form 和 JSON，鉴权同时支持 cookie 和 token。

- ✅ 路由最少
- ❌ 每个路由都要 if/else 分支判断"是浏览器还是 bot"，可读性崩坏
- ❌ CSRF 中间件难以精确判断何时豁免
- ❌ 违反"边界清晰"

**结论：采用 B。**

## 架构

```
web/app.py (create_app)
  ├── 现有中间件链（SessionMiddleware → auth_guard → csrf_guard）
  │     ↑ /api/* 在 auth_guard 和 csrf_guard 里都被豁免
  ├── 现有 web 路由（HTML）
  └── 新增: api.v1.router  (挂载 prefix="/api/v1")
        ├── api/auth.py         — token 鉴权依赖
        ├── api/schemas.py      — Pydantic 请求/响应模型
        ├── api/routes/
        │     ├── check.py      — POST /check/run, GET /check/status, GET /check/stream
        │     ├── messages.py   — GET /messages, GET /messages/{id}, POST /messages/rerun
        │     ├── subscriptions.py — GET/POST/DELETE /subscriptions
        │     └── health.py     — GET /health
        └── api/state.py        — 复用 web app.state 的薄封装（避免重复字段名拼写错误）
```

### 鉴权：API Token

- **存储**：`data/auth.toml` 新增 `api_tokens` AoT（与 `admin_password_hash` 同文件，便于和 Web UI 在同一 setup 流程管理）。每条 token：`{name, token_hash, created_at}`。
  - 不存明文；存 SHA-256 hash。bot 侧只持明文，服务端 hash 后比对。
  - 复用 `argon2` 不合适（每次校验 ~50ms，bot 高频调用不划算）；token 是高熵随机串，SHA-256 + 常量时间比对足够。
- **生成**：Web UI `/settings` 页加"API Tokens"管理面板（create / revoke）。首版可以 CLI 工具 `python -m api.token_tool create <name>` 生成，UI 后续迭代。
- **校验**：FastAPI Dependency `require_token()`，从 `Authorization: Bearer <token>` 提取，hash 后查表。
  - 失败 → 401 `{"detail": "invalid or missing token"}`
- **豁免 CSRF**：`csrf_guard` 和 `auth_guard` 中间件对 `path.startswith("/api/")` 直接放行（token 校验由路由依赖兜底）。

### 端点清单（v1）

| Method | Path | 作用 | 复用 |
|---|---|---|---|
| `POST` | `/api/v1/check/run` | 触发一次检查（全量 or 手动） | `run_check_once` / `PipelineEngine.run_specific_messages` |
| `GET`  | `/api/v1/check/status` | 当前 run 状态 | `app.state.check_running` 等 |
| `GET`  | `/api/v1/check/stream` | SSE 日志流（与 Web UI 同源） | `state.subscribers` |
| `GET`  | `/api/v1/messages` | 查询消息（since/title/author/platform/phase） | `MessageStore.query_messages` |
| `GET`  | `/api/v1/messages/{msg_id}` | 单条消息详情 | `MessageStore.get_message` |
| `POST` | `/api/v1/messages/rerun` | 批量手动重跑 | `PipelineEngine.run_specific_messages` |
| `GET`  | `/api/v1/subscriptions` | 列出订阅 | `list_subscriptions` |
| `POST` | `/api/v1/subscriptions` | 添加订阅 | `add_subscription` |
| `DELETE` | `/api/v1/subscriptions/{platform}/{identifier}` | 删除订阅 | `remove_subscription` |
| `GET`  | `/api/v1/health` | 健康检查（无需鉴权） | — |

### 请求/响应模型（Pydantic）

`POST /api/v1/check/run` 请求：
```json
{
  "mode": "full",            // "full" | "manual"
  "platform": "all",         // full 模式可选；默认 "all"
  "since": "24h",            // manual 模式可选，复用 parse_since
  "title": null,
  "author": null,
  "reset_phase": "summarized", // manual 模式默认
  "skip_push": true            // manual 模式默认 true（禁止重推）
}
```

响应（202 Accepted）：
```json
{
  "status": "started",
  "task_id": "<uuid>",
  "mode": "full"
}
```

若已有 run 在跑：`409 Conflict {"status": "already_running", "task_id": "..."}`。

`POST /api/v1/messages/rerun` 请求：
```json
{
  "msg_ids": ["bili:BV1...", "xhs:abc"],
  "from_phase": "summarized",
  "skip_push": true
}
```

### 中间件豁免细节

当前 `web/app.py` 的中间件顺序（add 顺序 → 执行顺序倒序）：
```
SessionMiddleware (最外) → auth_guard → csrf_guard → 路由
```

改造：
- `auth_guard`：`path.startswith("/api/")` 加入 `_PUBLIC_PREFIXES`，跳过 setup/login 重定向
- `csrf_guard`：`path.startswith("/api/")` 加入豁免（API 走 token，无 session 可盗）
- API 路由通过 `Depends(require_token)` 自己鉴权（health 路由除外）

**关键不变量**：API 触发的 check run 与 Web UI 触发的 check run **共享 `state.check_running` 锁**，两边互斥。这是有意为之 — detector 并发跑两份会产生重复消息。API 不绕过这个锁。

### 与 Web UI 的关系

- API `/check/run` 与 Web UI `POST /check/run` **等价**，只是入参/出参格式不同，且鉴权方式不同。内部都调 `run_check_once` / `run_specific_messages`，都写 `state.log_history`，都广播到 `state.subscribers`。
- 这意味着：bot 通过 API 触发 run 时，浏览器如果开着 Web UI 也会看到 SSE 日志流。这是 feature，不是 bug。

## 错误处理

- API 路由抛业务异常 → 由 `api.errors` 注册的 exception handler 统一转 JSON：
  - `ValueError`（如 parse_since 失败）→ 422 `{"detail": "..."}`
  - `KeyError`（如未知 phase）→ 422
  - 其他未捕获 → 500 `{"detail": "internal error"}`（已有 `web/app.py` 全局 handler 会先捕获，但 API 路由的 JSON 响应不会触发它）
- API 路由不返回 HTML / Redirect，全程 JSON。

## 测试策略

TDD。每条路由先写测试再写实现，文件命名 `tests/test_api_<module>.py`。

- `test_api_auth.py` — token 校验、401、豁免 health
- `test_api_check.py` — full / manual 模式、互斥锁、status、SSE（用 httpx ASGITransport）
- `test_api_messages.py` — query 过滤、rerun
- `test_api_subscriptions.py` — CRUD
- `test_api_health.py` — 无鉴权可访问
- `test_api_errors.py` — 错误格式统一

现有测试（`test_web_*`）一行不改 — 中间件豁免 `/api/*` 不影响浏览器路径。

## 配置变更

- `data/auth.toml` 新增可选 `[[api_tokens]]` AoT；不存在则 API 鉴权依赖返回 401（未配置 token = 无 API 访问）。
- `config.toml.example` 不变（token 走 auth.toml）。
- `WebAuthConfig` dataclass 新增 `api_tokens: list[ApiTokenEntry] = field(default_factory=list)`。
- `_apply_env_overrides` 不需要改（token 不通过环境变量配，必须通过 UI/CLI 管理）。

## 非目标（YAGNI）

- ❌ WebSocket（SSE 已够用，且 Web UI 已有 SSE 基础设施）
- ❌ API rate limiting（同进程 + 单锁已天然限流；首版不引入）
- ❌ OAuth / JWT（单一 admin + 长寿命 token 足够 bot 场景）
- ❌ API 版本协商 header（path 前缀 `/v1` 已足够）
- ❌ 自动重试 / webhook 回调（bot 自己轮询 `/check/status` 即可）
- ❌ 把现有 Web 路由改造成同时支持 JSON（方案 C，明确否决）

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 中间件豁免 `/api/*` 漏豁某层 → bot 收到 HTML redirect | 测试覆盖：未带 token 打 `/api/v1/check/run` 必须 401 JSON，不是 302 |
| Token 泄漏 | UI 显示 token 一次（创建时），后续只显示 hash 前 8 位；revoke 即时生效 |
| API 与 Web UI 同时触发 run | 共享 `state.check_running` 锁，第二个请求 409 |
| `parse_since` 等公共函数被两个入口调用导致行为漂移 | 不复制函数，直接 import；测试覆盖 API 与 CLI 走同一函数 |

## 实施切片（高层，详细 plan 由 @explorer 写）

1. `api/` 包骨架 + token 鉴权依赖 + 中间件豁免 + health 端点
2. `check` 路由（run / status / stream）
3. `messages` 路由（list / get / rerun）
4. `subscriptions` 路由（list / add / remove）
5. `data/auth.toml` 的 `api_tokens` 持久化 + CLI token 工具
6. 文档：`docs/api.md` + 在 README 加一节
7. 端到端验证：真实 curl 触发一次 full run + 一次 rerun

每切片独立可测、可 review，最后单 PR 合并。
