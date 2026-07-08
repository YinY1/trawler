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

    async def test_empty_scopes_token_passes_any(
        self, auth_path: Path
    ) -> None:
        """空 scope token = 全权限，任何 required scope 都放行（spec §5）。"""
        from fastapi.security import SecurityScopes

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
        from fastapi.security import SecurityScopes

        from api.auth import create_token, require_scopes

        plain = create_token("any-bot", scopes=["messages:read"])
        security = SecurityScopes(scopes=[])  # 路由不要求 scope
        request = self._make_request(plain)
        name = await require_scopes(security, request)
        assert name == "any-bot"


# ═══════════════════════════════════════════════════════════
# 行级过滤数据层（issue #106 — ResourceRules + auth.toml 嵌套）
# ═══════════════════════════════════════════════════════════


class TestResourceRulesData:
    """``ResourceRules`` dataclass + ``ApiTokenEntry.resource_rules`` + auth.toml
    嵌套 table 读写（spec §4 / plan T1）。

    注：项目 ``pyproject.toml`` 已设 ``asyncio_mode = "auto"``，async 测试直接
    ``async def`` 即可，无需 ``@pytest.mark.asyncio``。
    """

    def test_resource_rules_default_is_unrestricted(self) -> None:
        """新 ``ApiTokenEntry`` 默认 ``resource_rules`` 两字段 None（全权限）。"""
        from shared.config import ApiTokenEntry

        entry = ApiTokenEntry(name="x", token_hash="h")
        assert entry.resource_rules.platforms is None
        assert entry.resource_rules.subscription_refs is None

    def test_resource_rules_with_platforms(self) -> None:
        from shared.config import ApiTokenEntry, ResourceRules

        entry = ApiTokenEntry(
            name="x",
            token_hash="h",
            resource_rules=ResourceRules(platforms=["bili"]),
        )
        assert entry.resource_rules.platforms == ["bili"]
        assert entry.resource_rules.subscription_refs is None

    def test_load_auth_config_legacy_no_resource_rules(self, auth_path: Path) -> None:
        """老 auth.toml 无 ``resource_rules`` 字段 → 加载为默认全权限。"""
        # 写入一条老格式 token（无 resource_rules）
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
        assert cfg.api_tokens[0].resource_rules.platforms is None
        assert cfg.api_tokens[0].resource_rules.subscription_refs is None

    def test_load_auth_config_with_resource_rules(self, auth_path: Path) -> None:
        """新格式含 ``[resource_rules]`` → 正确加载嵌套字段。"""
        auth_path.write_text(
            '[[api_tokens]]\n'
            'name = "bili-bot"\n'
            f'token_hash = "{"a" * 64}"\n'
            "created_at = 1717500000.0\n"
            "scopes = [\"messages:read\"]\n"
            "[api_tokens.resource_rules]\n"
            "platforms = [\"bili\"]\n"
            "subscription_refs = [\"bili:100\", \"bili:200\"]\n",
            encoding="utf-8",
        )
        from web.auth import load_auth_config

        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 1
        entry = cfg.api_tokens[0]
        assert entry.resource_rules.platforms == ["bili"]
        assert entry.resource_rules.subscription_refs == ["bili:100", "bili:200"]

    def test_save_auth_config_default_omits_resource_rules(
        self, auth_path: Path
    ) -> None:
        """默认 ``ResourceRules()`` 不写出 ``[resource_rules]`` section（diff 干净）。"""
        from shared.config import ApiTokenEntry, ResourceRules, WebAuthConfig

        from web.auth import save_auth_config

        cfg = WebAuthConfig(
            admin_password_hash="x",
            session_secret="s",
            api_tokens=[
                ApiTokenEntry(
                    name="default",
                    token_hash="abc",
                    resource_rules=ResourceRules(),  # 两字段 None
                )
            ],
        )
        save_auth_config(cfg)
        text = auth_path.read_text(encoding="utf-8")
        assert "resource_rules" not in text

    def test_save_auth_config_round_trip(self, auth_path: Path) -> None:
        """非默认 rules 写出后再加载，字段一致（5 种形态覆盖）。

        形态: platforms only / subs only / both / both None / platforms=[]（空 list）。
        特别：``platforms=[]`` 写盘再读回必须仍是 ``[]``（不是 None）—— 空 list
        是「拒绝一切」语义，与 None=全权限 相反，不能被 tomlkit 丢失。
        """
        from shared.config import (
            ApiTokenEntry,
            ResourceRules,
            WebAuthConfig,
        )

        from web.auth import load_auth_config, save_auth_config

        cases: list[tuple[str, ResourceRules]] = [
            ("platforms-only", ResourceRules(platforms=["bili"])),
            ("subs-only", ResourceRules(subscription_refs=["bili:100", "xhs:u456"])),
            (
                "both",
                ResourceRules(platforms=["bili", "xhs"], subscription_refs=["bili:100"]),
            ),
            ("both-none", ResourceRules(platforms=None, subscription_refs=None)),
            ("empty-platforms", ResourceRules(platforms=[])),
        ]
        for name, rules in cases:
            cfg = WebAuthConfig(
                admin_password_hash="x",
                session_secret="s",
                api_tokens=[
                    ApiTokenEntry(name=name, token_hash="abc", resource_rules=rules)
                ],
            )
            save_auth_config(cfg)
            reloaded = load_auth_config()
            assert len(reloaded.api_tokens) == 1, f"case={name}"
            got = reloaded.api_tokens[0].resource_rules
            assert got.platforms == rules.platforms, f"case={name} platforms mismatch"
            assert (
                got.subscription_refs == rules.subscription_refs
            ), f"case={name} subscription_refs mismatch"

    def test_create_token_with_resource_rules(self, auth_path: Path) -> None:
        """``create_token(resource_rules=...)`` 落盘后能读回。"""
        from shared.config import ResourceRules

        from api.auth import create_token
        from web.auth import load_auth_config

        plain = create_token(
            "bili-only",
            scopes=["messages:read"],
            resource_rules=ResourceRules(platforms=["bili"]),
        )
        assert plain  # 明文非空
        cfg = load_auth_config()
        entry = cfg.api_tokens[0]
        assert entry.name == "bili-only"
        assert entry.resource_rules.platforms == ["bili"]
        assert entry.resource_rules.subscription_refs is None
