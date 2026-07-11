from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestTokensPage:
    @patch("web.routes.tokens.load_auth_config")
    async def test_list_page_returns_200(self, mock_load, client: AsyncClient) -> None:
        from shared.config import WebAuthConfig

        mock_load.return_value = WebAuthConfig(api_tokens=[])
        resp = await client.get("/tokens")
        assert resp.status_code == 200
        assert "API Token" in resp.text

    @patch("web.routes.tokens.load_auth_config")
    async def test_list_shows_tokens(self, mock_load, client: AsyncClient) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_load.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(
                    name="admin",
                    token_hash="a1b2c3d4e5f6...",
                    created_at=1720600000.0,
                    scopes=["tokens:manage"],
                ),
                ApiTokenEntry(
                    name="viewer",
                    token_hash="e5f6g7h8...",
                    created_at=1720500000.0,
                    scopes=["subscriptions:read"],
                ),
            ]
        )
        resp = await client.get("/tokens")
        assert resp.status_code == 200
        assert "admin" in resp.text
        assert "viewer" in resp.text
        assert "a1b2c3d4" in resp.text  # hash 前 8 位
        assert "tokens:manage" in resp.text
        assert "subscriptions:read" in resp.text

    @patch("web.routes.tokens.create_token", return_value="trawler_test_plain")
    async def test_plaintext_banner_appears_with_session(self, mock_create, client: AsyncClient) -> None:
        """POST /tokens/create 写入 session flash，首次 GET 渲染明文 banner，
        第二次 GET banner 消失（session pop，一次性显示）。
        覆盖 Task 2 的模板 banner block + Task 3 的 session flash 写入。"""
        # POST 触发 create → session flash 写入
        resp = await client.post(
            "/tokens/create",
            data={"name": "flash-test", "scopes": ["subscriptions:read"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        # 跟随 redirect：cookies 携带 session，首次 GET 渲染 banner
        follow = await client.get("/tokens")
        assert follow.status_code == 200
        assert "trawler_test_plain" in follow.text
        assert "flash-test" in follow.text
        # 第二次 GET：session 已 pop，banner 消失（一次性显示）
        follow2 = await client.get("/tokens")
        assert follow2.status_code == 200
        assert "trawler_test_plain" not in follow2.text


class TestTokenCreate:
    @patch("web.routes.tokens.create_token")
    async def test_create_redirects_to_tokens(self, mock_create, client: AsyncClient) -> None:
        mock_create.return_value = "trawler_abc123def456"
        resp = await client.post(
            "/tokens/create",
            data={"name": "test-token", "scopes": ["subscriptions:read"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/tokens?toast_key=token.created&type=success"
        mock_create.assert_called_once_with("test-token", ["subscriptions:read"])

    @patch("web.routes.tokens.create_token")
    async def test_create_multiple_scopes(self, mock_create, client: AsyncClient) -> None:
        mock_create.return_value = "trawler_xyz789"
        resp = await client.post(
            "/tokens/create",
            data={"name": "multi", "scopes": ["subscriptions:read", "messages:read", "messages:write"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/tokens?toast_key=token.created&type=success"
        mock_create.assert_called_once_with("multi", ["subscriptions:read", "messages:read", "messages:write"])

    async def test_create_empty_name_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/tokens/create",
            data={"name": "", "scopes": ["subscriptions:read"]},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.name_invalid" in loc
        assert "type=error" in loc


class TestTokenRevoke:
    @patch("web.routes.tokens.revoke_token")
    async def test_revoke_success(self, mock_revoke, client: AsyncClient) -> None:
        mock_revoke.return_value = True
        resp = await client.post(
            "/tokens/revoke",
            data={"token_name": "test-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.revoked" in loc
        assert "type=success" in loc
        mock_revoke.assert_called_once_with("test-token")

    @patch("web.routes.tokens.revoke_token")
    async def test_revoke_not_found(self, mock_revoke, client: AsyncClient) -> None:
        mock_revoke.return_value = False
        resp = await client.post(
            "/tokens/revoke",
            data={"token_name": "nonexistent"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.not_found" in loc
        assert "type=error" in loc


class TestTokenToastKeys:
    """验证新 token.* toast key 在 JS TOAST_KEY_MAP 中存在。

    ``base.html`` 的 TOAST_KEY_MAP 是纯前端 JS 对象；route 层把 toast_key
    拼到 redirect URL 里，前端 JS 查表转中文。如果表里漏了某个 key，
    用户会看到「完成」fallback，体验降级。本测试锁定 8 个新增 token.* key。
    """

    def test_toast_keys_exist(self) -> None:
        import re

        content = Path("web/templates/base.html").read_text()
        # 匹配 TOAST_KEY_MAP 内 'key': 'value' 格式中的 key 部分
        keys_in_map = re.findall(r"'([^']+)':\s*'", content)
        required_keys = [
            "token.created",
            "token.revoked",
            "token.name_invalid",
            "token.not_found",
            "token.assigned",
            "token.assign_failed",
            "token.owner_set",
            "token.owner_failed",
        ]
        for k in required_keys:
            assert k in keys_in_map, f"TOAST_KEY_MAP missing: {k}"
