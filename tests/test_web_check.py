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

    async def test_check_stream_content(self, client: AsyncClient) -> None:
        """Verify SSE stream produces log events and done sentinel."""
        # Seed the queue with test events
        await app.state.log_queue.put({"type": "log", "message": "test log", "time": "00:00:00"})
        await app.state.log_queue.put(None)  # EOF

        async with client.stream("GET", "/check/stream") as resp:
            assert resp.status_code == 200
            chunks = []
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)

        text = b"".join(chunks).decode("utf-8")
        assert "event: log" in text
        assert "test log" in text
        assert "event: done" in text

    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_sends_sse(self, mock_load, mock_run, client: AsyncClient) -> None:
        """Verify starting a check produces events on the SSE stream."""
        mock_load.return_value.general.data_dir = "/tmp"

        # Start a check run
        resp = await client.post("/check/run")
        assert resp.status_code == 200

        # Read SSE stream
        async with client.stream("GET", "/check/stream") as sse:
            assert sse.status_code == 200
            chunks = []
            async for chunk in sse.aiter_bytes():
                chunks.append(chunk)
                # Stop after getting the done event
                if b"event: done" in b"".join(chunks):
                    break

        text = b"".join(chunks).decode("utf-8")
        assert "event: log" in text or "event: done" in text

    async def test_check_run_twice_returns_already_running(self, client: AsyncClient) -> None:
        """Second POST /check/run while running returns already_running."""
        # Mark as running
        app.state.check_running = True
        resp = await client.post("/check/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "already_running"
        app.state.check_running = False
