from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class TestOwnershipModal:
    @patch("web.routes.subscription_ownership.load_subscriptions")
    @patch("web.routes.subscription_ownership.load_auth_config")
    async def test_modal_returns_html(
        self, mock_auth, mock_subs, client: AsyncClient
    ) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_auth.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin-token", token_hash="aaa", scopes=["tokens:manage"]),
                ApiTokenEntry(name="viewer-token", token_hash="bbb", scopes=["subscriptions:read"]),
            ]
        )
        # `load_subscriptions` returns a list of subscription dicts
        mock_subs.return_value = {
            "bilibili": [
                {"uid": 1, "name": "UP主", "owner_token": "admin-token", "assigned_tokens": ["viewer-token"]}
            ]
        }
        resp = await client.get("/subscriptions/bili/1/ownership")
        assert resp.status_code == 200
        assert "admin-token" in resp.text  # current owner in dropdown
        assert "viewer-token" in resp.text  # assigned token shown
        assert "UP主" in resp.text

    @patch("web.routes.subscription_ownership.load_auth_config")
    @patch("web.routes.subscription_ownership.load_subscriptions")
    async def test_modal_orphan_shows_no_owner(
        self, mock_subs, mock_auth, client: AsyncClient
    ) -> None:
        from shared.config import ApiTokenEntry, WebAuthConfig

        mock_auth.return_value = WebAuthConfig(
            api_tokens=[
                ApiTokenEntry(name="admin-token", token_hash="aaa", scopes=["tokens:manage"]),
            ]
        )
        mock_subs.return_value = {
            "bilibili": [
                {"uid": 2, "name": "孤儿UP"}
            ]
        }
        resp = await client.get("/subscriptions/bili/2/ownership")
        assert resp.status_code == 200
        assert "孤儿" in resp.text or "无" in resp.text


class TestTokenAssign:
    @patch("web.routes.subscription_ownership.assign_token_to_subscription", new_callable=AsyncMock)
    async def test_assign_success(self, mock_assign, client: AsyncClient) -> None:
        mock_assign.return_value = (True, "已分配: viewer-token")
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.assign_token_to_subscription", new_callable=AsyncMock)
    async def test_assign_failure(self, mock_assign, client: AsyncClient) -> None:
        mock_assign.return_value = (False, "未知 token: bad")
        resp = await client.post(
            "/subscriptions/bili/1/assign",
            data={"token_name": "bad"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assign_failed" in loc
        assert "type=error" in loc

    @patch("web.routes.subscription_ownership.unassign_token_from_subscription", new_callable=AsyncMock)
    async def test_unassign_success(self, mock_unassign, client: AsyncClient) -> None:
        mock_unassign.return_value = (True, "已解绑: viewer-token")
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assigned" in loc  # 复用 success key
        assert "type=success" in loc

    @patch("web.routes.subscription_ownership.unassign_token_from_subscription", new_callable=AsyncMock)
    async def test_unassign_failure(self, mock_unassign, client: AsyncClient) -> None:
        mock_unassign.return_value = (False, "未找到订阅")
        resp = await client.post(
            "/subscriptions/bili/1/unassign",
            data={"token_name": "viewer-token"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=token.assign_failed" in loc
        assert "type=error" in loc
