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
