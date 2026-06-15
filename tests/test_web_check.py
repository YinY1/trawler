from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestCheck:
    async def test_check_page(self, client: AsyncClient) -> None:
        resp = await client.get("/check")
        assert resp.status_code == 200

    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run(self, mock_load, mock_run, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "/tmp"
        resp = await client.post("/check/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("started",)

    async def test_check_stream_returns_sse(self, client: AsyncClient) -> None:
        resp = await client.get("/check/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
