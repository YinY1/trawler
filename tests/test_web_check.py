from __future__ import annotations

import asyncio
import time
from typing import Any
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
        # StreamingResponse only flushes headers after the generator yields
        # its first chunk, and the live SSE loop blocks on sub_queue.get()
        # until a producer puts something. Drive a producer that broadcasts
        # EOF as soon as the per-connection sub_queue is registered so the
        # response completes and headers become readable.
        async def producer() -> None:
            await asyncio.sleep(0)
            for _ in range(50):
                if app.state.subscribers:
                    break
                await asyncio.sleep(0.01)
            for sub in list(app.state.subscribers):
                sub.put_nowait(None)

        producer_task = asyncio.create_task(producer())
        try:
            async with client.stream("GET", "/check/stream") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")
        finally:
            await producer_task

    async def test_check_stream_content(self, client: AsyncClient) -> None:
        """Verify SSE stream produces log events and done sentinel."""
        # Isolate from other tests: clear log_history so the SSE handler's
        # connect-time replay does not surface events produced elsewhere
        # (e.g. another test's mock pipeline, the global LogBus). Without
        # this, when check_started_at is None the replay filter admits every
        # prior item and the test sees stale messages instead of "test log".
        app.state.log_history.clear()
        app.state.check_running = False
        app.state.check_started_at = None

        # Fan-out architecture: each SSE connection registers its own
        # subscriber queue in ``state.subscribers`` when the route handler
        # runs. Mimic ``_log_callback`` + ``_run`` finally block: after the
        # stream connects (its sub_queue is registered), broadcast one log
        # item then a None EOF sentinel to every subscriber.
        async def producer() -> None:
            # Yield once so the SSE request reaches check_stream and
            # registers its per-connection sub_queue in state.subscribers.
            await asyncio.sleep(0)
            for _ in range(50):
                if app.state.subscribers:
                    break
                await asyncio.sleep(0.01)
            item: dict[str, Any] = {
                "type": "log",
                "message": "test log",
                "time": "00:00:00",
                "_ts": time.time(),
            }
            for sub in list(app.state.subscribers):
                sub.put_nowait(item)
            for sub in list(app.state.subscribers):
                sub.put_nowait(None)

        producer_task = asyncio.create_task(producer())
        try:
            async with client.stream("GET", "/check/stream") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)

            text = b"".join(chunks).decode("utf-8")
            assert "event: log" in text
            assert "test log" in text
            assert "event: done" in text
        finally:
            await producer_task

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
