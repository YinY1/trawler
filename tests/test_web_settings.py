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

        resp = await client.post(
            "/settings", data={"data_dir": "/data/test"}, headers={"X-Requested-With": "XMLHttpRequest"}
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "toast" in resp.headers.get("HX-Trigger", "")


class TestSettingsVersionDisplay:
    """issue #55: settings 页含 '系统信息' 卡片，展示版本字段。"""

    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page_contains_system_info_card(self, mock_load, client: AsyncClient) -> None:
        from shared.config import Config
        from shared.constants import VERSION_DISPLAY

        mock_load.return_value = Config()
        resp = await client.get("/settings")
        assert resp.status_code == 200
        body = resp.text
        assert "系统信息" in body
        # settings 页继承 base.html，sidebar 也会渲染；VERSION_DISPLAY 应出现
        assert VERSION_DISPLAY in resp.text

    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page_contains_version_display(self, mock_load, client: AsyncClient) -> None:
        from shared.config import Config
        from shared.constants import VERSION_DISPLAY

        mock_load.return_value = Config()
        resp = await client.get("/settings")
        assert resp.status_code == 200
        # VERSION_DISPLAY 形如 `0.1.0+dev (unknown)`，HTML 渲染后应原样出现
        assert VERSION_DISPLAY in resp.text
