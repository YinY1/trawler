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
