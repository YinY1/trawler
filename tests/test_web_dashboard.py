from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestDashboard:
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_returns_200(self, mock_load, mock_list, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "/tmp"
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {"bilibili": [{"uid": 1, "name": "test"}]}

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
