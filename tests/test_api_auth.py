"""Tests for api.auth (T1) — token 鉴权依赖。

覆盖：
- ``require_token`` FastAPI 依赖：无 header / 格式错 / token 不匹配 → 401
- ``_hash_token`` / ``_verify_token`` 常量时间比对
- ``create_token`` / ``revoke_token`` 持久化到 data/auth.toml

测试通过 monkeypatch ``web.auth.AUTH_TOML_PATH`` 隔离到 tmp_path，
不触碰真实 ``data/auth.toml``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from shared.config import ApiTokenEntry
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
def auth_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp 隔离的 AUTH_TOML_PATH，并 set_password 让 setup 完成。"""
    p = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", p)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", p)
    set_password(PASSWORD)
    return p


class TestHashVerify:
    def test_hash_is_deterministic(self) -> None:
        from api.auth import _hash_token

        h1 = _hash_token("abc123")
        h2 = _hash_token("abc123")
        assert h1 == h2
        # SHA-256 hexdigest 长度 64
        assert len(h1) == 64

    def test_hash_differs_for_different_input(self) -> None:
        from api.auth import _hash_token

        assert _hash_token("abc123") != _hash_token("abc124")

    def test_verify_matches_correct_token(self) -> None:
        from api.auth import _hash_token, _verify_token

        plain = "secret-token-xyz"
        h = _hash_token(plain)
        assert _verify_token(plain, h) is True

    def test_verify_rejects_wrong_token(self) -> None:
        from api.auth import _hash_token, _verify_token

        h = _hash_token("correct")
        assert _verify_token("wrong", h) is False


class TestRequireToken:
    async def test_no_header_returns_401(self) -> None:
        from api.auth import require_token

        # request.headers 为空 dict
        request = SimpleNamespace(headers={})
        with pytest.raises(HTTPException) as exc_info:
            await require_token(request)
        assert exc_info.value.status_code == 401

    async def test_malformed_scheme_returns_401(self, auth_path: Path) -> None:
        """Authorization: Token xxx（非 Bearer scheme）→ 401。"""
        from api.auth import require_token

        request = SimpleNamespace(headers={"authorization": "Token xxx"})
        with pytest.raises(HTTPException) as exc_info:
            await require_token(request)
        assert exc_info.value.status_code == 401

    async def test_bearer_with_wrong_token_returns_401(self, auth_path: Path) -> None:
        """格式对（Bearer）但 token 不匹配任何已存 token → 401。"""
        from api.auth import create_token, require_token

        # 先创建一个 token 让 auth.toml 有内容（但用错误 token 访问）
        create_token("real-bot")
        request = SimpleNamespace(headers={"authorization": "Bearer wrong-token-value"})
        with pytest.raises(HTTPException) as exc_info:
            await require_token(request)
        assert exc_info.value.status_code == 401

    async def test_valid_token_passes_and_returns_name(self, auth_path: Path) -> None:
        """带正确 Bearer → 不抛异常，返回 token name。"""
        from api.auth import create_token, require_token

        plain = create_token("my-bot")
        request = SimpleNamespace(headers={"authorization": f"Bearer {plain}"})
        name = await require_token(request)
        assert name == "my-bot"

    async def test_no_tokens_configured_returns_401(self, auth_path: Path) -> None:
        """auth.toml 无 api_tokens 段 → 任何 token 都 401。"""
        from api.auth import require_token

        request = SimpleNamespace(headers={"authorization": "Bearer any-token"})
        with pytest.raises(HTTPException) as exc_info:
            await require_token(request)
        assert exc_info.value.status_code == 401


class TestCreateRevokeToken:
    def test_create_token_returns_plaintext_and_persists_hash(
        self, auth_path: Path
    ) -> None:
        from api.auth import _hash_token, create_token
        from web.auth import load_auth_config

        plain = create_token("test-bot")
        # 明文长度合理（token_urlsafe(32) → ~43 chars）
        assert isinstance(plain, str)
        assert len(plain) >= 30

        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 1
        entry = cfg.api_tokens[0]
        assert entry.name == "test-bot"
        # 存的是 hash，不是明文
        assert entry.token_hash == _hash_token(plain)
        assert entry.token_hash != plain
        # created_at 是合理的 unix ts（>0）
        assert entry.created_at > 0

    def test_create_token_overwrites_same_name(self, auth_path: Path) -> None:
        from api.auth import create_token
        from web.auth import load_auth_config

        plain1 = create_token("bot")
        plain2 = create_token("bot")  # 覆盖
        assert plain1 != plain2
        cfg = load_auth_config()
        # 只有 1 条（覆盖，不重复）
        assert len([t for t in cfg.api_tokens if t.name == "bot"]) == 1

    def test_revoke_existing_token(self, auth_path: Path) -> None:
        from api.auth import create_token, revoke_token
        from web.auth import load_auth_config

        create_token("bot-1")
        assert revoke_token("bot-1") is True
        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 0

    def test_revoke_nonexistent_returns_false(self, auth_path: Path) -> None:
        from api.auth import revoke_token

        assert revoke_token("never-existed") is False

    def test_revoke_does_not_touch_other_tokens(self, auth_path: Path) -> None:
        from api.auth import create_token, revoke_token
        from web.auth import load_auth_config

        create_token("keep")
        create_token("drop")
        assert revoke_token("drop") is True
        cfg = load_auth_config()
        names = [t.name for t in cfg.api_tokens]
        assert names == ["keep"]

    def test_save_auth_config_preserves_password_hash(self, auth_path: Path) -> None:
        """create_token 写 auth.toml 不能擦掉 admin_password_hash（与密码同文件）。"""
        from api.auth import create_token
        from web.auth import load_auth_config

        before = load_auth_config().admin_password_hash
        create_token("bot")
        after = load_auth_config().admin_password_hash
        assert before == after
        assert before  # 非空（set_password 已设）

    def test_load_auth_config_roundtrip_empty_tokens(self, auth_path: Path) -> None:
        """无 api_tokens 段的 auth.toml 加载后 api_tokens 为空列表。"""
        from web.auth import load_auth_config

        cfg = load_auth_config()
        assert cfg.api_tokens == []


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

    def test_token_has_scope_empty_scopes_denies_all(self) -> None:
        """token.scopes == [] → 任何 required 都拒绝（issue #108 破坏性变更）。

        旧版（#105）「空 = 全权限」已废弃，#108 后空 = 无权。
        完整覆盖见 ``TestTokenHasScopeEmptyScopes``。
        """
        from api.auth import ApiTokenEntry, token_has_scope

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, "messages:read") is False
        assert token_has_scope(token, "check:run") is False
        assert token_has_scope(token, "subscriptions:write") is False

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
        from fastapi.security import SecurityScopes

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
        from fastapi.security import SecurityScopes

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
        from fastapi.security import SecurityScopes

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
        from fastapi.security import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("writer", scopes=["messages:write"])
        security = SecurityScopes(scopes=["messages:read"])
        request = self._make_request(plain)
        name = await require_scopes(security, request)
        assert name == "writer"

    async def test_empty_security_scopes_acts_like_require_token(
        self, auth_path: Path
    ) -> None:
        """路由不要求 scope（SecurityScopes.scopes == ()）→ 只校验身份。"""
        from fastapi.security import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("any-bot", scopes=["messages:read"])
        security = SecurityScopes(scopes=[])  # 路由不要求 scope
        request = self._make_request(plain)
        name = await require_scopes(security, request)
        assert name == "any-bot"

    async def test_empty_scopes_token_denied(
        self, auth_path: Path
    ) -> None:
        """空 scope token 在 #108 后任何 required scope 都被拒（403）。"""
        from fastapi.security import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("empty-bot")  # 不传 scopes → []
        for req in ["messages:read", "messages:write", "check:run",
                    "subscriptions:write"]:
            security = SecurityScopes(scopes=[req])
            request = self._make_request(plain)
            with pytest.raises(HTTPException) as exc:
                await require_scopes(security, request)
            assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════════
# token_has_scope 空 scopes 语义（issue #108 破坏性变更）
# ═══════════════════════════════════════════════════════════


class TestTokenHasScopeEmptyScopes:
    """#108 后空 scopes 不再 = 全权限（spec §6.2）。"""

    def test_empty_scopes_denies_messages_read(self) -> None:
        """空 scopes token 对 messages:read 返回 False（#105 是 True，#108 改 False）。"""
        from api.auth import SCOPE_MESSAGES_READ, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, SCOPE_MESSAGES_READ) is False

    def test_empty_scopes_denies_subscriptions_write(self) -> None:
        from api.auth import SCOPE_SUBSCRIPTIONS_WRITE, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, SCOPE_SUBSCRIPTIONS_WRITE) is False

    def test_empty_scopes_denies_tokens_manage(self) -> None:
        """空 scopes 连 tokens:manage 都没有 → 不是 superuser。"""
        from api.auth import SCOPE_TOKENS_MANAGE, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, SCOPE_TOKENS_MANAGE) is False

    def test_tokens_manage_grants_superuser_scope(self) -> None:
        """持 tokens:manage 的 token 对 tokens:manage 返回 True（superuser 标识）。"""
        from api.auth import SCOPE_TOKENS_MANAGE, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(
            name="admin", token_hash="h", scopes=["tokens:manage"]
        )
        assert token_has_scope(token, SCOPE_TOKENS_MANAGE) is True


# ═══════════════════════════════════════════════════════════
# get_token_ownership FastAPI 依赖（issue #108，替代 #106 get_resource_filter）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
async def superuser_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """持 tokens:manage 的 superuser client（#108 后空 scopes 无权）。

    附带 ``messages:read`` scope：``/api/v1/messages`` GET 需要 messages:read，
    tokens:manage 本身不隐含 messages:read（scope 是独立维度）。
    """
    from httpx import ASGITransport

    from web.app import create_app

    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token("super-bot", scopes=["tokens:manage", "messages:read"])
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestGetTokenOwnership:
    """``get_token_ownership`` FastAPI 依赖用例（issue #108，替代 #106 get_resource_filter）。"""

    def test_get_token_ownership_importable(self) -> None:
        """``get_token_ownership`` 已定义（模块级 import 不应抛异常）。"""
        from api.auth import get_token_ownership  # noqa: F401

    async def test_superuser_token_passes_ownership(
        self, superuser_client: AsyncClient
    ) -> None:
        """持 tokens:manage + messages:read 的 superuser → 不被 ownership 拦截（200）。"""
        resp = await superuser_client.get("/api/v1/messages")
        assert resp.status_code == 200  # 不被 ownership 拦截

    async def test_missing_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 Authorization header → 401（与 require_scopes 一致）。"""
        from httpx import ASGITransport

        from web.app import create_app

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test"
            # 故意不带 Authorization header
        ) as c:
            resp = await c.get("/api/v1/messages")
        assert resp.status_code == 401
        assert "token" in resp.json()["detail"]

    async def test_insufficient_scope_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scope 不够 → 403（ownership 层在 scope 之后，scope 先拦）。"""
        from httpx import ASGITransport

        from api.auth import create_token
        from web.app import create_app

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

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

    async def test_openapi_docs_include_scopes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenAPI schema 在新依赖写法下能正常生成。"""
        from httpx import ASGITransport  # noqa: F401

        from web.app import create_app

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        app = create_app()
        schema = app.openapi()
        paths = schema["paths"]
        assert "/api/v1/messages" in paths
        assert "get" in paths["/api/v1/messages"]
