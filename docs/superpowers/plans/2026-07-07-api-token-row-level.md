# Implementation Plan: API Token 行级权限（Row-Level）

- 日期: 2026-07-07
- 关联 spec: `docs/superpowers/specs/2026-07-07-api-token-row-level-design.md`
- 关联 issue: #106
- 分支: `feat/api-token-row-level-106`
- 任务数: 7
- TDD 风格：每任务先写测试（红），再写实现（绿），最后跑验证命令

## 全局约定

- 所有改动遵守 `from __future__ import annotations` + type hint
- 调试用 `logging.getLogger(__name__)`，用户可见输出用 `console.print`
- 不改 `print` / `console.print` 已有 emoji / 颜色标签
- 不改已有 error message 文本
- 提交粒度：每个 Task 一个 commit（TDD 红→绿 拆两个 commit 可选）

## Task 依赖图

```
T1 数据层 ──┬──> T2 过滤工具 ──> T3 FastAPI 依赖 ──┬──> T4 GET 路由
           │                                      ├──> T5 写入路由
           │                                      └──> T6 CLI 扩展
           └──────────────────────────────────────> T6（共享 create_token 改造）
T7 测试补全（依赖 T1-T6）
```

T1 是基础，T2 / T6 都依赖 T1；T3 依赖 T2；T4 / T5 依赖 T3；T7 是收尾整合。

---

## Task 1: 数据层 — `ResourceRules` + `ApiTokenEntry.resource_rules` + auth.toml 嵌套读写

### 目标

让 `ApiTokenEntry` 能携带行级规则，`auth.toml` 能正确序列化嵌套 `[resource_rules]`
table，老格式文件无字段时默认全权限。

### 涉及文件:行号

- `shared/config.py:248-262` — `ApiTokenEntry` dataclass
- `web/auth.py:74-132` — `load_auth_config` / `save_auth_config`
- `api/auth.py:158-181` — `create_token`（加 `resource_rules` 参数）

### TDD

**先写测试**（`tests/test_api_auth.py` 追加 `TestResourceRulesData` class）：

```python
class TestResourceRulesData:
    # 注：项目 pyproject.toml 已设 asyncio_mode = "auto"，async 测试直接
    # async def 即可，不要在 conftest 另配 @pytest.mark.asyncio 装饰器
    # （与现有 TestRequireScopes 风格一致，见 tests/test_api_auth.py:313）。

    def test_resource_rules_default_is_unrestricted(self):
        """新 ApiTokenEntry 默认 resource_rules 两字段 None（全权限）。"""
        entry = ApiTokenEntry(name="x", token_hash="h")
        assert entry.resource_rules.platforms is None
        assert entry.resource_rules.subscription_refs is None

    def test_resource_rules_with_platforms(self):
        entry = ApiTokenEntry(
            name="x", token_hash="h",
            resource_rules=ResourceRules(platforms=["bili"]),
        )
        assert entry.resource_rules.platforms == ["bili"]
        assert entry.resource_rules.subscription_refs is None

    async def test_load_auth_config_legacy_no_resource_rules(self, tmp_path, monkeypatch):
        """老 auth.toml 无 resource_rules 字段 → 加载为默认全权限。"""
        ...  # 写入老格式 toml，断言 entry.resource_rules.platforms is None

    async def test_load_auth_config_with_resource_rules(self, tmp_path, monkeypatch):
        """新格式含 [resource_rules] → 正确加载嵌套字段。"""
        ...

    async def test_save_auth_config_default_omits_resource_rules(self, tmp_path, monkeypatch):
        """默认 ResourceRules() 不写出 [resource_rules] section（diff 干净）。"""
        ...

    async def test_save_auth_config_round_trip(self, tmp_path, monkeypatch):
        """非默认 rules 写出后再加载，字段一致（5 种形态覆盖）。

        形态: platforms only / subs only / both / both None / platforms=[]（空 list）。
        特别：platforms=[] 写盘再读回必须仍是 []（不是 None）—— 空 list 是
        「拒绝一切」语义，与 None=全权限 相反，不能被 tomlkit 丢失。
        空 array 要用 tomlkit.array() 显式构造（参考 web/auth.py:119-123
        scopes 空 list 的处理），不能直接赋 []。
        """
        # 形态 5 单独断言：
        #   entry = ApiTokenEntry(name="x", token_hash="h",
        #       resource_rules=ResourceRules(platforms=[]))
        #   save_auth_config(cfg_with(entry)); reloaded = load_auth_config()
        #   assert reloaded.api_tokens[0].resource_rules.platforms == []
        ...

    async def test_create_token_with_resource_rules(self, tmp_path, monkeypatch):
        """create_token(resource_rules=...) 落盘后能读回。"""
        ...
```

**再写实现**：

1. `shared/config.py` 在 `ApiTokenEntry` 之前新增 `ResourceRules` dataclass
   （spec §4.1）。注意 import 顺序：`ResourceRules` 必须在 `ApiTokenEntry`
   之前定义（`ApiTokenEntry.resource_rules` 默认值 `field(default_factory=ResourceRules)`
   需要 `ResourceRules` 在模块 globals 中可见）。

2. `web/auth.py:load_auth_config`（line 84-93）扩展：

   ```python
   rules_raw = t.get("resource_rules", {})
   resource_rules = ResourceRules(
       platforms=list(rules_raw["platforms"]) if "platforms" in rules_raw else None,
       subscription_refs=list(rules_raw["subscription_refs"]) if "subscription_refs" in rules_raw else None,
   )
   ApiTokenEntry(name=..., ..., resource_rules=resource_rules)
   ```

   注意 `rules_raw` 是 dict（tomllib 解析嵌套 table 自动成 dict）。

3. `web/auth.py:save_auth_config`（line 113-124）扩展：判断 `ResourceRules`
   非默认（两字段任一非 None）时写嵌套 table：

   ```python
   rules = t.resource_rules
   if rules.platforms is not None or rules.subscription_refs is not None:
       nested = tomlkit.table()
       if rules.platforms is not None:
           arr = tomlkit.array()
           for p in rules.platforms:
               arr.append(p)
           nested["platforms"] = arr
       if rules.subscription_refs is not None:
           arr = tomlkit.array()
           for s in rules.subscription_refs:
               arr.append(s)
           nested["subscription_refs"] = arr
       entry["resource_rules"] = nested
   ```

4. `api/auth.py:create_token`（line 158-181）加 `resource_rules` 参数：

   ```python
   def create_token(
       name: str,
       scopes: list[str] | None = None,
       resource_rules: ResourceRules | None = None,
       auth_path: Path = AUTH_TOML_PATH,
   ) -> str:
       ...
       cfg.api_tokens.append(
           ApiTokenEntry(
               name=name,
               token_hash=_hash_token(plain),
               created_at=datetime.now(timezone.utc).timestamp(),
               scopes=list(scopes) if scopes else [],
               resource_rules=resource_rules or ResourceRules(),
           )
       )
   ```

### 验证

```bash
uv run pytest -x tests/test_api_auth.py::TestResourceRulesData -v
uv run ruff check shared/config.py web/auth.py api/auth.py
uv run pyright
```

### 依赖

无（基础任务）。

---

## Task 2: 过滤工具 — `TokenResourceFilter` dataclass + `from_token` / `allows_*`

### 目标

把 `ResourceRules` 转成路由层易用的不可变 `TokenResourceFilter`，集中所有
「消息可见性」判断逻辑。

### 涉及文件:行号

- 新增 `api/resource_filter.py`
- `tests/test_resource_filter.py`（新增）

### TDD

**先写测试**（`tests/test_resource_filter.py`，新文件）：

```python
from dataclasses import replace
from shared.config import ApiTokenEntry, ResourceRules
from shared.protocols import MessageRecord, ContentType, Phase
from api.resource_filter import TokenResourceFilter


def _msg(platform: str, sub_ref: str) -> MessageRecord:
    return MessageRecord(
        msg_id=f"{platform}:x", platform=platform, content_type=ContentType.TEXT,
        phase=Phase.DETECTED, pubdate=0, title="", author="",
        subscription_ref=sub_ref,
    )


class TestTokenResourceFilter:
    def test_from_token_no_rules_is_unrestricted(self):
        token = ApiTokenEntry(name="x", token_hash="h")
        f = TokenResourceFilter.from_token(token)
        assert f.platforms is None
        assert f.subscription_refs is None
        assert f.allows_platform("bili") is True
        assert f.allows_message(_msg("xhs", "u1")) is True

    def test_platforms_filter_restricts(self):
        token = ApiTokenEntry(
            name="x", token_hash="h",
            resource_rules=ResourceRules(platforms=["bili"]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_platform("bili") is True
        assert f.allows_platform("xhs") is False

    def test_subscription_refs_uses_composite_key(self):
        token = ApiTokenEntry(
            name="x", token_hash="h",
            resource_rules=ResourceRules(subscription_refs=["bili:100"]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_subscription("bili", "100") is True
        assert f.allows_subscription("bili", "200") is False
        assert f.allows_subscription("xhs", "100") is False  # 跨平台

    def test_allows_message_and_combination(self):
        """platforms=[bili] + subs=[bili:100] AND 组合。"""
        token = ApiTokenEntry(
            name="x", token_hash="h",
            resource_rules=ResourceRules(platforms=["bili"], subscription_refs=["bili:100"]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_message(_msg("bili", "100")) is True
        assert f.allows_message(_msg("bili", "200")) is False  # sub 维度拒绝
        assert f.allows_message(_msg("xhs", "u456")) is False  # platform 维度拒绝

    def test_empty_platforms_list_denies_all(self):
        """platforms=[] = 拒绝一切（与 None=全权限 相反）。"""
        token = ApiTokenEntry(
            name="x", token_hash="h",
            resource_rules=ResourceRules(platforms=[]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_platform("bili") is False
        assert f.allows_message(_msg("bili", "100")) is False

    def test_unrestricted_factory(self):
        f = TokenResourceFilter.unrestricted()
        assert f.allows_message(_msg("bili", "100")) is True

    def test_frozen_dataclass(self):
        """TokenResourceFilter 不可变。"""
        import dataclasses

        f = TokenResourceFilter(platforms=frozenset({"bili"}), subscription_refs=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.platforms = None  # type: ignore[misc]
```

**再写实现**（`api/resource_filter.py`）：

按 spec §6.2 实现 `TokenResourceFilter` dataclass。注意：

- `from_token` 接受 `ApiTokenEntry`，读 `.resource_rules`
- 用 `frozenset` 而非 `set`（不可变 + `in` 查找 O(1)）
- `allows_subscription(platform, subscription_ref)` 接受**不带前缀**的原始
  subscription_ref（detector 注入值），内部拼 `<platform>:<subscription_ref>`
  再比对

模块顶部统一定义 platform 映射常量（全文唯一来源，T4/T5 一律 import 复用，
**禁止 inline 重定义**）。从 `core.subscription_cli.PLATFORM_TO_SECTION` 反推
`SECTION_TO_SHORT`，避免两处手写同一映射（DRY）：

```python
from core.subscription_cli import PLATFORM_TO_SECTION

# TOML section 全名 → CLI short name（bilibili → bili）
# 唯一定义点：api/resource_filter.py。T4 filter_subscription_dict、
# T5 三个订阅写入路由均 import 此常量。
SECTION_TO_SHORT: dict[str, str] = {v: k for k, v in PLATFORM_TO_SECTION.items()}

# short name → 订阅主键字段（spec §7.3）
SHORT_TO_KEY_FIELD: dict[str, str] = {"bili": "uid", "xhs": "user_id", "weibo": "user_id"}
```

T5 还会用到一个订阅可见性 helper `subscription_visible`，也在本模块定义（见 T5）。

### 验证

```bash
uv run pytest -x tests/test_resource_filter.py -v
uv run ruff check api/resource_filter.py
uv run pyright
```

### 依赖

T1（`ResourceRules` dataclass 已存在）。

---

## Task 3: FastAPI 依赖 — `get_resource_filter`

### 目标

提供单一 FastAPI 依赖，承担 scope 校验 + 行级过滤视图构造，让路由签名简洁。

### 涉及文件:行号

- `api/auth.py:121-155` — `require_scopes`（保留，不删）
- `api/auth.py` 追加 `get_resource_filter`
- `tests/test_api_auth.py` 追加 `TestGetResourceFilter` class

### TDD

**先写测试**：

```python
class TestGetResourceFilter:
    """get_resource_filter 路由集成测试。

    注：项目 ``pyproject.toml`` 已设 ``asyncio_mode = "auto"``，async 测试
    直接 ``async def`` 即可，不要在 conftest 另配 ``@pytest.mark.asyncio``。
    """

    async def test_unrestricted_token_returns_unrestricted_filter(
        self, authed_client
    ):
        """全权限 token → filter.allows_* 永远 True（通过 GET /messages 行为验证）。"""
        # 用现有 authed_client（全权限），不应被过滤
        resp = await authed_client.get("/api/v1/messages")
        assert resp.status_code == 200  # 不被行级过滤拦截

    async def test_missing_token_returns_401(self, tmp_path, monkeypatch):
        """无 Authorization header → 401（与 require_scopes 一致）。

        直接起 ASGI client，不带 Authorization；断言 401 + detail 含 "token"。
        参考 tests/test_api_auth.py:TestRequireScopes.test_no_header_returns_401
        的 401 风格。
        """
        from httpx import ASGITransport, AsyncClient

        from api.auth import create_token  # noqa: F401  (确保模块已 import)
        from shared.config import ResourceRules  # noqa: F401
        from web.app import create_app
        from web.auth import set_password

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
            # 故意不带 Authorization header
        ) as c:
            resp = await c.get("/api/v1/messages")
        assert resp.status_code == 401
        assert "token" in resp.json()["detail"]

    async def test_insufficient_scope_returns_403(self, tmp_path, monkeypatch):
        """scope 不够 → 403（行级过滤层在 scope 之后，scope 先拦）。

        token 只带 ``subscriptions:read``，访问需要 ``messages:read`` 的
        ``GET /api/v1/messages`` → 403 + detail 含 "scope"。

        注：T7 的 ``row_filtered_client`` fixture 此时尚未定义，这里 inline
        构造 client；T7 完成后可回填为 ``row_filtered_client(scopes=[...])``
        形式，但 T3 阶段必须能独立跑通。
        """
        from httpx import ASGITransport, AsyncClient

        from api.auth import create_token
        from web.app import create_app
        from web.auth import set_password

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        # 只给 subscriptions:read，访问 messages → 缺 messages:read
        plain = create_token("sub-only", scopes=["subscriptions:read"])
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plain}"},
        ) as c:
            resp = await c.get("/api/v1/messages")
        assert resp.status_code == 403
        assert "scope" in resp.json()["detail"].lower()

    async def test_openapi_docs_include_scopes(self, tmp_path, monkeypatch):
        """OpenAPI docs 仍正确渲染每个被保护路由的 scope（spec §11 风险表缓解）。

        ``GET /openapi.json`` → 每个 ``Security(get_resource_filter, scopes=[...])``
        路由的 security scheme 含对应 scope（如 ``/api/v1/messages`` GET 含
        ``messages:read``）。防止 FastAPI 嵌套依赖写法下 scope 信息丢失。
        """
        from httpx import ASGITransport, AsyncClient

        from web.app import create_app
        from web.auth import set_password

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            resp = await c.get("/openapi.json")
        assert resp.status_code == 200
        paths = resp.json()["paths"]
        # GET /api/v1/messages 的 security 必须声明 messages:read
        msgs_get = paths["/api/v1/messages"]["get"]
        security = msgs_get.get("security", [])
        declared_scopes: list[str] = []
        for sec in security:
            for _scheme, scopes in sec.items():
                declared_scopes.extend(scopes)
        assert "messages:read" in declared_scopes
```

**再写实现**：

`api/auth.py` 追加（与 `require_scopes` 共存）：

```python
from api.resource_filter import TokenResourceFilter


def _authenticate_and_check_scope(
    request: Request, required_scopes: Sequence[str]
) -> ApiTokenEntry:
    """身份校验 + scope 校验共享逻辑（spec §6.3 私有 helper）。

    返回匹配的 ApiTokenEntry；失败抛 HTTPException（401 / 403）。
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    plain = auth[len("Bearer "):]
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if _verify_token(plain, entry.token_hash):
            for required in required_scopes:
                if not token_has_scope(entry, required):
                    raise HTTPException(
                        status_code=403,
                        detail=f"insufficient scope: requires {required}",
                    )
            return entry
    raise HTTPException(status_code=401, detail="invalid or missing token")


async def get_resource_filter(
    security_scopes: SecurityScopes,
    request: Request,
) -> TokenResourceFilter:
    """FastAPI 依赖：scope 校验 + 行级过滤视图构造（spec §6.3）。

    - 无 header / token 不匹配 → 401
    - 缺 scope → 403
    - 通过 → 返回 TokenResourceFilter（含 token 的行级规则视图）
    """
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return TokenResourceFilter.from_token(entry)
```

**同时**重构 `require_scopes` 调 `_authenticate_and_check_scope`，避免逻辑重复：

```python
async def require_scopes(
    security_scopes: SecurityScopes,
    request: Request,
) -> str:
    """保留供「不需要行级过滤」的路由（如 check）使用。"""
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return entry.name
```

### 验证

```bash
uv run pytest -x tests/test_api_auth.py::TestGetResourceFilter -v
uv run pytest -x tests/test_api_auth.py -v  # 回归 require_scopes 不被破坏
uv run ruff check api/auth.py
uv run pyright
```

### 依赖

T2（`TokenResourceFilter` 已存在）。

---

## Task 4: GET 路由迁移 — `list_messages` / `get_message` / `list_subs` 行级过滤

### 目标

把 3 个 GET 路由从 `Security(require_scopes)` 切到 `Security(get_resource_filter)`，
并在响应里叠加行级过滤。

### 涉及文件:行号

- `api/routes/messages.py:95-138` — `list_messages`
- `api/routes/messages.py:146-158` — `get_message`
- `api/routes/subscriptions.py:39-47` — `list_subs`
- `tests/test_api_messages.py`、`tests/test_api_subscriptions.py`

### TDD

**先写测试**（`tests/test_api_messages.py` 追加 `TestRowLevelGet`）：

```python
class TestRowLevelGet:
    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:read"], "platforms": ["bili"]}],
        indirect=True,
    )
    async def test_list_messages_filters_by_platform(self, row_filtered_client, tmp_data_dir_with_mixed_msgs):
        """token 只允许 bili → response 只含 bili 消息。"""
        # tmp_data_dir_with_mixed_msgs 写入 bili + xhs 各一条消息
        resp = await row_filtered_client.get("/api/v1/messages")
        assert resp.status_code == 200
        platforms = {m["platform"] for m in resp.json()["messages"]}
        assert platforms == {"bili"}

    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:read"], "subscription_refs": ["bili:100"]}],
        indirect=True,
    )
    async def test_list_messages_filters_by_subscription(self, row_filtered_client, tmp_data_dir_with_mixed_msgs):
        """token 只允许 bili:100 → response 只含 uid=100 的消息。"""
        ...

    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:read"], "platforms": ["xhs"]}],
        indirect=True,
    )
    async def test_get_message_unauthorized_platform_returns_404(
        self, row_filtered_client, tmp_data_dir_with_mixed_msgs
    ):
        """越权平台 → 404（不暴露存在性）。"""
        # tmp_data_dir_with_mixed_msgs 含 bili 消息
        resp = await row_filtered_client.get("/api/v1/messages/bili:BV1xx")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:read"], "subscription_refs": ["bili:200"]}],
        indirect=True,
    )
    async def test_get_message_unauthorized_subscription_returns_404(
        self, row_filtered_client, tmp_data_dir_with_mixed_msgs
    ):
        """越权订阅 → 404。"""
        ...

    async def test_unrestricted_token_no_filter(self, authed_client, tmp_data_dir_with_mixed_msgs):
        """全权限 token 不过滤（兼容性回归）。"""
        ...
```

类似地 `tests/test_api_subscriptions.py` 加 `TestRowLevelListSubs`：

- token 只允许 bili → response 只含 `bilibili` section
- token 只允许 `bili:100` → response.bilibili 列表里只含 uid=100 那条

**再写实现**：

1. **`api/routes/messages.py:list_messages`**（line 95-138）：

   ```python
   from api.auth import get_resource_filter
   from api.resource_filter import TokenResourceFilter

   @router.get("/messages", response_model=MessageListResponse)
   async def list_messages(
       request: Request,
       since: str | None = Query(None),
       ...
       filt: TokenResourceFilter = Security(get_resource_filter, scopes=["messages:read"]),
   ) -> MessageListResponse:
       ...
       matched = store.query_messages(since=since_ts, title=title, author=author, platform=platform, phase=phase_enum)
       matched = [m for m in matched if filt.allows_message(m)]  # 新增行级过滤
       return MessageListResponse(...)
   ```

2. **`api/routes/messages.py:get_message`**（line 146-158）：

   ```python
   rec = store.get_message(msg_id)
   if rec is None or not filt.allows_message(rec):
       raise HTTPException(status_code=404, detail="message not found")
   return _record_to_out(rec)
   ```

3. **`api/routes/subscriptions.py:list_subs`**（line 39-47）：

   ```python
   result = await list_subscriptions(platform=platform)
   result = _filter_subscriptions(result, filt)  # 新增 helper
   return SubscriptionListResponse(platforms=result)
   ```

   `_filter_subscriptions` 在本文件内或 `api/resource_filter.py` 实现（推荐后者，
   与 `TokenResourceFilter` 同模块，按 spec §7.3 给出）：

   ```python
   # 注意：SECTION_TO_SHORT 在 api/resource_filter.py 顶部统一定义（见 T2），
   # 这里 import 复用，禁止 inline 重定义（DRY，与 core.subscription_cli.PLATFORM_TO_SECTION 对齐）。
   from api.resource_filter import SECTION_TO_SHORT, SHORT_TO_KEY_FIELD

   def filter_subscription_dict(
       result: dict[str, list[dict]], filt: TokenResourceFilter
   ) -> dict[str, list[dict]]:
       out: dict[str, list[dict]] = {}
       for section, subs in result.items():
           short = SECTION_TO_SHORT.get(section)
           if short is None or not filt.allows_platform(short):
               continue
           if filt.subscription_refs is None:
               out[section] = subs
               continue
           key_field = SHORT_TO_KEY_FIELD.get(short, "")
           kept = []
           for s in subs:
               sub_id = str(s.get(key_field, ""))
               if filt.allows_subscription(short, sub_id):
                   kept.append(s)
           if kept:
               out[section] = kept
       return out
   ```

### 验证

```bash
uv run pytest -x tests/test_api_messages.py::TestRowLevelGet -v
uv run pytest -x tests/test_api_subscriptions.py::TestRowLevelListSubs -v
uv run pytest -x tests/test_api_messages.py tests/test_api_subscriptions.py -v  # 回归
uv run ruff check api/routes/messages.py api/routes/subscriptions.py api/resource_filter.py
uv run pyright
```

### 依赖

T3（`get_resource_filter` 已存在）。

---

## Task 5: 写入路由迁移 — `rerun` / `fetch` / `delete_sub` / `bind/unbind` 越权处理

### 目标

写入路由对越权 id 返回 404（messages）或 `success=False`（subscriptions），
不暴露存在性。

### 涉及文件:行号

- `api/routes/messages.py:166-263` — `rerun_messages`
- `api/routes/messages.py:271-360` — `fetch_messages`
- `api/routes/subscriptions.py:50-86` — `add_sub` / `remove_sub`
- `api/routes/subscriptions.py:94-139` — `bind_endpoint` / `unbind_endpoint`
- `tests/test_api_messages.py`、`tests/test_api_subscriptions.py`、`tests/test_api_fetch.py`

### TDD

**先写测试**：

```python
# tests/test_api_messages.py
class TestRowLevelRerun:
    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:write"], "platforms": ["bili"]}],
        indirect=True,
    )
    async def test_rerun_all_unauthorized_returns_404(self, row_filtered_client, tmp_data_dir_with_mixed_msgs):
        """全部 msg_id 越权 → 404。"""
        resp = await row_filtered_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": ["xhs:note1", "xhs:note2"], "from_phase": "detected"},
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:write"], "platforms": ["bili"]}],
        indirect=True,
    )
    @patch("api.routes.messages.PipelineEngine")
    async def test_rerun_partial_unauthorized_silently_skipped(
        self, row_filtered_client, tmp_data_dir_with_mixed_msgs, mock_engine
    ):
        """部分越权 → 202，reset_count 只含合法的，且只把合法 id 传给后台 task。"""
        resp = await row_filtered_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": ["bili:BV1xx", "xhs:note1"], "from_phase": "detected"},
        )
        assert resp.status_code == 202
        assert resp.json()["reset_count"] == 1
        # M4：确认只把 authorized id 传给后台 task（越权 id 被过滤掉，不泄漏到 pipeline）
        mock_run_specific = mock_engine.run_specific_messages
        mock_run_specific.assert_called_once()
        called_kwargs = mock_run_specific.call_args.kwargs
        assert called_kwargs["msg_ids"] == ["bili:BV1xx"]  # 只含 authorized id


# tests/test_api_fetch.py
class TestRowLevelFetch:
    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:write"], "platforms": ["bili"]}],
        indirect=True,
    )
    async def test_fetch_all_unauthorized_returns_404(self, row_filtered_client):
        """fetch msg_id 前缀平台全部被禁 → 404。"""
        resp = await row_filtered_client.post(
            "/api/v1/messages/fetch",
            json={"msg_ids": ["xhs:note1"], "skip_push": False},
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["messages:write"], "platforms": ["bili"]}],
        indirect=True,
    )
    async def test_fetch_partial_unauthorized_filtered(self, row_filtered_client):
        """部分越权 id 被静默过滤掉（只跑合法的）。"""
        ...


# tests/test_api_subscriptions.py
class TestRowLevelSubsWrite:
    @pytest.mark.parametrize(
        "row_filtered_client",
        [{"scopes": ["subscriptions:write"], "platforms": ["bili"]}],
        indirect=True,
    )
    async def test_delete_sub_unauthorized_returns_success_false(
        self, row_filtered_client
    ):
        """越权删除 → 200 + success=False（与未找到语义合并）。"""
        resp = await row_filtered_client.delete("/api/v1/subscriptions/xhs/u456")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
```

**再写实现**：

1. **`rerun_messages`**（line 166-263）：

   ```python
   existing = [
       m for m in (store.get_message(mid) for mid in body.msg_ids)
       if m is not None and filt.allows_message(m)
   ]
   reset_count = len(existing)
   if reset_count == 0:
       raise HTTPException(status_code=404, detail="message not found")
   ```

   注意：`run_specific_messages` 仍传**完整** `body.msg_ids`？还是传过滤后的？
   **答**：传过滤后的合法 msg_ids，避免后台 task 处理越权消息。需要从
   `existing` 提取 msg_id：

   ```python
   authorized_msg_ids = [m.msg_id for m in existing]
   ...
   async def _rerun() -> None:
       try:
           await PipelineEngine.run_specific_messages(
               msg_ids=authorized_msg_ids,  # 改这里
               ...
           )
   ```

2. **`fetch_messages`**（line 271-360）：

   ```python
   # 按 msg_id 前缀过滤
   authorized_ids = [mid for mid in body.msg_ids if _msg_id_platform_allowed(mid, filt)]
   if not authorized_ids:
       raise HTTPException(status_code=404, detail="message not found")
   ...
   async def _fetch() -> None:
       try:
           await PipelineEngine.run_fetch_and_process(
               msg_ids=authorized_ids,  # 改这里
               ...
           )
   ```

   `_msg_id_platform_allowed` 放在 `api/resource_filter.py` 或本文件 helper。

3. **`remove_sub`**（subscriptions.py:72-86）：

   越权判断抽成 `api/resource_filter.py` 的 helper（I3），三个订阅写入路由
   （`remove_sub` / `bind_endpoint` / `unbind_endpoint`）统一调用，避免重复
   inline 越权逻辑。helper 在 T2 模块顶部常量已就绪后定义：

   ```python
   # api/resource_filter.py（追加，紧跟 filter_subscription_dict 之后）
   def subscription_visible(
       filt: TokenResourceFilter, platform_full: str, identifier: str | int
   ) -> bool:
       """订阅是否在 token 的行级权限内。

       platform_full 是路由 URL 段的 TOML section 全名（如 ``bilibili``），
       identifier 是订阅主键（uid / user_id）。内部走 SECTION_TO_SHORT 反查
       short name 再交给 ``filt.allows_subscription``。
       """
       short = SECTION_TO_SHORT.get(platform_full)
       if short is None:
           return False
       return filt.allows_subscription(short, str(identifier))
   ```

   `remove_sub` 改为：

   ```python
   from api.resource_filter import subscription_visible

   async def remove_sub(
       platform: str,
       identifier: str,
       request: Request,
       filt: TokenResourceFilter = Security(get_resource_filter, scopes=["subscriptions:write"]),
   ) -> SubscriptionRemoveResponse:
       if not subscription_visible(filt, platform, identifier):
           # 与「未找到」语义合并，不暴露存在性（spec §7 表 + §8.3）
           return SubscriptionRemoveResponse(success=False, message="未找到: 订阅不存在或无权访问")
       success, message = await remove_subscription(platform, identifier)
       return SubscriptionRemoveResponse(success=success, message=message)
   ```

   注意：`Security(get_resource_filter, scopes=["subscriptions:write"])` 替换
   原 `Security(require_scopes, ...)`。路由 URL 的 `{platform}` 段是 TOML
   section 全名（如 `/subscriptions/bilibili/123`，与 #103 一致），
   `subscription_visible` 内部用 `SECTION_TO_SHORT`（T2 已从
   `core.subscription_cli.PLATFORM_TO_SECTION` 反推）做全名→short 映射，
   **禁止在路由文件里再 inline 一份 `_full_to_short` / `{"bilibili": "bili", ...}`**。

4. **`bind_endpoint` / `unbind_endpoint`**（subscriptions.py:94-139）：同 `remove_sub`
   模式，统一调 `subscription_visible(filt, platform, identifier)`。三个路由的
   越权响应 message 文本保持一致（按 spec §7 表：`remove_sub` →
   `"未找到: 订阅不存在或无权访问"`，`bind/unbind` → `"未找到订阅"`）。

### 验证

```bash
uv run pytest -x tests/test_api_messages.py::TestRowLevelRerun -v
uv run pytest -x tests/test_api_fetch.py::TestRowLevelFetch -v
uv run pytest -x tests/test_api_subscriptions.py::TestRowLevelSubsWrite -v
uv run pytest -x tests/test_api_messages.py tests/test_api_fetch.py tests/test_api_subscriptions.py -v
uv run ruff check api/routes/
uv run pyright
```

### 依赖

T3、T4（`get_resource_filter` + `filter_subscription_dict` 等 helper 已就绪）。

---

## Task 6: CLI 扩展 — `--resource-platform` / `--resource-sub` multi-flag + list 显示

### 目标

`api/token_tool.py` 支持创建带行级规则的 token，`list` 命令显示规则。

### 涉及文件:行号

- `api/token_tool.py:47-94` — `create` 命令
- `api/token_tool.py:97-117` — `list` 命令
- `api/auth.py:create_token` — 已在 T1 加 `resource_rules` 参数
- `tests/test_api_token_tool.py`

### TDD

**先写测试**（`tests/test_api_token_tool.py` 追加）：

```python
class TestCreateResourceRules:
    def test_create_with_resource_platform(self, tmp_path, monkeypatch):
        result = CliRunner().invoke(
            cli, ["create", "bili-bot", "--scope", "messages:read", "--resource-platform", "bili"]
        )
        assert result.exit_code == 0
        cfg = load_auth_config()
        token = next(t for t in cfg.api_tokens if t.name == "bili-bot")
        assert token.resource_rules.platforms == ["bili"]
        assert token.resource_rules.subscription_refs is None

    def test_create_with_multiple_resource_sub(self, tmp_path, monkeypatch):
        result = CliRunner().invoke(
            cli, ["create", "up-bot", "--scope", "messages:read",
                  "--resource-sub", "bili:100", "--resource-sub", "xhs:u456"]
        )
        assert result.exit_code == 0
        cfg = load_auth_config()
        token = next(t for t in cfg.api_tokens if t.name == "up-bot")
        assert token.resource_rules.subscription_refs == ["bili:100", "xhs:u456"]

    def test_create_invalid_platform_rejected(self, tmp_path, monkeypatch):
        result = CliRunner().invoke(
            cli, ["create", "x", "--resource-platform", "invalid"]
        )
        assert result.exit_code != 0

    def test_create_invalid_sub_format_rejected(self, tmp_path, monkeypatch):
        result = CliRunner().invoke(
            cli, ["create", "x", "--resource-sub", "no-colon"]
        )
        assert result.exit_code != 0

    def test_create_warns_on_meaningless_combination(self, tmp_path, monkeypatch, capsys):
        """platforms=[bili] + subs=[xhs:u456] 输出 warning（不阻止创建）。"""
        result = CliRunner().invoke(
            cli, ["create", "x", "--resource-platform", "bili", "--resource-sub", "xhs:u456"]
        )
        assert result.exit_code == 0
        assert "warning" in result.output.lower() or "⚠" in result.output


class TestListResourceRules:
    def test_list_shows_resource_rules_column(self, tmp_path, monkeypatch):
        # 先创建几个 token
        ...
        result = CliRunner().invoke(cli, ["list"])
        assert "Resource Rules" in result.output
        assert "platforms=[bili]" in result.output
        assert "(unrestricted)" in result.output
```

**再写实现**：

1. `api/token_tool.py:create` 加两个 option：

   校验顺序（与现有 `api/token_tool.py:65-80` 对齐，避免行为漂移）：
   1. scope 白名单校验（已有，防拼写错误）
   2. resource platform 白名单校验（新增）
   3. resource sub 格式校验（新增）
   4. 无意义组合 warning（新增，不阻止）
   5. `_token_exists` / `--force` 同名覆盖检查（已有）
   6. `create_token(...)` 落盘（已有）

   ```python
   @click.option(
        "--resource-platform",
        "resource_platforms",
        multiple=True,
        help="限制 token 可访问平台（可多次）。合法: bili, xhs, weibo。不指定 = 不限。",
    )
    @click.option(
        "--resource-sub",
        "resource_subs",
        multiple=True,
        help="限制 token 可访问订阅（<short>:<id>，可多次）。如 bili:100。",
    )
    def create(name, force, scopes, resource_platforms, resource_subs):
        ...
        # 1. scope 白名单校验（已有，保持原位置）
        invalid = [s for s in scopes if s not in ALL_SCOPES]
        if invalid:
            ...  # 原逻辑

        # 2. resource platform 白名单
        VALID_PLATFORMS_SHORT = {"bili", "xhs", "weibo"}
        invalid_p = [p for p in resource_platforms if p not in VALID_PLATFORMS_SHORT]
        if invalid_p:
            console.print(f"[red]✗[/] 未知 platform: {', '.join(invalid_p)}", style="red")
            console.print(f"[dim]合法: {', '.join(sorted(VALID_PLATFORMS_SHORT))}[/]")
            sys.exit(1)

        # 3. resource sub 格式校验
        invalid_s = [s for s in resource_subs if ":" not in s or s.split(":", 1)[0] not in VALID_PLATFORMS_SHORT]
        if invalid_s:
            console.print(f"[red]✗[/] 非法 subscription_ref 格式: {', '.join(invalid_s)}", style="red")
            console.print("[dim]格式: <platform_short>:<id>，如 bili:100 / xhs:u456[/]")
            sys.exit(1)

        # 4. 无意义组合 warning（不阻止）
        if resource_platforms and resource_subs:
            declared_platforms_in_subs = {s.split(":", 1)[0] for s in resource_subs}
            if not declared_platforms_in_subs.intersection(resource_platforms):
                console.print(
                    "[yellow]⚠️[/] platforms 与 subscription_refs 无交集，"
                    "token 将拒绝所有资源",
                    style="yellow",
                )

        # 5. _token_exists / --force（已有，保持原位置）
        if _token_exists(name) and not force:
            ...

        # 6. 落盘
        rules = ResourceRules(
            platforms=list(resource_platforms) if resource_platforms else None,
            subscription_refs=list(resource_subs) if resource_subs else None,
        )
        plain = create_token(name, scopes=scope_list, resource_rules=rules)
        ...
    ```

2. `list_cmd` 加 `Resource Rules` 列：

   ```python
   table.add_column("Resource Rules")
   for t in cfg.api_tokens:
       rules = t.resource_rules
       if rules.platforms is None and rules.subscription_refs is None:
           rules_str = "(unrestricted)"
       else:
           parts = []
           if rules.platforms is not None:
               parts.append(f"platforms=[{', '.join(rules.platforms)}]")
           if rules.subscription_refs is not None:
               parts.append(f"subs=[{', '.join(rules.subscription_refs)}]")
           rules_str = " ".join(parts)
       row = [t.name, t.token_hash[:8], created, scopes_str, rules_str]
       table.add_row(*row)
   ```

### 验证

```bash
uv run pytest -x tests/test_api_token_tool.py -v
uv run ruff check api/token_tool.py
uv run pyright
```

### 依赖

T1（`create_token` 已接受 `resource_rules` 参数）。

---

## Task 7: 测试补全 — `row_filtered_client` fixture + 全场景覆盖

### 目标

新增 `row_filtered_client` fixture，覆盖 5 种行级场景，确保各路由行为一致。

### 涉及文件:行号

- `tests/conftest.py`（或每个测试文件独立 fixture，与 #103 `scoped_client` 风格一致）
- `tests/test_api_messages.py`、`tests/test_api_subscriptions.py`、`tests/test_api_fetch.py`、`tests/test_api_auth.py`

### TDD

**先写 fixture**（与 `scoped_client` 同模式）：

```python
# 放 tests/test_api_messages.py 或 conftest.py
@pytest.fixture
async def row_filtered_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncClient:
    """带指定 resource_rules 的 client。

    request.param 形如：
        {"scopes": [...], "platforms": [...], "subscription_refs": [...]}
    缺省字段 = None（不限）。
    """
    params = request.param
    scopes = params.get("scopes", [])
    platforms = params.get("platforms")
    subs = params.get("subscription_refs")

    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token
    from shared.config import ResourceRules

    app = create_app()
    plain = create_token(
        "row-bot",
        scopes=scopes,
        resource_rules=ResourceRules(platforms=platforms, subscription_refs=subs),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        yield c
```

**数据 fixture**（I4）：`tmp_data_dir_with_mixed_msgs` 构造 4-6 条覆盖
`bili:100` / `bili:200` / `xhs:u456` 的 `MessageRecord`，落盘到 tmp_path 的
`messages.json`，供行级过滤矩阵测试断言返回集合。**统一命名**：全文只用
`tmp_data_dir_with_mixed_msgs`（T4/T5 测试签名已统一为本名，不再出现裸
`tmp_data_dir`）。

```python
@pytest.fixture
def tmp_data_dir_with_mixed_msgs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """落盘 4-6 条混合平台/订阅的 MessageRecord 到 messages.json。

    覆盖矩阵：
      - bili:100 （platform=bili, subscription_ref=100）
      - bili:200 （platform=bili, subscription_ref=200）
      - xhs:u456 （platform=xhs, subscription_ref=u456）
      - bili:300 （platform=bili, subscription_ref=300，用于「部分越权」场景）

    这样 5 种 resource_rules 场景都能在返回集合上做出可区分断言。
    """
    import json
    import time

    from shared.protocols import ContentType, MessageRecord, Phase

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("shared.message_store.DATA_DIR", data_dir)

    now = time.time()
    records = [
        MessageRecord(
            msg_id="bili:100", platform="bili",
            content_type=ContentType.VIDEO, phase=Phase.SUMMARIZED,
            pubdate=int(now), title="bili-100", author="a",
            subscription_ref="100", created_at=now, updated_at=now,
        ),
        MessageRecord(
            msg_id="bili:200", platform="bili",
            content_type=ContentType.VIDEO, phase=Phase.SUMMARIZED,
            pubdate=int(now), title="bili-200", author="a",
            subscription_ref="200", created_at=now, updated_at=now,
        ),
        MessageRecord(
            msg_id="xhs:u456", platform="xhs",
            content_type=ContentType.TEXT, phase=Phase.SUMMARIZED,
            pubdate=int(now), title="xhs-u456", author="a",
            subscription_ref="u456", created_at=now, updated_at=now,
        ),
        MessageRecord(
            msg_id="bili:300", platform="bili",
            content_type=ContentType.VIDEO, phase=Phase.SUMMARIZED,
            pubdate=int(now), title="bili-300", author="a",
            subscription_ref="300", created_at=now, updated_at=now,
        ),
    ]
    payload = {
        "messages": [json.loads(r.to_json()) for r in records],
        "version": 1,
    }
    (data_dir / "messages.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return data_dir
```

> 注：`MessageRecord.to_json()` 的确切方法名以 `shared/protocols.py` 实际
> 提供的序列化接口为准；若 dataclass 无 `to_json`，改用
> `dataclasses.asdict(r)` + `json.dumps`。实现时核对 `MessageStore.save`
> 的落盘格式，保证 fixture 写出的 JSON 与 store 读回的格式一致。

**新增 5 类场景测试**（覆盖矩阵）：

| 场景 | platforms | subscription_refs | 期望行为 |
|------|-----------|-------------------|---------|
| 全权限（兼容） | None | None | 所有路由不过滤 |
| 仅 platform 限制 | `["bili"]` | None | GET list 只返 bili；GET 单条越权 → 404 |
| 仅 subscription 限制 | None | `["bili:100"]` | GET list 只返 uid=100；GET 单条其他 → 404 |
| AND 组合 | `["bili"]` | `["bili:100"]` | GET 只返 bili uid=100 |
| 空 list（拒绝一切） | `[]` | None | GET list 返空；GET 单条 → 404 |

每类场景对每个相关路由（`list_messages` / `get_message` / `rerun` / `fetch` /
`list_subs` / `delete_sub`）都要有覆盖。

**关键测试**：

```python
@pytest.mark.parametrize(
    "row_filtered_client",
    [
        # 全权限
        pytest.param({"scopes": ["messages:read"]}, id="unrestricted"),
        # 仅 platform
        pytest.param({"scopes": ["messages:read"], "platforms": ["bili"]}, id="platform-bili"),
        # 仅 subscription
        pytest.param({"scopes": ["messages:read"], "subscription_refs": ["bili:100"]}, id="sub-bili-100"),
        # AND 组合
        pytest.param(
            {"scopes": ["messages:read"], "platforms": ["bili"], "subscription_refs": ["bili:100"]},
            id="platform-and-sub",
        ),
        # 空 list（拒绝一切）
        pytest.param({"scopes": ["messages:read"], "platforms": []}, id="deny-all"),
    ],
    indirect=True,
)
async def test_list_messages_row_level_matrix(
    self, row_filtered_client, tmp_data_dir_with_mixed_msgs, request
):
    """5 种 resource_rules 场景下 list_messages 的行为。

    tmp_data_dir_with_mixed_msgs 含 4 条: bili:100 / bili:200 / xhs:u456 / bili:300。
    """
    resp = await row_filtered_client.get("/api/v1/messages")
    assert resp.status_code == 200
    msg_ids = {m["msg_id"] for m in resp.json()["messages"]}

    case = request.node.callspec.id
    expected = {
        "unrestricted": {"bili:100", "bili:200", "xhs:u456", "bili:300"},
        "platform-bili": {"bili:100", "bili:200", "bili:300"},  # 排除 xhs
        "sub-bili-100": {"bili:100"},  # 只剩 uid=100
        "platform-and-sub": {"bili:100"},  # bili AND uid=100
        "deny-all": set(),  # platforms=[] 拒绝一切
    }[case]
    assert msg_ids == expected
```

### 验证

```bash
uv run pytest -x tests/ -k "row_level or row_filtered" -v
uv run pytest -x tests/test_api_auth.py tests/test_api_messages.py tests/test_api_subscriptions.py tests/test_api_fetch.py tests/test_api_token_tool.py tests/test_resource_filter.py -v
uv run pytest -x                    # 全量回归
uv run ruff check .
uv run pyright
```

### 依赖

T1-T6 全部完成。

---

## 收尾验证

完成 T1-T7 后跑：

```bash
uv run ruff check .
uv run pyright
uv run pytest -x -v
```

全部通过后，做手动 smoke test（见 spec §12 命令清单），确认 CLI + HTTP 行为
符合预期。

## 提交策略

每个 Task 一个 commit，commit message 格式：

```
feat(api-token): row-level filtering — T1 ResourceRules dataclass + auth.toml
feat(api-token): row-level filtering — T2 TokenResourceFilter
feat(api-token): row-level filtering — T3 get_resource_filter dep
feat(api-token): row-level filtering — T4 GET routes
feat(api-token): row-level filtering — T5 write routes
feat(api-token): row-level filtering — T6 CLI flags
feat(api-token): row-level filtering — T7 test fixtures + matrix
```

最后一个 commit 后 push + 创建 PR。

## 风险提示

- T3 重构 `require_scopes` 时要小心不要破坏现有 12 个路由的 scope 测试 —— TDD
  必须先跑现有 `test_*_insufficient_scope_returns_403` 全绿才能开始改。
- T5 rerun 把 `body.msg_ids` 改成 `authorized_msg_ids` 时，要确认
  `run_specific_messages` 不会因为 id 变少而报错（应该不会，它内部按 id 查
  store）。
- T6 CLI warning 输出不要破坏现有 list 表格格式（rich Table 自动列宽，加一列
  会让老 column 变窄，需要检查）。
- T7 fixture 在 `tests/conftest.py` 共享 vs 每文件独立 —— 与 #103 `scoped_client`
  保持一致（每文件独立，避免 fixture 命名冲突）。
