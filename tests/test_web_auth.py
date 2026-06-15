from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestAuth:
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page(self, mock_load, client: AsyncClient) -> None:
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        resp = await client.get("/auth")
        assert resp.status_code == 200

    @patch("web.routes.auth.get_authenticator")
    async def test_auth_qr_returns_png(self, mock_get_auth, client: AsyncClient) -> None:
        mock_auth = MagicMock()
        mock_auth.generate_qr_code = AsyncMock(
            return_value=MagicMock(qr_url="https://example.com/qr", qr_key="key1")
        )
        mock_get_auth.return_value = mock_auth
        resp = await client.get("/auth/qr/bili")
        assert resp.status_code == 200
        assert "image/png" in resp.headers.get("content-type", "")

    @patch("web.routes.auth.get_authenticator")
    async def test_auth_poll(self, mock_get_auth, client: AsyncClient) -> None:
        from shared.auth.base import AuthStatus, QRStatus

        mock_auth = MagicMock()
        # generate_qr_code is called by the /auth/qr setup step (must be awaitable)
        mock_auth.generate_qr_code = AsyncMock(
            return_value=MagicMock(qr_url="https://example.com/qr", qr_key="key1")
        )
        mock_auth.poll_qr_status = AsyncMock(
            return_value=AuthStatus(success=False, status=QRStatus.WAITING, message="waiting")
        )
        mock_get_auth.return_value = mock_auth
        # Need a QR session first
        await client.get("/auth/qr/bili")
        resp = await client.get("/auth/poll/bili")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "waiting"
