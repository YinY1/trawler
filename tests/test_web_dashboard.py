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
    """已登录 client（适配 login_guard + CSRF middleware）。"""
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


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
