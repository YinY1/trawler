# API Token 分级权限（Scopes）设计

- 日期: 2026-07-07
- 状态: Draft (待用户 review)
- 范围: `shared/config.py`、`api/auth.py`、`api/routes/{check,messages,subscriptions}.py`、`api/token_tool.py`、`web/auth.py`、`tests/test_api_*.py`
- 关联 issue: #103

## 1. 背景

trawler 当前的 HTTP API 鉴权是「全或无」语义：任何持有合法 token 的调用方都能
访问全部 13 个受保护路由（包括触发全量 check、写订阅、rerun 消息等高危操作）。
token 一旦泄露给 bot，bot 就拥有和管理员同等的 API 能力。

实际部署中，不同 bot / 集成方只需要部分能力：

- 通知 bot 只需要 `messages:read` 拉取新消息
- 触发 bot 只需要 `check:run` 启动检查
- 订阅同步 bot 需要 `subscriptions:read` + `subscriptions:write`

issue #103 要求引入「分级权限 / scope」机制，让管理员能为每个 token 单独指定
可访问的资源范围，最小化 token 泄露的爆炸半径。本 spec 锁定设计决策，给出实现
蓝图，并明确非目标（Web UI token 管理、`tokens:manage` HTTP endpoint 等）。

## 2. 目标

1. 给每个 token 关联一组 scope（字符串列表），表示该 token 可访问的资源 + 操作。
2. 在 FastAPI 路由层强制校验：token 缺少必需 scope → 403。
3. 向后兼容：已存在的无 scope token 行为不变（视为拥有全部 scope）。
4. CLI 扩展：`token create` 支持显式指定 scope 列表，`token list` 显示 scope。
5. scope 体系与 OpenAPI docs 集成（FastAPI `Security` + `SecurityScopes` 自带）。

## 3. 非目标

- **不做 Web UI token 管理**（issue #103 后续单独追踪）。本 PR 不动
  `web/templates/` 与 `web/routes/`，只动 API / CLI / 数据层。
- **不建 `tokens:manage` 的 HTTP endpoint**。token 生命周期管理走本地 CLI
  （`api/token_tool.py`），CLI 等价于「文件系统权限 = 鉴权」，不暴露给网络调用方。
  本 PR 只在代码里**定义** `tokens:manage` 常量占位，不在任何路由上消费它。
- **不引入 admin / `*` 通配 scope**。理由见 §4.4。
- **不强制 rotate 老 token**。空 scope = 全权限是过渡期默认行为，管理员需主动用
  `--scope` 重建 token 才能限制权限（spec §5）。
- **不改 `Authorization: Bearer` header 协议**，scope 元数据不进 HTTP 头，只在
  服务端读 token 时按 hash 查 `auth.toml`。
- **不改密码 / session 鉴权**。本 PR 只影响 API token 鉴权链路。

## 4. Scope 体系

### 4.1 Scope 命名规范

- 格式：`<resource>:<action>`，全小写，单数资源名
- 与 **GitHub fine-grained PAT** 命名一致（参考 `contents:read` / `contents:write`），
  让运维直观理解
- 不用 `read_only` / `admin` 这类扁平形容词 —— resource 维度天然分组，便于
  「只给 messages 写权限不给 subscriptions」这类细粒度授权

### 4.2 Scope 清单（6 个消费 + 1 个占位）

| Scope | 隐含 | 作用 |
|-------|------|------|
| `subscriptions:read` | — | 列订阅 |
| `subscriptions:write` | `subscriptions:read` | 增 / 删订阅、绑定/解绑 endpoint |
| `messages:read` | — | 列消息 / 取单条 |
| `messages:write` | `messages:read` | rerun / fetch 消息 |
| `check:read` | — | 查 run 状态 / SSE 日志流 |
| `check:run` | — | 触发 run（与 `check:read` 正交，不互相隐含） |
| `tokens:manage` | — | **占位**，本 PR 不消费（spec §3、§8） |

**共 7 个常量**，本 PR 在路由层实际校验前 6 个。

### 4.3 write → read 隐含规则

「能写就能读」是天然语义（写操作前几乎总要先看一下状态）。实现层通过
`scope_implies(granted, required)` 工具函数统一处理，路由只声明自己需要的
**最小** scope（写路由声明 `:write`，框架自动放过持有 `:write` 的 token）。

| 路由声明的 scope | token 持有 | 放行？ |
|-----------------|-----------|-------|
| `messages:read` | `messages:read` | ✓ |
| `messages:read` | `messages:write` | ✓（write 隐含 read） |
| `messages:write` | `messages:write` | ✓ |
| `messages:write` | `messages:read` | ✗（read 不隐含 write） |
| `messages:read` | `[]`（空） | ✓（空 = 全权限，spec §5） |

### 4.4 `check:run` 与 `check:read` 正交

`check:run` 是「触发」动作（POST `/check/run`，副作用大），`check:read` 是
「查看」动作（GET `/check/status` / `/check/stream`，只读）。两者**不互相隐含**：

- 给 bot 只触发不查看：仅给 `check:run`，它没法 SSE 监听
- 给 dashboard 只查看不触发：仅给 `check:read`，它没法启动 run

这种正交与 `messages:write` → `messages:read` 的「写隐含读」语义不同 ——
触发 run 并不需要先查看状态，所以正交更符合最小权限原则。

### 4.5 不引入 admin / `*` 通配的理由

| 方案 | 问题 |
|------|------|
| `*` 通配 | token 持有者 = 等价管理员，分级毫无意义 |
| `admin` scope | 同上，且容易演变成「懒得细分就给 admin」的反模式 |
| 空 = 全权限（本 spec 选择） | 仅作为**向后兼容过渡期**机制，不鼓励新建；CLI 不带 `--scope` 时显式提示「无限制」 |

如果未来真有「管理员 token」需求，挂新 issue 重新讨论（届时再考虑引入 `tokens:manage`
消费层 + HTTP endpoint）。

## 5. 向后兼容策略

### 5.1 核心规则

**`token.scopes == []`（空 list）→ 视为拥有全部 scope（6 个消费 scope 全放行）。**

- 仅运行时处理，**不写回** `auth.toml`（避免污染数据 / 让老 token 在重新部署后
  反而被限制）
- 管理员需主动用 CLI 重建 token 才能限制权限（`token create <name> --scope xxx --force`
  覆盖同名老 token）
- 老的 `data/auth.toml` 条目（无 `scopes` 字段）直接兼容 —— 缺字段 = 默认 `[]`
  = 全权限

### 5.2 为什么不在加载时补全

补全（即把 `[]` 在加载时展开成 6 个 scope 列表）有两个问题：

1. 写回时会污染数据（让管理员误以为老 token 真的有 6 个显式 scope）
2. 后续加新 scope 时，老的「全权限」token 反而被冻结成显式 6 个，反而失去全权限

因此保持「空 = 全权限」的运行时语义，永不写回。

### 5.3 CLI 行为

- `token create <name>`（不带 `--scope`）→ 创建**空 scope** token = 全权限。
  输出明确提示「无限制（建议加 --scope 收紧）」。
- `token create <name> --scope messages:read --scope check:run` → 显式多 flag，
  创建受限 token。
- `token list` → 显示 scope 列表，空则显示「(unrestricted)」。

## 6. FastAPI 实现选型

### 6.1 用 `Security` + `SecurityScopes`，不用 `Depends`

```python
from fastapi import Security, SecurityScopes

async def require_scopes(
    security_scopes: SecurityScopes,
    request: Request,
) -> str:
    """FastAPI 依赖：校验 Bearer token + 所需 scope。

    security_scopes.scopes 由路由层 Security(..., scopes=[...]) 注入。
    """
    ...

@router.get("/messages")
async def list_messages(
    _token: str = Security(require_scopes, scopes=["messages:read"]),
):
    ...
```

### 6.2 为什么选 Security

| 维度 | `Depends` | `Security` |
|------|-----------|-----------|
| 路由声明 scope | 不支持，得手动加参数 | 原生 `scopes=[...]` 参数 |
| OpenAPI docs | 不显示 scope 信息 | 自动生成 `security` 字段（OAuth2 风格 scope 数组） |
| scope 自动注入 | 手动读 request | `SecurityScopes` 自动注入 |
| 与现有 `require_token` 共存 | 得改依赖签名 | 平滑替换，签名兼容 |

OpenAPI docs 受益对前端 / 文档生成工具直接可用（redoc / swagger UI 会渲染
scope 列表），无需额外维护文档。

### 6.3 HTTP 错误码语义

| 场景 | 状态码 | 响应体 |
|------|-------|-------|
| 无 Authorization header / 格式错 / token 不匹配 | 401 | `{"detail": "invalid or missing token"}` |
| token 合法但缺必需 scope | 403 | `{"detail": "insufficient scope: requires xxx"}` |

401 vs 403 的语义对齐 RFC 7235：401 = 身份未知，403 = 身份已知但权限不够。
现有 `test_*_no_token_returns_401` 全部沿用，新增 `test_*_insufficient_scope_returns_403`
覆盖 403 分支。

### 6.4 `require_scopes` 函数契约

```python
async def require_scopes(
    security_scopes: SecurityScopes,
    request: Request,
) -> str:
    """FastAPI 依赖：校验 Bearer + scope。

    - 无 header / 格式错 / token 不匹配 → 401（与 require_token 一致）
    - token 合法但缺 scope → 403
    - 通过 → 返回 token name（供日志/审计）

    空 token.scopes = 全权限（spec §5），与 security_scopes.scopes 为空（路由
    不要求 scope）正交处理。
    """
```

**关键点**：`Security(require_scopes)` 不带 `scopes=[...]` 时，
`security_scopes.scopes == ()`，行为等价于 `require_token`（只校验身份，不校验
scope）。这保证了「路由不要求 scope」的兼容路径。

## 7. 数据模型变更

### 7.1 `ApiTokenEntry` 加 `scopes` 字段

```python
# shared/config.py:248
@dataclass
class ApiTokenEntry:
    """API token 条目（``data/auth.toml`` 的 ``[[api_tokens]]`` AoT 行）。"""

    name: str
    token_hash: str
    created_at: float = 0.0
    scopes: list[str] = field(default_factory=list)  # 新增；空 = 全权限
```

### 7.2 `auth.toml` AoT 扩展

```toml
# 老格式（仍兼容，加载时 scopes 默认 []）
[[api_tokens]]
name = "bot-1"
token_hash = "abc..."
created_at = 1717500000.0

# 新格式
[[api_tokens]]
name = "notifier"
token_hash = "def..."
created_at = 1717500000.0
scopes = ["messages:read", "check:read"]
```

### 7.3 `web/auth.py` 加载/保存容错

`load_auth_config` (line 84-92) 读 `t.get("scopes", [])`；
`save_auth_config` (line 113-122) 写 `entry["scopes"] = t.scopes`（空 list 也写，
保持显式）。**注意**：空 list 在 tomlkit 中需要显式 `tomlkit.array()` 处理，
避免 `scopes = []` 被丢失。

## 8. CLI 变更

### 8.1 `token create` 加 `--scope` multi-flag

```bash
# 全权限（默认）
python -m api.token_tool create bot-1

# 显式受限
python -m api.token_tool create notifier \
    --scope messages:read \
    --scope check:read
```

Click 写法：

```python
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="限制 token scope（可多次指定）。不指定 = 全权限",
)
def create(name: str, force: bool, scopes: tuple[str, ...]) -> None:
    ...
```

**Scope 校验**：CLI 层校验 `--scope xxx` 必须是 §4.2 表里的合法值（含
`tokens:manage` 占位）。未知 scope → 报错退出（防拼写错误静默丢权限）。

### 8.2 `token list` 显示 scopes

```
API Tokens
 Name       Hash (前 8)   Created At          Scopes
 bot-1      a1b2c3d4      2024-06-10 12:00   (unrestricted)
 notifier   e5f6g7h8      2024-06-10 13:00   messages:read, check:read
```

### 8.3 CLI 不受 scope 约束

`api/token_tool.py` 是本地管理员 CLI（直接读写 `data/auth.toml`），**不**通过
HTTP、不**通过** `require_scopes`。理由：

- 「能跑这个 CLI = 能直接 `vim auth.toml` = 是管理员」，文件系统权限就是鉴权
- 引入 scope 反而让管理员被自己设的 scope 锁死，无意义
- 与 spec §3 的「不建 tokens:manage HTTP endpoint」决策一致

## 9. 影响面：13 个路由迁移

| 文件:行 | 方法 路径 | scope |
|--------|----------|-------|
| `api/routes/check.py:42` | POST `/check/run` | `check:run` |
| `api/routes/check.py:164` | GET `/check/status` | `check:read` |
| `api/routes/check.py:186` | GET `/check/stream` | `check:read` |
| `api/routes/messages.py:95` | GET `/messages` | `messages:read` |
| `api/routes/messages.py:146` | GET `/messages/{id}` | `messages:read` |
| `api/routes/messages.py:166` | POST `/messages/rerun` | `messages:write` |
| `api/routes/messages.py:271` | POST `/messages/fetch` | `messages:write` |
| `api/routes/subscriptions.py:39` | GET `/subscriptions` | `subscriptions:read` |
| `api/routes/subscriptions.py:50` | POST `/subscriptions` | `subscriptions:write` |
| `api/routes/subscriptions.py:72` | DELETE `/subscriptions/{p}/{id}` | `subscriptions:write` |
| `api/routes/subscriptions.py:94` | POST `/subscriptions/{p}/{id}/endpoints` | `subscriptions:write` |
| `api/routes/subscriptions.py:121` | DELETE `/subscriptions/{p}/{id}/endpoints/{ep}` | `subscriptions:write` |
| `api/routes/health.py:22` | GET `/health` | 无（探活，公开） |

迁移规则：把 `_token_name: str = Depends(require_token)` 换成
`_token_name: str = Security(require_scopes, scopes=["<scope>"])`。
`health` 不动（无鉴权）。

## 10. 测试策略

### 10.1 单元测试（`tests/test_api_auth.py`）

新增 `TestScopes` class：

- `scope_implies(write, read)` → True / `scope_implies(read, write)` → False
- `scope_implies(check_run, check_read)` → False（正交验证）
- `token_has_scope(token_with_empty_scopes, any)` → True（空 = 全权限）
- `token_has_scope(token_with_messages_write, messages_read)` → True（隐含）
- `token_has_scope(token_with_messages_read, messages_write)` → False
- `require_scopes` 直接调用：合法 token 缺 scope → 403；空 scope token = 全权限放行
- `create_token(name, scopes=[...])` 持久化 scopes 到 auth.toml

### 10.2 集成测试（每个路由文件）

为 13 个路由中**除 health 外的 12 个**加 `test_*_insufficient_scope_returns_403`：

```python
async def test_run_insufficient_scope_returns_403(
    self, tmp_path, monkeypatch
):
    """token 只有 messages:read，访问 /check/run → 403。"""
    # 用 create_token(name, scopes=["messages:read"]) 限制
    ...
    resp = await c.post("/api/v1/check/run", json={"mode": "full"})
    assert resp.status_code == 403
    assert "scope" in resp.json()["detail"]
```

**写路由测试模式**：用 `messages:read` token 访问 `messages:write` 路由 → 403
（验证 write 不被 read 放行）。

**check:run / check:read 正交测试**：

- `check:run` token 访问 `/check/status` → 403
- `check:read` token 访问 `/check/run` → 403

### 10.3 CLI 测试（`tests/test_api_token_tool.py`）

- `create bot --scope messages:read --scope check:run` → auth.toml 落盘 scopes
- `create bot --scope invalid` → 退出码非 0
- `list` 输出含 scopes 列（受限 token 显示，空 token 显示 `(unrestricted)`）

### 10.4 兼容性测试

- 老 auth.toml（无 scopes 字段）→ 加载后 `scopes == []` → 全权限
- 升级路径：现网老 token 不需要任何迁移动作

### 10.5 测试 fixture 复用

现有 `authed_client` fixture（`tests/test_api_check.py:32-60`）创建的 token
不带 scope（即全权限），现有所有 happy-path 测试**无需改动**继续通过 ——
这验证了「空 = 全权限」的兼容性。

新增 `scoped_client(scope_list)` fixture 辅助 403 测试：

```python
@pytest.fixture
async def scoped_client(...):
    """创建带指定 scope 的 client。"""
    plain = create_token("scoped-bot", scopes=scope_list)
    ...
```

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 老 token 全权限过渡期长，新增 scope 形同虚设 | 文档强调：建议生产环境主动 rotate；CLI create 不带 `--scope` 时输出警告 |
| `tokens:manage` 占位引起误解（开发者以为是真 scope） | 常量 docstring 明确「占位、不消费」，本 PR 不在路由层引用 |
| 空 list 在 tomlkit 中被丢失（写成空 AoT） | `web/auth.py:save_auth_config` 显式 `tomlkit.array()` 处理；单测覆盖空 scopes 落盘 |
| `Security` 替换 `Depends` 时签名漏改导致 happy path 测试失败 | TDD：先跑现有测试确认绿，再批量替换，红一改一绿 |
| OpenAPI docs 渲染异常（FastAPI 版本差异） | 集成测试加 `test_openapi_docs_include_scopes` 检查 `/openapi.json` 含 scope 字段 |
| 调用方（bot）现有逻辑假设 401 = 全部错，没处理 403 | API 已发布版本未对外，无外部 bot；通知调用方更新错误处理 |

## 12. 未来的 tokens:manage / Web UI

本 PR 不做的事，挂新 issue 单独追踪：

- **Web UI token 管理**：在 `/settings/tokens` 页面 CRUD token，可视化勾选 scope
- **`tokens:manage` HTTP endpoint**：`POST /api/v1/tokens` / `DELETE /api/v1/tokens/{name}`，
  需要 `tokens:manage` scope（即「用 token 管理 token」）。安全性要求高（避免
  token 提权），需要更详细的设计
- **token 过期时间**：`expires_at` 字段 + 后台清理任务
- **scope 审计日志**：记录每个 token 的调用历史
- **更细的资源 scope**：如 `messages:read:platform:bilibili`（按平台细分），
  当前 6 scope 够用，YAGNI

## 13. 验证清单

实现完成后必须通过：

```bash
uv run ruff check .
uv run pyright
uv run pytest -x tests/test_api_auth.py -v
uv run pytest -x tests/test_api_token_tool.py -v
uv run pytest -x tests/test_api_check.py -v
uv run pytest -x tests/test_api_messages.py -v
uv run pytest -x tests/test_api_subscriptions.py -v
uv run pytest -x tests/test_api_fetch.py -v
uv run pytest -x                                     # 全量回归
```

手动验证：

```bash
# 1. 创建全权限 token（CLI 警告「无限制」）
python -m api.token_tool create admin-bot

# 2. 创建受限 token
python -m api.token_tool create notifier \
    --scope messages:read --scope check:read

# 3. list 应显示两种 token 的 scope 差异
python -m api.token_tool list

# 4. 用受限 token 触发 run → 403
curl -X POST http://localhost:8000/api/v1/check/run \
  -H "Authorization: Bearer <notifier-token>" \
  -H "Content-Type: application/json" \
  -d '{"mode":"full"}'
# 预期 403 + {"detail": "insufficient scope: requires check:run"}

# 5. 用受限 token 拉消息 → 200
curl http://localhost:8000/api/v1/messages \
  -H "Authorization: Bearer <notifier-token>"

# 6. OpenAPI docs 含 scope 信息
curl http://localhost:8000/openapi.json | python -m json.tool | grep -A2 security
```
