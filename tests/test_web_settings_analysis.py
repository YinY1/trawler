"""Tests for /settings/analysis/test and /settings/analysis/save endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestAnalysisTest:
    @patch(
        "web.routes.settings._probe_provider",
        new_callable=AsyncMock,
        return_value={"ok": True, "message": "连通正常，模型响应: pong"},
    )
    async def test_analysis_test_success(self, mock_probe: AsyncMock, client: AsyncClient) -> None:
        resp = await client.post(
            "/settings/analysis/test",
            data={
                "provider": "openai",
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-x",
                "model_name": "gpt-4o-mini",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "连通正常" in body["message"]
        mock_probe.assert_awaited_once_with("openai", "https://api.openai.com/v1", "sk-x", "gpt-4o-mini")

    @patch(
        "web.routes.settings._probe_provider",
        new_callable=AsyncMock,
        return_value={"ok": False, "message": "连接失败: timeout"},
    )
    async def test_analysis_test_failure(self, mock_probe: AsyncMock, client: AsyncClient) -> None:
        resp = await client.post(
            "/settings/analysis/test",
            data={
                "provider": "openai",
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-bad",
                "model_name": "gpt-4o-mini",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "连接失败" in body["message"]

    @patch(
        "web.routes.settings._probe_provider",
        new_callable=AsyncMock,
        return_value={"ok": False, "message": "不支持的 provider: codebuddy"},
    )
    async def test_analysis_test_invalid_provider(self, mock_probe: AsyncMock, client: AsyncClient) -> None:
        resp = await client.post(
            "/settings/analysis/test",
            data={
                "provider": "codebuddy",
                "api_base": "",
                "api_key": "",
                "model_name": "",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "不支持的 provider" in body["message"]


class TestAnalysisSave:
    @patch("web.routes.settings.Path.read_text", return_value='[analysis]\nenabled = true\nprovider = "old"\n')
    @patch("web.routes.settings.Path.exists", return_value=True)
    @patch("web.routes.settings.Path.write_text")
    @patch("web.routes.settings.tomlkit.dumps", return_value="")
    async def test_analysis_save_writes_toml(
        self,
        mock_dumps: AsyncMock,
        mock_write: AsyncMock,
        mock_exists: AsyncMock,
        mock_read: AsyncMock,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/settings/analysis/save",
            data={
                "enabled": "true",
                "provider": "openai",
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-x",
                "model_name": "gpt-4o-mini",
            },
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "toast" in resp.headers["HX-Trigger"]

    @patch("web.routes.settings.Path.read_text", return_value="")
    @patch("web.routes.settings.Path.exists", return_value=True)
    @patch("web.routes.settings.Path.write_text")
    @patch("web.routes.settings.tomlkit.dumps", return_value="")
    async def test_analysis_save_creates_section_if_missing(
        self,
        mock_dumps: AsyncMock,
        mock_write: AsyncMock,
        mock_exists: AsyncMock,
        mock_read: AsyncMock,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/settings/analysis/save",
            data={
                "enabled": "true",
                "provider": "openai",
                "api_base": "https://api.openai.com/v1",
                "api_key": "sk-x",
                "model_name": "gpt-4o-mini",
            },
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers

    @patch("web.routes.settings.Path.read_text", return_value='[analysis]\nenabled = true\nprovider = "old"\n')
    @patch("web.routes.settings.Path.exists", return_value=True)
    @patch("web.routes.settings.Path.write_text")
    @patch("web.routes.settings.tomlkit.dumps", return_value="")
    async def test_analysis_save_empty_api_key_is_allowed(
        self,
        mock_dumps: AsyncMock,
        mock_write: AsyncMock,
        mock_exists: AsyncMock,
        mock_read: AsyncMock,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/settings/analysis/save",
            data={
                "enabled": "true",
                "provider": "ollama",
                "api_base": "http://localhost:11434",
                "api_key": "",
                "model_name": "llama3",
            },
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers

    @patch("web.routes.settings.Path.read_text", return_value='[analysis]\nenabled = true\nprovider = "old"\n')
    @patch("web.routes.settings.Path.exists", return_value=True)
    @patch("web.routes.settings.Path.write_text")
    @patch("web.routes.settings.tomlkit.dumps", return_value="")
    async def test_analysis_save_disabled_flag(
        self,
        mock_dumps: AsyncMock,
        mock_write: AsyncMock,
        mock_exists: AsyncMock,
        mock_read: AsyncMock,
        client: AsyncClient,
    ) -> None:
        resp = await client.post(
            "/settings/analysis/save",
            data={
                "enabled": "false",
                "provider": "openai",
                "api_base": "",
                "api_key": "",
                "model_name": "",
            },
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
