# API Token 分级权限（Scopes）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `data/auth.toml` 的 API token 加 `scopes` 字段，引入 6+1 个 scope 体系（write 隐含 read，check:run/read 正交，空 = 全权限），13 个路由迁移到 FastAPI `Security` 依赖，CLI 支持 `--scope` multi-flag。Web UI 与 `tokens:manage` HTTP endpoint 为非目标，挂新 issue。

**Architecture:** 方案 —— 数据层加字段（向后兼容）→ scope 工具函数（纯函数）→ FastAPI `require_scopes` 依赖（`Security` + `SecurityScopes`，OpenAPI 自动渲染）→ 13 路由批量替换 `Depends(require_token)` → CLI 扩展 → 测试补全。空 scopes = 全权限是运行时语义，永不写回 auth.toml。

**Tech Stack:** Python 3.14, FastAPI（`Security` / `SecurityScopes`）, Pydantic v2, tomlkit, Click, pytest (+ asyncio)。所有源文件顶部 `from __future__ import annotations`，async + await 模式。

**参考文档：** spec `docs/superpowers/specs/2026-07-07-api-token-scopes-design.md`

---

## File Structure

| 文件 | 改动类型 | 责任 |
|------|---------|------|
| `shared/config.py` | 扩展 | `ApiTokenEntry` 加 `scopes: list[str]` 字段（line 248-258） |
| `web/auth.py` | 扩展 | `load_auth_config`（line 74-98）读 `scopes`；`save_auth_config`（line 101-126）写 `scopes` AoT |
| `api/auth.py` | 扩展 | 加 `SCOPES` 常量、`scope_implies` / `token_has_scope` 工具；新增 `require_scopes` 依赖；扩展 `create_token` 接受 `scopes` 参数 |
| `api/routes/check.py` | 替换 | 3 个路由 `Depends(require_token)` → `Security(require_scopes, scopes=[...])` |
| `api/routes/messages.py` | 替换 | 4 个路由同上 |
| `api/routes/subscriptions.py` | 替换 | 5 个路由同上 |
| `api/token_tool.py` | 扩展 | `create` 加 `--scope` multi-flag；`list` 显示 scopes |
| `tests/test_api_auth.py` | 扩展 | `TestScopes` class + `require_scopes` 5 场景 + `create_token` scopes 持久化 |
| `tests/test_api_token_tool.py` | 扩展 | `--scope` happy/拒绝 + `list` 显示 scopes |
| `tests/test_api_check.py` | 扩展 | 3 个路由的 `test_*_insufficient_scope_returns_403` |
| `tests/test_api_messages.py` | 扩展 | 4 个路由同上 |
| `tests/test_api_subscriptions.py` | 扩展 | 5 个路由同上 |
| `tests/test_api_fetch.py` | 扩展 | 1 个路由同上（fetch 路由） |

**依赖顺序**：Task 1（数据层）→ Task 2（scope 工具）→ Task 3（FastAPI 依赖）→ Task 4（路由迁移）→ Task 5（CLI）→ Task 6（测试补全）→ Task 7（全量验证）。Task 4 必须在 Task 3 完成后；Task 6 必须在 Task 4 完成后。

---

## Task 1: 数据层 — `ApiTokenEntry` 加 scopes 字段 + auth.toml I/O 容错

**依赖：** 无（基础）。

**Files:**
- Modify: `shared/config.py:248-258`（`ApiTokenEntry` dataclass）
- Modify: `web/auth.py:74-126`（`load_auth_config` / `save_auth_config`）
- Test: `tests/test_api_auth.py`（追加 `TestScopesPersistence` class）

**关键决策：**
- `scopes: list[str] = field(default_factory=list)` —— 默认空 = 全权限（spec §5）
- `load_auth_config` 用 `t.get("scopes", [])` 兼容老 auth.toml（无字段 → 空 list → 全权限）
- `save_auth_config` 写 `entry["scopes"] = tomlkit.array(t.scopes)` —— 显式 array，
  避免空 list 被 tomlkit 丢失（spec §11 风险表）

- [ ] **Step 1.1: 写失败测试 `test_scopes_persisted_to_toml`**

追加到 `tests/test_api_auth.py`（在 `TestCreateToken` / `TestRevokeToken` 之后，
`TestLoadAuthConfig` class 之前）：

```python
class TestScopesPersistence:
    """scopes 字段持久化用例（spec §7）。"""

    def test_create_token_with_scopes_persists(
        self, auth_path: Path
    ) -> None:
        from api.auth import create_token
        from web.auth import load_auth_config

        plain = create_token(
            "scoped-bot", scopes=["messages:read", "check:read"]
        )
        assert plain  # 明文非空

        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 1
        entry = cfg.api_tokens[0]
        assert entry.name == "scoped-bot"
        assert entry.scopes == ["messages:read", "check:read"]

    def test_create_token_without_scopes_defaults_empty(
        self, auth_path: Path
    ) -> None:
        from api.auth import create_token
        from web.auth import load_auth_config

        create_token("legacy-bot")  # 不传 scopes
        cfg = load_auth_config()
        assert cfg.api_tokens[0].scopes == []

    def test_old_auth_toml_without_scopes_loads_as_empty(self, auth_path: Path) -> None:
        """手写老格式 auth.toml（无 scopes 字段）→ 加载后 scopes == []。

        验证向后兼容（spec §5.1）。
        """
        # 手写一条无 scopes 字段的 token
        auth_path.write_text(
            '[[api_tokens]]\n'
            'name = "legacy"\n'
            f'token_hash = "{"a" * 64}"\n'
            "created_at = 1717500000.0\n",
            encoding="utf-8",
        )
        from web.auth import load_auth_config

        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 1
        assert cfg.api_tokens[0].scopes == []

    def test_empty_scopes_round_trip_through_toml(self, auth_path: Path) -> None:
        """scopes == [] 的 token 写盘再读回仍是 []（不被 tomlkit 丢失）。"""
        from api.auth import create_token
        from web.auth import load_auth_config

        create_token("bot", scopes=[])  # 显式空
        cfg = load_auth_config()  # 重新读
        assert cfg.api_tokens[0].scopes == []
```

- [ ] **Step 1.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_api_auth.py::TestScopesPersistence -v
```

预期：`TypeError: create_token() got an unexpected keyword argument 'scopes'`。

- [ ] **Step 1.3: 实现 `ApiTokenEntry.scopes` 字段**

修改 `shared/config.py:248-258`：

```python
@dataclass
class ApiTokenEntry:
    """API token 条目（``data/auth.toml`` 的 ``[[api_tokens]]`` AoT 行）。

    bot 友好的 HTTP API 鉴权用（``api/`` 包），存 SHA-256 hash 不存明文。
    与 ``EndpointConfig`` 同风格：所有字段无 default，dataclass 字段顺序灵活。

    ``scopes`` 为空 list 表示拥有全部 scope（向后兼容老 token，spec §5）。
    非 list 表示受限 —— 路由层通过 ``api.auth.require_scopes`` 强制校验。
    """

    name: str
    token_hash: str  # SHA-256 hexdigest
    created_at: float = 0.0  # unix ts；默认 0.0 允许老数据/手工编辑兼容
    scopes: list[str] = field(default_factory=list)
```

**注意：** `field` 已在 `shared/config.py` 顶部 import（line 5 区附近，dataclass
其他字段已用），不用新增 import。

- [ ] **Step 1.4: 实现 `web/auth.py` 读写 scopes**

修改 `web/auth.py:84-92`（`load_auth_config` 的列表构造）：

```python
api_tokens: list[ApiTokenEntry] = [
    ApiTokenEntry(
        name=t["name"],
        token_hash=t["token_hash"],
        created_at=t.get("created_at", 0.0),
        scopes=list(t.get("scopes", [])),
    )
    for t in api_tokens_raw
    if isinstance(t, dict) and "name" in t and "token_hash" in t
]
```

修改 `web/auth.py:113-118`（`save_auth_config` 的 token table 构造）：

```python
for t in cfg.api_tokens:
    entry = tomlkit.table()
    entry["name"] = t.name
    entry["token_hash"] = t.token_hash
    entry["created_at"] = t.created_at
    # tomlkit: 显式 array，避免空 list 被丢失
    scopes_arr = tomlkit.array()
    for s in t.scopes:
        scopes_arr.append(s)
    entry["scopes"] = scopes_arr
    tokens_aot.append(entry)
```

**为什么 `tomlkit.array()` 不用 inline 写法**：tomlkit 的 `entry["scopes"] = []`
在 dump 时会变成空 inline table 或被丢，显式 array 走 AoT 风格稳。

- [ ] **Step 1.5: 实现 `api/auth.py:create_token` 接受 scopes**

修改 `api/auth.py:59-75`：

```python
def create_token(
    name: str,
    scopes: list[str] | None = None,
    auth_path: Path = AUTH_TOML_PATH,
) -> str:
    """生成新 token，hash 后存 ``data/auth.toml``，返回明文（仅此一次）。

    同名 token 覆盖（先删后加），保证唯一性。
    ``scopes`` 为 None 或空 list → 空 list 落盘（= 全权限，spec §5）。
    ``auth_path`` 参数供测试 monkeypatch。
    """
    plain = secrets.token_urlsafe(32)
    cfg = load_auth_config()
    cfg.api_tokens = [t for t in cfg.api_tokens if t.name != name]
    cfg.api_tokens.append(
        ApiTokenEntry(
            name=name,
            token_hash=_hash_token(plain),
            created_at=datetime.now(timezone.utc).timestamp(),
            scopes=list(scopes) if scopes else [],
        )
    )
    save_auth_config(cfg)
    return plain
```

- [ ] **Step 1.6: 运行测试确认通过**

```bash
uv run pytest -x tests/test_api_auth.py::TestScopesPersistence -v
```

预期：4 个测试 PASS。

**回归检查**：

```bash
uv run pytest -x tests/test_api_auth.py tests/test_api_token_tool.py -v
```

预期：所有原测试（`TestHashVerify` / `TestRequireToken` / `TestCreateToken` /
`TestRevokeToken` / `TestLoadAuthConfig` + CLI 测试）继续 PASS，验证 `scopes`
新增字段对老代码透明。

- [ ] **Step 1.7: 提交**

```bash
git add shared/config.py web/auth.py api/auth.py tests/test_api_auth.py
git commit -m "feat(api): ApiTokenEntry add scopes field with backward-compatible toml I/O

- shared/config.py: ApiTokenEntry.scopes: list[str] (default [])
- web/auth.py: load/save_auth_config handle scopes (tomlkit.array for empty)
- api/auth.py: create_token accepts scopes kwarg
- empty scopes == [] means full access (spec §5), runtime only, never write back"
```

**工作量：** 15 min

---

## Task 2: scope 常量与工具函数

**依赖：** Task 1（用 `ApiTokenEntry` 做 `token_has_scope` 工具入参）。

**Files:**
- Modify: `api/auth.py`（顶部加 scope 常量段，加 2 个工具函数）
- Test: `tests/test_api_auth.py`（追加 `TestScopeUtils` class）

**关键决策：**
- 常量集中定义在 `api/auth.py` 顶部，避免散落（不新建 `api/scopes.py` 文件，YAGNI）
- `scope_implies` / `token_has_scope` 是**纯函数**（无 IO，不读 auth.toml），
  方便单测覆盖
- `tokens:manage` 占位常量**必须**定义，docstring 明确「不消费」（spec §3 / §8）

- [ ] **Step 2.1: 写失败测试**

追加到 `tests/test_api_auth.py`（`TestScopesPersistence` 之后）：

```python
class TestScopeUtils:
    """scope_implies / token_has_scope 纯函数用例（spec §4.3 / §4.4）。"""

    def test_scope_implies_write_implies_read(self) -> None:
        from api.auth import scope_implies

        assert scope_implies("messages:write", "messages:read") is True
        assert scope_implies("subscriptions:write", "subscriptions:read") is True

    def test_scope_implies_read_does_not_imply_write(self) -> None:
        from api.auth import scope_implies

        assert scope_implies("messages:read", "messages:write") is False

    def test_scope_implies_check_run_read_orthogonal(self) -> None:
        """check:run 与 check:read 正交（spec §4.4）。"""
        from api.auth import scope_implies

        assert scope_implies("check:run", "check:read") is False
        assert scope_implies("check:read", "check:run") is False

    def test_scope_implies_different_resources(self) -> None:
        from api.auth import scope_implies

        assert scope_implies("messages:write", "subscriptions:read") is False
        assert scope_implies("messages:read", "messages:read") is True

    def test_token_has_scope_empty_scopes_means_full(self) -> None:
        """token.scopes == [] → 任何 required 都放行（spec §5）。"""
        from api.auth import ApiTokenEntry, token_has_scope

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, "messages:read") is True
        assert token_has_scope(token, "check:run") is True
        assert token_has_scope(token, "subscriptions:write") is True

    def test_token_has_scope_explicit_grant(self) -> None:
        from api.auth import ApiTokenEntry, token_has_scope

        token = ApiTokenEntry(
            name="x", token_hash="h", scopes=["messages:read"]
        )
        assert token_has_scope(token, "messages:read") is True

    def test_token_has_scope_write_grants_read(self) -> None:
        from api.auth import ApiTokenEntry, token_has_scope

        token = ApiTokenEntry(
            name="x", token_hash="h", scopes=["messages:write"]
        )
        assert token_has_scope(token, "messages:read") is True  # 隐含
        assert token_has_scope(token, "messages:write") is True

    def test_token_has_scope_insufficient(self) -> None:
        from api.auth import ApiTokenEntry, token_has_scope

        token = ApiTokenEntry(
            name="x", token_hash="h", scopes=["messages:read"]
        )
        assert token_has_scope(token, "messages:write") is False
        assert token_has_scope(token, "check:run") is False
```

**注意：** `ApiTokenEntry` 直接从 `api.auth` import 是为了测试简洁；实际它从
`shared.config` re-import 进 `api/auth`（Task 2 Step 2.3 一并暴露）。

- [ ] **Step 2.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_api_auth.py::TestScopeUtils -v
```

预期：`ImportError: cannot import name 'scope_implies' from 'api.auth'`。

- [ ] **Step 2.3: 实现常量 + 工具函数**

在 `api/auth.py` 顶部（`from shared.config import ApiTokenEntry` 之后、
`logger = ...` 之前）插入：

```python
# ═══════════════════════════════════════════════════════════
# Token scopes（spec §4）
# ═══════════════════════════════════════════════════════════

#: Scope 常量。命名规范 ``<resource>:<action>``，全小写单数资源名。
#:
#: 消费 scope（6 个，路由层校验）：
SCOPE_SUBSCRIPTIONS_READ = "subscriptions:read"
SCOPE_SUBSCRIPTIONS_WRITE = "subscriptions:write"
SCOPE_MESSAGES_READ = "messages:read"
SCOPE_MESSAGES_WRITE = "messages:write"
SCOPE_CHECK_READ = "check:read"
SCOPE_CHECK_RUN = "check:run"
#:
#: 占位 scope（spec §3、§12）。**本 PR 不在路由层消费**，
#: 仅供未来 ``tokens:manage`` HTTP endpoint 或 CLI 校验白名单引用。
SCOPE_TOKENS_MANAGE = "tokens:manage"

#: 所有合法 scope（CLI ``--scope`` 白名单校验用）。包含 tokens:manage 占位。
ALL_SCOPES: tuple[str, ...] = (
    SCOPE_SUBSCRIPTIONS_READ,
    SCOPE_SUBSCRIPTIONS_WRITE,
    SCOPE_MESSAGES_READ,
    SCOPE_MESSAGES_WRITE,
    SCOPE_CHECK_READ,
    SCOPE_CHECK_RUN,
    SCOPE_TOKENS_MANAGE,
)

#: write → read 隐含规则映射（spec §4.3）。check:run / check:read 正交，不在此表。
_WRITE_IMPLIES_READ: dict[str, str] = {
    SCOPE_SUBSCRIPTIONS_WRITE: SCOPE_SUBSCRIPTIONS_READ,
    SCOPE_MESSAGES_WRITE: SCOPE_MESSAGES_READ,
}


def scope_implies(granted: str, required: str) -> bool:
    """判断 granted scope 是否满足 required（含 write→read 隐含）。

    - ``granted == required`` → True
    - granted 是某 resource 的 write，required 是同 resource read → True
    - 其余（不同 resource、read→write、check:run↔check:read）→ False
    """
    if granted == required:
        return True
    return _WRITE_IMPLIES_READ.get(granted) == required


def token_has_scope(token: ApiTokenEntry, required: str) -> bool:
    """token 是否满足 required scope。

    空 scopes（``[]``）= 全权限，永远返回 True（spec §5，仅运行时）。
    非 list 遍历 granted scope，调 ``scope_implies`` 判断。
    """
    if not token.scopes:
        return True
    return any(scope_implies(g, required) for g in token.scopes)
```

**re-export `ApiTokenEntry`**：在 `api/auth.py` 顶部已有
`from shared.config import ApiTokenEntry`（line 23），测试就能 `from api.auth import ApiTokenEntry`
直接用。

- [ ] **Step 2.4: 运行测试确认通过**

```bash
uv run pytest -x tests/test_api_auth.py::TestScopeUtils -v
```

预期：8 个测试 PASS。

- [ ] **Step 2.5: 提交**

```bash
git add api/auth.py tests/test_api_auth.py
git commit -m "feat(api): scope constants + scope_implies/token_has_scope helpers

- 7 scope constants (6 consumed + tokens:manage placeholder)
- scope_implies handles write→read implication
- token_has_scope: empty scopes = full access (runtime only)
- check:run / check:read orthogonal by design"
```

**工作量：** 10 min

---

## Task 3: FastAPI 依赖 `require_scopes`

**依赖：** Task 1 + Task 2。

**Files:**
- Modify: `api/auth.py`（在 `require_token` 之后加 `require_scopes`）
- Test: `tests/test_api_auth.py`（追加 `TestRequireScopes` class）

**关键决策：**
- `require_scopes` 用 `Security` 注入 `SecurityScopes`，不用 `Depends`
- `security_scopes.scopes == ()` 时（路由不要求 scope）行为等价于 `require_token`
- 401（无 token）与 403（缺 scope）严格分离，错误 detail 文本含 "scope" 关键字便于测试
- `require_token` **保留不删**（向后兼容，未来若需「不要求 scope」路由继续用）

- [ ] **Step 3.1: 写失败测试**

追加到 `tests/test_api_auth.py`（`TestScopeUtils` 之后）：

```python
class TestRequireScopes:
    """require_scopes FastAPI 依赖用例（spec §6）。

    通过 FastAPI TestClient 或直接 async 调测试。直接 async 调更轻量，
    与现有 TestRequireToken 风格一致。
    """

    @pytest.fixture
    def token_entry(self, auth_path: Path) -> tuple[str, ApiTokenEntry]:
        """返回 (明文 token, ApiTokenEntry)。"""
        from api.auth import ApiTokenEntry, create_token

        plain = create_token("scoped", scopes=["messages:read"])
        return plain, ApiTokenEntry(
            name="scoped",
            token_hash="",  # 不用，require_scopes 内部读 auth.toml
            scopes=["messages:read"],
        )

    def _make_request(self, token: str | None) -> SimpleNamespace:
        headers = {}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        return SimpleNamespace(headers=headers)

    async def test_no_header_returns_401(self, auth_path: Path) -> None:
        from fastapi import Security, SecurityScopes

        from api.auth import require_scopes

        security = SecurityScopes(scopes=["messages:read"])
        request = self._make_request(None)
        with pytest.raises(HTTPException) as exc:
            await require_scopes(security, request)
        assert exc.value.status_code == 401
        assert "token" in exc.value.detail

    async def test_invalid_token_returns_401(
        self, auth_path: Path
    ) -> None:
        from fastapi import SecurityScopes

        from api.auth import require_scopes

        security = SecurityScopes(scopes=["messages:read"])
        request = self._make_request("not-a-real-token")
        with pytest.raises(HTTPException) as exc:
            await require_scopes(security, request)
        assert exc.value.status_code == 401

    async def test_insufficient_scope_returns_403(
        self, auth_path: Path
    ) -> None:
        """token 有 messages:read，访问 messages:write → 403。"""
        from fastapi import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("limited", scopes=["messages:read"])
        security = SecurityScopes(scopes=["messages:write"])
        request = self._make_request(plain)
        with pytest.raises(HTTPException) as exc:
            await require_scopes(security, request)
        assert exc.value.status_code == 403
        assert "scope" in exc.value.detail.lower()

    async def test_sufficient_scope_passes(
        self, auth_path: Path
    ) -> None:
        """token 有 messages:write，访问 messages:read（隐含）→ 放行返 token name。"""
        from fastapi import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("writer", scopes=["messages:write"])
        security = SecurityScopes(scopes=["messages:read"])
        request = self._make_request(plain)
        name = await require_scopes(security, request)
        assert name == "writer"

    async def test_empty_scopes_token_passes_any(
        self, auth_path: Path
    ) -> None:
        """空 scope token = 全权限，任何 required scope 都放行（spec §5）。"""
        from fastapi import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("admin-like")  # 不传 scopes → []
        for req in ["messages:read", "messages:write", "check:run",
                    "subscriptions:write"]:
            security = SecurityScopes(scopes=[req])
            request = self._make_request(plain)
            name = await require_scopes(security, request)
            assert name == "admin-like"

    async def test_empty_security_scopes_acts_like_require_token(
        self, auth_path: Path
    ) -> None:
        """路由不要求 scope（SecurityScopes.scopes == ()）→ 只校验身份。"""
        from fastapi import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("any-bot", scopes=["messages:read"])
        security = SecurityScopes(scopes=[])  # 路由不要求 scope
        request = self._make_request(plain)
        name = await require_scopes(security, request)
        assert name == "any-bot"
```

- [ ] **Step 3.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_api_auth.py::TestRequireScopes -v
```

预期：`ImportError: cannot import name 'require_scopes' from 'api.auth'`。

- [ ] **Step 3.3: 实现 `require_scopes`**

修改 `api/auth.py` import（line 21）：

```python
from fastapi import HTTPException, Request, Security, SecurityScopes
```

在 `require_token` 函数（line 56）之后插入：

```python
async def require_scopes(
    security_scopes: SecurityScopes,
    request: Request,
) -> str:
    """FastAPI 依赖：校验 ``Authorization: Bearer`` + 所需 scope（spec §6）。

    用 ``Security(require_scopes, scopes=[...])`` 挂到路由，FastAPI 自动：
    - 把 scope 列表注入 ``security_scopes.scopes``
    - 在 OpenAPI docs 渲染 security 字段（redoc / swagger UI 直接可见）

    错误码语义：
    - 无 header / 格式错 / token 不匹配 → 401（与 ``require_token`` 一致）
    - token 合法但缺 scope → 403 ``insufficient scope: requires xxx``
    - 通过 → 返回 token name（供日志/审计）

    特殊情况：
    - ``security_scopes.scopes == ()``（路由不要求 scope）→ 行为等价 ``require_token``
    - ``token.scopes == []``（空 list）= 全权限（spec §5），任何 required 都放行
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    plain = auth[len("Bearer ") :]
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if _verify_token(plain, entry.token_hash):
            # 身份通过，校验 scope
            for required in security_scopes.scopes:
                if not token_has_scope(entry, required):
                    raise HTTPException(
                        status_code=403,
                        detail=f"insufficient scope: requires {required}",
                    )
            return entry.name
    raise HTTPException(status_code=401, detail="invalid or missing token")
```

**注意：** 不删除 `require_token`（line 40-56），保留作为「不要求 scope」场景的
最简依赖；但 Task 4 的路由迁移会把所有现有路由改成 `require_scopes`，未来
`require_token` 可酌情标 deprecated 或保留共存（本 PR 不动）。

- [ ] **Step 3.4: 运行测试确认通过**

```bash
uv run pytest -x tests/test_api_auth.py::TestRequireScopes -v
```

预期：6 个测试 PASS。

**回归检查**：

```bash
uv run pytest -x tests/test_api_auth.py -v
```

预期：原 `TestRequireToken` 5 场景 + 新增 3 class 共 ~19 测试全 PASS。

- [ ] **Step 3.5: 提交**

```bash
git add api/auth.py tests/test_api_auth.py
git commit -m "feat(api): add require_scopes FastAPI dependency

- Security + SecurityScopes for OpenAPI integration
- 401 for missing/invalid token, 403 for insufficient scope
- empty SecurityScopes.scopes == require_token behavior
- empty token.scopes == full access (spec §5)"
```

**工作量：** 15 min

---

## Task 4: 13 个路由迁移 `Depends(require_token)` → `Security(require_scopes, scopes=[...])`

**依赖：** Task 3 完成。

**Files:**
- Modify: `api/routes/check.py:46, 167, 189`（3 路由）
- Modify: `api/routes/messages.py:103, 150, 170, 275`（4 路由）
- Modify: `api/routes/subscriptions.py:43, 54, 79, 103, 130`（5 路由）
- `api/routes/health.py` 不动（无鉴权）

**关键决策：**
- scope 映射严格按 spec §9 表
- 替换前先跑现有 happy-path 测试确认绿，替换后再跑确认绿（验证「空 = 全权限」兼容）
- import 改成 `from fastapi import Security`（去掉 `Depends` 若不再用，ruff F401 检查）

- [ ] **Step 4.1: 基线测试（先确认现状全绿）**

```bash
uv run pytest -x tests/test_api_check.py tests/test_api_messages.py \
  tests/test_api_subscriptions.py tests/test_api_fetch.py -v
```

预期：所有现有测试 PASS（含 9 个 `test_*_no_token_returns_401`）。
**这一步不动代码，纯基线**。

- [ ] **Step 4.2: 迁移 `api/routes/check.py`**

修改 `api/routes/check.py:14-21` 的 import（把 `Depends` 改成 `Security`）：

```python
from fastapi import APIRouter, Query, Request, Security
```

`from api.auth import require_token` → `from api.auth import require_scopes`（line 19 附近）。

修改 3 个路由签名（line 46 / 167 / 189）：

```python
# line 42-47: POST /check/run
async def check_run(
    body: CheckRunRequest,
    request: Request,
    _token_name: str = Security(require_scopes, scopes=["check:run"]),
) -> CheckRunResponse:

# line 164-168: GET /check/status
async def check_status(
    request: Request,
    _token_name: str = Security(require_scopes, scopes=["check:read"]),
) -> CheckStatusResponse:

# line 186-190: GET /check/stream
async def check_stream(
    request: Request,
    _token_name: str = Security(require_scopes, scopes=["check:read"]),
) -> StreamingResponse:
```

- [ ] **Step 4.3: 迁移 `api/routes/messages.py`**

import 同 Step 4.2 模式。修改 4 个路由签名（line 103 / 150 / 170 / 275）：

```python
# line 95-104: GET /messages → messages:read
# line 146-151: GET /messages/{id} → messages:read
# line 166-171: POST /messages/rerun → messages:write
# line 271-276: POST /messages/fetch → messages:write
```

**注意：** `_token_name: str = Security(require_scopes, scopes=["messages:read"])` /
`scopes=["messages:write"]`。

- [ ] **Step 4.4: 迁移 `api/routes/subscriptions.py`**

import 同上。修改 5 个路由签名（line 43 / 54 / 79 / 103 / 130）：

```python
# line 39-44: GET /subscriptions → subscriptions:read
# line 50-55: POST /subscriptions → subscriptions:write
# line 72-80: DELETE /subscriptions/{p}/{id} → subscriptions:write
# line 94-104: POST /subscriptions/{p}/{id}/endpoints → subscriptions:write
# line 121-131: DELETE /subscriptions/{p}/{id}/endpoints/{ep} → subscriptions:write
```

- [ ] **Step 4.5: 跑回归（验证兼容性）**

```bash
uv run pytest -x tests/test_api_check.py tests/test_api_messages.py \
  tests/test_api_subscriptions.py tests/test_api_fetch.py -v
```

预期：所有现有测试 **继续 PASS**（含 happy path + 9 个 401 测试）。
原因：测试 fixture `authed_client` 创建的 token 不带 scope（= 全权限），
任何 required scope 都放行 —— 这本身就是「空 = 全权限」兼容性的端到端验证。

**如果有测试红**：检查是否漏改某个 import（`Depends` 残留在某路由）。
ruff 会标 `F401 'Depends' imported but unused` 帮助定位。

- [ ] **Step 4.6: ruff 检查 import 清理**

```bash
uv run ruff check api/routes/ --select F401
```

预期：无 F401（`Depends` 已从 check/messages/subscriptions 全部移除，但
保留 `Security` / `Query` / `Request` 等仍在用的）。

**注意：** 如果某文件还有 `Query` 等用 `Depends` 之外的需求被 ruff 误删，
手动加回。一般 check/messages/subscriptions 三文件改完后 `Depends` 都不再
需要（路由签名只用 `Security`）。

- [ ] **Step 4.7: pyright 类型检查**

```bash
uv run pyright api/routes/ api/auth.py
```

预期：0 errors, 0 warnings。

**注意：不要加 `.` 参数！见 AGENTS.md gotcha。**

- [ ] **Step 4.8: 提交**

```bash
git add api/routes/check.py api/routes/messages.py api/routes/subscriptions.py
git commit -m "refactor(api): migrate 12 routes from Depends(require_token) to Security(require_scopes)

scope mapping (spec §9):
- check.py:  POST /check/run → check:run
             GET /check/status, /check/stream → check:read
- messages.py: GET /messages, /messages/{id} → messages:read
               POST /messages/rerun, /messages/fetch → messages:write
- subscriptions.py: GET /subscriptions → subscriptions:read
                    POST/DELETE (5 routes) → subscriptions:write
- health.py unchanged (public, no auth)"
```

**工作量：** 15 min

---

## Task 5: CLI 扩展 — `token create --scope` + `list` 显示 scopes

**依赖：** Task 1 + Task 2（用 `ALL_SCOPES` 做白名单校验）。

**Files:**
- Modify: `api/token_tool.py:47-62`（`create` 命令）+ `65-80`（`list_cmd`）
- Test: `tests/test_api_token_tool.py`（追加 `TestCreateWithScopes` /
  `TestListWithScopes` class）

**关键决策：**
- Click `multiple=True` 实现 multi-flag（`--scope a --scope b`）
- CLI 层校验 scope 在 `ALL_SCOPES` 白名单（防拼写错误）
- `list` 空 scope 显示 `(unrestricted)`，非空用 `, ` 连接
- CLI 本身**不受 scope 约束**（spec §8.3，文件系统权限 = 鉴权）

- [ ] **Step 5.1: 写失败测试**

追加到 `tests/test_api_token_tool.py`（`TestEndToEnd` class 之后）：

```python
# ── create --scope ──────────────────────────────────────────────


class TestCreateWithScopes:
    def test_create_with_scopes_persists(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from web.auth import load_auth_config

        result = runner.invoke(
            cli,
            [
                "create", "notifier",
                "--scope", "messages:read",
                "--scope", "check:read",
            ],
        )
        assert result.exit_code == 0
        cfg = load_auth_config()
        assert cfg.api_tokens[0].scopes == ["messages:read", "check:read"]

    def test_create_without_scope_warns_unrestricted(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """不带 --scope → 全权限，输出警告（spec §5.3）。"""
        result = runner.invoke(cli, ["create", "bot"])
        assert result.exit_code == 0
        # 警告文本含「无限制」或「unrestricted」
        assert "无限制" in result.output or "unrestricted" in result.output.lower()

    def test_create_with_invalid_scope_fails(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """--scope xxx 不在白名单 → 退出码非 0，不落盘。"""
        result = runner.invoke(
            cli, ["create", "bot", "--scope", "messages:delete"]
        )
        assert result.exit_code != 0
        assert "scope" in result.output.lower() or "未知" in result.output

        from web.auth import load_auth_config
        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 0  # 未落盘

    def test_create_with_tokens_manage_placeholder_ok(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """tokens:manage 占位常量也应通过白名单校验（虽然路由不消费）。"""
        result = runner.invoke(
            cli, ["create", "bot", "--scope", "tokens:manage"]
        )
        assert result.exit_code == 0


# ── list with scopes ────────────────────────────────────────────


class TestListWithScopes:
    def test_list_shows_unrestricted_for_empty_scopes(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from api.auth import create_token

        create_token("admin-bot")  # 空 scope
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "admin-bot" in result.output
        assert "无限制" in result.output or "unrestricted" in result.output.lower()

    def test_list_shows_scope_list(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from api.auth import create_token

        create_token(
            "notifier", scopes=["messages:read", "check:read"]
        )
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        out = result.output
        assert "notifier" in out
        assert "messages:read" in out
        assert "check:read" in out
```

- [ ] **Step 5.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_api_token_tool.py::TestCreateWithScopes \
  tests/test_api_token_tool.py::TestListWithScopes -v
```

预期：`Error: No such option: --scope`（Click 拒绝未知 option）。

- [ ] **Step 5.3: 实现 `create` 命令的 --scope**

修改 `api/token_tool.py:47-62`（`create` 函数）：

```python
@cli.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="覆盖同名 token")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="限制 token scope（可多次指定，如 --scope messages:read --scope check:read）。"
    "不指定 = 全权限。合法 scope 见 ALL_SCOPES 常量。",
)
def create(name: str, force: bool, scopes: tuple[str, ...]) -> None:
    """生成新 token，明文仅打印一次（存储为 SHA-256 hash，无法恢复）。

    ``--scope`` 可多次指定，限制 token 可访问的 API 范围（spec §4）。
    不传 ``--scope`` → 全权限 token（向后兼容老 bot，但建议生产环境显式收紧）。
    """
    from api.auth import ALL_SCOPES

    # scope 白名单校验（防拼写错误）
    invalid = [s for s in scopes if s not in ALL_SCOPES]
    if invalid:
        console.print(
            f"[red]✗[/] 未知 scope: {', '.join(invalid)}",
            style="red",
        )
        console.print(f"[dim]合法 scope: {', '.join(ALL_SCOPES)}[/]")
        sys.exit(1)

    if _token_exists(name) and not force:
        console.print(
            f"[red]✗[/] token '{name}' 已存在，加 --force 覆盖",
            style="red",
        )
        sys.exit(1)

    scope_list = list(scopes)
    plain = create_token(name, scopes=scope_list)
    console.print(f"[green]✓[/] 已创建 token '{name}'，明文（仅此一次）：")
    console.print(f"[yellow]{plain}[/]")
    console.print("[dim]存储为 SHA-256 hash，后续无法再查看明文。[/]")
    if scope_list:
        console.print(f"[cyan]📝[/] Scopes: {', '.join(scope_list)}")
    else:
        console.print(
            "[yellow]⚠️[/] 未指定 scope = [bold]无限制[/]（全权限）。"
            " 建议生产环境用 --scope 显式收紧。",
            style="yellow",
        )
```

**注意：** `create_token` 已在 `api/token_tool.py:29` import。`ALL_SCOPES` 走
lazy import（函数内 `from api.auth import ALL_SCOPES`），避免顶层 unused import
（其他子命令 `list` / `revoke` 不需要 ALL_SCOPES）。

- [ ] **Step 5.4: 实现 `list` 命令显示 scopes**

修改 `api/token_tool.py:73-80`（`list_cmd` 的 Table 构造）：

```python
table = Table(title="API Tokens")
table.add_column("Name")
table.add_column("Hash (前 8 位)")
table.add_column("Created At")
table.add_column("Scopes")
for t in cfg.api_tokens:
    created = datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M")
    if t.scopes:
        scopes_str = ", ".join(t.scopes)
    else:
        scopes_str = "(无限制)"
    table.add_row(t.name, t.token_hash[:8], created, scopes_str)
console.print(table)
```

- [ ] **Step 5.5: 运行测试确认通过**

```bash
uv run pytest -x tests/test_api_token_tool.py -v
```

预期：原 `TestCreate` / `TestList` / `TestRevoke` / `TestEndToEnd` 共 8 测试 +
新增 6 测试 = **14 测试 PASS**。

- [ ] **Step 5.6: 提交**

```bash
git add api/token_tool.py tests/test_api_token_tool.py
git commit -m "feat(cli): token create --scope multi-flag + list shows scopes

- Click multiple=True for --scope (spec §8.1)
- ALL_SCOPES whitelist validation prevents typos
- create without --scope warns 'unrestricted' (spec §5.3)
- list column 'Scopes' shows comma-joined or '(无限制)'
- CLI itself not subject to scope (filesystem = authz, spec §8.3)"
```

**工作量：** 15 min

---

## Task 6: 集成测试补全 — 12 个路由的 `insufficient_scope_returns_403`

**依赖：** Task 4 完成。

**Files:**
- Modify: `tests/test_api_check.py`（3 路由）
- Modify: `tests/test_api_messages.py`（4 路由）
- Modify: `tests/test_api_subscriptions.py`（5 路由，含 endpoints）
- Modify: `tests/test_api_fetch.py`（1 路由，fetch）

**关键决策：**
- 新建 `scoped_client(scopes)` fixture（每个测试文件独立定义，复用
  `authed_client` 模式，避免跨文件共享 fixture 复杂度）
- 403 测试用「明显不相关的 scope」制造权限不足（如给 messages:read 访问 check:run）
- 写路由测试用 read scope 访问 write 路由（验证 read 不隐含 write）
- check:run / check:read 正交专门测试

- [ ] **Step 6.1: 在 `tests/test_api_check.py` 加 scoped fixture + 3 个 403 测试**

在文件末尾（`TestCheckStream` class 之后）追加：

```python
@pytest.fixture
async def scoped_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request
) -> AsyncClient:
    """带指定 scope 的 client。

    用法: ``client = await scoped_client(["messages:read"])``
    通过 ``request.param`` 传 scope 列表（pytest indirect）。
    """
    scopes = request.param
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token("scoped-bot", scopes=scopes)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestCheckScopes:
    """check 路由 scope 校验（spec §10.2）。"""

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_run_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """token 只有 messages:read，访问 /check/run → 403。"""
        resp = await scoped_client.post(
            "/api/v1/check/run", json={"mode": "full"}
        )
        assert resp.status_code == 403
        assert "scope" in resp.json()["detail"].lower()
        assert "check:run" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["check:run"]], indirect=True
    )
    async def test_status_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """token 只有 check:run（正交），访问 /check/status → 403。

        spec §4.4: check:run / check:read 正交，不互相隐含。
        """
        resp = await scoped_client.get("/api/v1/check/status")
        assert resp.status_code == 403
        assert "check:read" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["check:run"]], indirect=True
    )
    async def test_stream_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """token 只有 check:run，访问 /check/stream → 403。"""
        resp = await scoped_client.get("/api/v1/check/stream")
        assert resp.status_code == 403
```

**注意：** `scoped_client` fixture 用 `request.param` + `indirect=True`，
scope 列表通过 parametrize 注入，每个测试独立 token / client。

- [ ] **Step 6.2: 跑 check 测试**

```bash
uv run pytest -x tests/test_api_check.py::TestCheckScopes -v
```

预期：3 个测试 PASS。

**跑全 check 测试确认无回归**：

```bash
uv run pytest -x tests/test_api_check.py -v
```

预期：所有测试 PASS。

- [ ] **Step 6.3: 在 `tests/test_api_messages.py` 加 scoped fixture + 4 个 403 测试**

追加到文件末尾（沿用 Step 6.1 的 `scoped_client` fixture 定义，复制到本文件）：

```python
class TestMessagesScopes:
    """messages 路由 scope 校验。"""

    @pytest.mark.parametrize(
        "scoped_client", [["check:read"]], indirect=True
    )
    async def test_list_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.get("/api/v1/messages")
        assert resp.status_code == 403
        assert "messages:read" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["check:read"]], indirect=True
    )
    async def test_get_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.get("/api/v1/messages/some-id")
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_rerun_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """messages:read 不隐含 messages:write（spec §4.3）。"""
        resp = await scoped_client.post(
            "/api/v1/messages/rerun",
            json={"mode": "all"},
        )
        assert resp.status_code == 403
        assert "messages:write" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_fetch_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """messages:read 不隐含 messages:write（spec §4.3）。

        fetch 路由在 messages.py:271，也要求 messages:write。
        """
        resp = await scoped_client.post(
            "/api/v1/messages/fetch",
            json={"platform": "bilibili", "msg_ids": ["123"]},
        )
        assert resp.status_code == 403
```

**注意：** 若 `test_api_fetch.py` 是独立文件，Step 6.4 单独处理。若 fetch
测试在 messages 测试文件里，跳过 Step 6.4。

- [ ] **Step 6.4: 检查 `tests/test_api_fetch.py` 是否需要单独 403 测试**

```bash
uv run pytest --collect-only tests/test_api_fetch.py -q
```

如果 `test_api_fetch.py` 已独立测试 fetch 路由（独立 `authed_client`），
在该文件追加 scoped fixture + 1 个 403 测试，沿用 Step 6.3 模式。

如果 fetch 测试在 `test_api_messages.py` 内（Step 6.3 已覆盖），跳过本步。

- [ ] **Step 6.5: 在 `tests/test_api_subscriptions.py` 加 scoped fixture + 5 个 403 测试**

追加到文件末尾：

```python
class TestSubscriptionsScopes:
    """subscriptions 路由 scope 校验。"""

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_list_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.get("/api/v1/subscriptions")
        assert resp.status_code == 403
        assert "subscriptions:read" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_add_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """read 不隐含 write（spec §4.3）。"""
        resp = await scoped_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bilibili", "identifier": "123", "name": "UP"},
        )
        assert resp.status_code == 403
        assert "subscriptions:write" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_remove_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.delete(
            "/api/v1/subscriptions/bilibili/123"
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_bind_endpoint_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.post(
            "/api/v1/subscriptions/bilibili/123/endpoints",
            json={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_unbind_endpoint_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.delete(
            "/api/v1/subscriptions/bilibili/123/endpoints/gotify-main"
        )
        assert resp.status_code == 403
```

- [ ] **Step 6.6: 跑 subscriptions 测试**

```bash
uv run pytest -x tests/test_api_subscriptions.py::TestSubscriptionsScopes -v
uv run pytest -x tests/test_api_subscriptions.py -v  # 全量回归
```

预期：新增 5 个 403 测试 + 原 16 测试 = **21 PASS**。

- [ ] **Step 6.7: messages + fetch 全量回归**

```bash
uv run pytest -x tests/test_api_messages.py tests/test_api_fetch.py -v
```

预期：所有 PASS。

- [ ] **Step 6.8: 提交**

```bash
git add tests/test_api_check.py tests/test_api_messages.py \
        tests/test_api_subscriptions.py tests/test_api_fetch.py
git commit -m "test(api): add insufficient_scope_returns_403 for 12 protected routes

- scoped_client fixture with parametrized scope list
- check:run / check:read orthogonality validated (spec §4.4)
- messages:read does NOT imply messages:write (spec §4.3)
- subscriptions:read does NOT imply subscriptions:write
- error detail includes required scope name for client debugging"
```

**工作量：** 25 min

---

## Task 7: 全量验证 — ruff + pyright + pytest + OpenAPI 检查

**依赖：** Task 1-6 全部完成。

**Files:** 无（只跑命令）。

- [ ] **Step 7.1: ruff**

```bash
uv run ruff check .
```

预期：`All checks passed!`。

**如果有 F401（unused import）**：检查 `api/routes/check.py` /
`messages.py` / `subscriptions.py` 顶部是否漏删 `Depends`（Task 4 已删，但
有时 `Query` 等 import 误删）。手动 `git diff api/routes/` 确认。

**如果有 I001（import order）**：跑 `uv run ruff check --fix .` 自动修。

- [ ] **Step 7.2: pyright**

```bash
uv run pyright
```

**注意：不要加 `.` 参数！见 AGENTS.md gotcha。** 加 `.` 会让 pyright 扫
`.venv/`，9601 文件卡死。

预期：`0 errors, 0 warnings`。

**常见可能告警**：
- `api/auth.py` 新增 `require_scopes` 函数的 `SecurityScopes` 参数无默认值，
  pyright 可能提示「param without default follows param with default」——
  这是 FastAPI 依赖函数的合法模式，FastAPI 通过依赖注入跳过普通 Python 调用规则。
  如出问题，函数签名不动（FastAPI doc 明示此模式 OK）。
- 测试 `scoped_client` fixture 的 `request` 参数无类型 —— 加
  `request: pytest.FixtureRequest` 显式标注。

- [ ] **Step 7.3: api_auth 单元测试**

```bash
uv run pytest -x tests/test_api_auth.py -v
```

预期：原 19 测试 + 新增 ~18 测试（TestScopesPersistence 4 + TestScopeUtils 8 +
TestRequireScopes 6）= **~37 测试 PASS**。

- [ ] **Step 7.4: token_tool 测试**

```bash
uv run pytest -x tests/test_api_token_tool.py -v
```

预期：原 8 测试 + 新增 6 测试 = **14 PASS**。

- [ ] **Step 7.5: 路由集成测试**

```bash
uv run pytest -x tests/test_api_check.py tests/test_api_messages.py \
  tests/test_api_subscriptions.py tests/test_api_fetch.py -v
```

预期：原测试 + 新增 12 个 403 测试 = 全部 PASS。

- [ ] **Step 7.6: OpenAPI docs 检查**

写一个一次性检查脚本（不入 git，跑完即删）：

```bash
uv run python -c "
import asyncio
from httpx import ASGITransport, AsyncClient
from web.app import create_app
from pathlib import Path
import tempfile

async def check():
    with tempfile.TemporaryDirectory() as td:
        import sys
        # monkeypatch auth.toml 到 tmp（避免读真 data/auth.toml）
        from web import auth as wa
        wa.AUTH_TOML_PATH = Path(td) / 'auth.toml'
        wa.set_password('test')
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://t') as c:
            resp = await c.get('/openapi.json')
            spec = resp.json()
            # /api/v1/messages 路径应含 security 字段
            messages_get = spec['paths']['/api/v1/messages']['get']
            assert 'security' in messages_get, 'security field missing'
            print('OK: /api/v1/messages security =', messages_get['security'])

asyncio.run(check())
"
```

预期：输出 `OK: /api/v1/messages security = [{'OAuth2PasswordBearer': ['messages:read']}]`
或类似（FastAPI 自动生成 `OAuth2PasswordBearer` 风格的 scope 数组）。

**如果 security 字段缺失**：检查 `api/auth.py:require_scopes` 是否用了
`Security` 而非 `Depends`（FastAPI 只对 `Security` 生成 security 字段）。

- [ ] **Step 7.7: 全量回归**

```bash
uv run pytest -x
```

预期：所有测试 PASS。如有 flaky 或无关失败，单独排查，不在本 PR 修。

- [ ] **Step 7.8: （可选）手动端到端验证**

仅当前 6 个 Task 全绿且要在本地确认端到端可用时跑：

```bash
# 1. 启动 API
uv run trawler serve &  # 或具体入口，看 pyproject [project.scripts]

# 2. 创建全权限 token（CLI 警告「无限制」）
uv run python -m api.token_tool create admin-bot

# 3. 创建受限 token
uv run python -m api.token_tool create notifier \
    --scope messages:read --scope check:read

# 4. list 应显示 scopes 列
uv run python -m api.token_tool list

# 5. 用受限 token 触发 run → 403
ADMIN_TOKEN=$(...)  # 从 step 2 输出捞
NOTIFIER_TOKEN=$(...)  # 从 step 3 输出捞

curl -X POST http://localhost:8000/api/v1/check/run \
  -H "Authorization: Bearer $NOTIFIER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"full"}'
# 预期 403 + {"detail":"insufficient scope: requires check:run"}

# 6. 用受限 token 拉消息 → 200
curl http://localhost:8000/api/v1/messages \
  -H "Authorization: Bearer $NOTIFIER_TOKEN"

# 7. OpenAPI docs 检查
curl -s http://localhost:8000/openapi.json | python -m json.tool | grep -A2 security | head
```

**工作量：** 15 min

---

## Self-Review 结果

### Spec coverage

| spec 章节 | 对应 Task |
|----------|----------|
| §4 Scope 体系（6+1 常量） | Task 2 Step 2.3 定义全部 7 常量 |
| §4.3 write→read 隐含 | Task 2 `scope_implies` + 测试 |
| §4.4 check:run/read 正交 | Task 2 `scope_implies` 测试 + Task 6 Step 6.2 集成测试 |
| §5 空 = 全权限 | Task 1 `ApiTokenEntry.scopes` 默认 `[]` + Task 2 `token_has_scope` + Task 6 fixture `authed_client` 现状自动验证 |
| §6 FastAPI Security + SecurityScopes | Task 3 `require_scopes` |
| §6.3 401 vs 403 分离 | Task 3 `require_scopes` + Task 6 全部 403 测试 |
| §7 ApiTokenEntry + auth.toml 扩展 | Task 1 完整覆盖 |
| §8 CLI --scope + list 显示 | Task 5 完整覆盖 |
| §9 13 路由迁移映射表 | Task 4 按表逐个迁移 |
| §10 测试策略 | Task 1 (持久化) + Task 2 (工具) + Task 3 (依赖) + Task 5 (CLI) + Task 6 (12 路由 403) |
| §11 风险与缓解 | tomlkit 空 array 处理（Task 1 Step 1.4 + 1.4 测试） / Security 签名兼容（Task 4 Step 4.5 回归验证） |
| §12 tokens:manage 占位 | Task 2 定义常量 + docstring 明确不消费 + Task 5 Step 5.3 测试 CLI 接受 |
| §13 验证清单 | Task 7 完整对应 |

### Placeholder scan

- 无 "TBD" / "TODO" / "implement later" / "add appropriate error handling"。
- 每个 Task 都有可直接 paste 的完整 Python 代码（除 Task 6 部分用 `...` 表示
  重复模式，已用文字说明复用 Step 6.1 的 fixture 定义）。
- 每个 step 都有具体命令 + 预期输出。

### Type consistency

- `create_token(name, scopes=None, auth_path=...)` — Task 1 Step 1.5 定义，
  Task 5 Step 5.3 / 测试调用签名一致（`scopes=None` 默认 → `[]` 落盘）。
- `scope_implies(granted: str, required: str) -> bool` — Task 2 定义，
  `token_has_scope` 内部调用签名一致。
- `token_has_scope(token: ApiTokenEntry, required: str) -> bool` — Task 2 定义，
  Task 3 `require_scopes` 内部调用一致。
- `require_scopes(security_scopes: SecurityScopes, request: Request) -> str` —
  Task 3 定义，Task 4 12 路由通过 `Security(require_scopes, scopes=[...])` 引用
  签名一致。
- `scoped_client` fixture 在 Task 6 各测试文件独立定义（避免跨文件共享），
  签名 `request.param` → scope list 一致。
- `ALL_SCOPES: tuple[str, ...]` — Task 2 定义，Task 5 CLI 校验引用一致
  （包含 `tokens:manage` 占位，spec §4.2 / §12）。
- `_WRITE_IMPLIES_READ` dict 是私有，只在 `scope_implies` 内部用。

### Backward compatibility

- 老 `data/auth.toml`（无 `scopes` 字段）→ `load_auth_config` 用 `t.get("scopes", [])`
  → 空 list → `token_has_scope` 返 True → 全权限放行（Task 1 测试 +
  Task 6 `authed_client` 默认无 scope fixture 自动验证）。
- 现有所有 happy-path 测试**不需要任何改动**继续通过（Task 4 Step 4.5 + Task 7
  Step 7.5 双重验证）。
- 现有 9 个 `test_*_no_token_returns_401` 测试**不需要任何改动**（401 错误
  detail 文本「invalid or missing token」未改）。

---

## 估算总览

| Task | 工作量 | 阻塞关系 |
|------|--------|---------|
| 1. 数据层：ApiTokenEntry + scopes + auth.toml I/O | 15 min | — |
| 2. scope 常量 + scope_implies / token_has_scope | 10 min | Task 1 |
| 3. FastAPI require_scopes 依赖 | 15 min | Task 1 + Task 2 |
| 4. 12 路由迁移 Depends → Security | 15 min | Task 3 |
| 5. CLI --scope + list 显示 | 15 min | Task 1 + Task 2 |
| 6. 12 路由 insufficient_scope 403 测试 | 25 min | Task 4 |
| 7. 全量验证（ruff/pyright/pytest/openapi） | 15 min | Task 1-6 全部 |

**总计**：约 **110 min**（不含手动端到端验证）。

**并行机会**：Task 5（CLI）可与 Task 3 / Task 4 并行（不依赖 FastAPI 路由
改动）。Task 6（测试）必须在 Task 4 之后。
