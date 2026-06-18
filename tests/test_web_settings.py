from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestSettings:
    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page(self, mock_load, client: AsyncClient) -> None:
        from shared.config import Config

        mock_load.return_value = Config()
        resp = await client.get("/settings")
        assert resp.status_code == 200

    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page_contains_analysis_card(self, mock_load, client: AsyncClient) -> None:
        from shared.config import Config

        mock_load.return_value = Config()
        resp = await client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        assert "AI 分析" in body
        assert 'name="provider"' in body
        assert "测试连通性" in body

    @patch("tomlkit.dumps", return_value="")
    @patch("web.routes.settings.Path.write_text")
    @patch("web.routes.settings.Path.exists")
    async def test_settings_save(self, mock_exists, mock_write, mock_dumps, client: AsyncClient) -> None:
        mock_exists.return_value = True

        resp = await client.post("/settings", data={"data_dir": "/data/test"})
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "toast" in resp.headers.get("HX-Trigger", "")
