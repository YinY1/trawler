from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from shared.protocols import Phase
from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"
HTMX_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """已登录 client（适配 login_guard + CSRF middleware）。

    SSE 测试需要直接访问 app.state（subscribers / log_history / check_running），
    所以 fixture 额外把 app 实例挂到 client._app 供测试读取。
    """
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        # SSE 测试需要访问 app.state.subscribers 等，挂到 client 上
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestCheck:
    async def test_check_page(self, client: AsyncClient) -> None:
        resp = await client.get("/check")
        assert resp.status_code == 200

    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run(self, mock_load, mock_run, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "/tmp"
        resp = await client.post("/check/run", headers=HTMX_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("started",)

    async def test_check_stream_returns_sse(self, client: AsyncClient) -> None:
        # StreamingResponse only flushes headers after the generator yields
        # its first chunk, and the live SSE loop blocks on sub_queue.get()
        # until a producer puts something. Drive a producer that broadcasts
        # EOF as soon as the per-connection sub_queue is registered so the
        # response completes and headers become readable.
        app = client._app  # type: ignore[attr-defined]

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
        app = client._app  # type: ignore[attr-defined]
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

        # Make run_check_once yield control briefly so the SSE connection
        # (opened below) can register its subscriber before _run's finally
        # broadcasts EOF. Without this, the check task may finish before SSE
        # connects, dropping the EOF sentinel and hanging the stream.
        async def _slow_run(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0.1)

        mock_run.side_effect = _slow_run

        # Start a check run
        resp = await client.post("/check/run", headers=HTMX_HEADERS)
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
        app = client._app  # type: ignore[attr-defined]
        # Mark as running
        app.state.check_running = True
        resp = await client.post("/check/run", headers=HTMX_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "already_running"
        app.state.check_running = False

    @patch("web.routes.check.PipelineEngine")
    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_with_filter_calls_run_specific_messages(
        self, mock_load, mock_run_once, mock_engine, client: AsyncClient
    ) -> None:
        """带 since/title 等筛选参数时走 run_specific_messages，不走 run_check_once。"""
        from types import SimpleNamespace

        mock_load.return_value.general.data_dir = "/tmp"
        # 让 run_specific_messages 是 AsyncMock，避免真实执行
        mock_engine.run_specific_messages = AsyncMock()

        # patch MessageStore.query_messages 返回 1 条匹配，否则「无匹配」短路返回
        with patch("web.routes.check.MessageStore") as mock_store_cls:
            mock_store_cls.return_value.query_messages.return_value = [SimpleNamespace(msg_id="bili:test1")]

            resp = await client.post(
                "/check/run",
                headers=HTMX_HEADERS,
                data={"since": "7d", "title": "测试", "reset_phase": "summarized", "skip_push": "on"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "started"
            # run_specific_messages 被调用（可能尚未 await 完成因是 background task，等待一下）
            await asyncio.sleep(0.05)
            mock_engine.run_specific_messages.assert_called_once()
            # 传入的 msg_ids 应包含 query 返回的 id
            call_kwargs = mock_engine.run_specific_messages.call_args.kwargs
            assert call_kwargs["msg_ids"] == ["bili:test1"]
            assert call_kwargs["skip_push"] is True
            assert call_kwargs["from_phase"] is Phase.SUMMARIZED  # reset_phase=summarized → Phase[upper()]

            # 验证筛选参数确实传给了 query_messages（避免 mock 掉 MessageStore 后
            # 参数解析路径失去覆盖：title/since/platform_filter 都应携带）
            mock_store = mock_store_cls.return_value
            mock_store.query_messages.assert_called_once()
            qkwargs = mock_store.query_messages.call_args.kwargs
            assert qkwargs["title"] == "测试"
            assert qkwargs["author"] is None  # 未传 author
            assert qkwargs["platform"] is None  # 未传 platform
            # since 经 parse_since("7d") 解析为时间戳，断言在合理区间（最近 8 天内）
            assert qkwargs["since"] is not None
            assert qkwargs["since"] > time.time() - 86400 * 8

        # run_check_once 不应被调用
        mock_run_once.assert_not_called()
        # 清理 background task 状态
        client._app.state.check_running = False  # type: ignore[attr-defined]

    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_without_filter_calls_run_check_once(
        self, mock_load, mock_run_once, client: AsyncClient
    ) -> None:
        """无筛选参数时走原 run_check_once（行为不变）。"""
        mock_load.return_value.general.data_dir = "/tmp"

        resp = await client.post("/check/run", headers=HTMX_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        await asyncio.sleep(0.05)
        mock_run_once.assert_called_once()
        client._app.state.check_running = False  # type: ignore[attr-defined]

    @patch("web.routes.check.PipelineEngine")
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_manual_mode_sends_sse(self, mock_load, mock_engine, client: AsyncClient) -> None:
        """手动模式（带筛选参数）应通过 SSE 广播日志事件。"""
        # isolate: 清理前序测试残留状态（对齐 test_check_stream_content:85-87）
        app = client._app
        app.state.log_history.clear()
        app.state.check_running = False
        app.state.check_started_at = None

        mock_load.return_value.general.data_dir = "/tmp"

        # 让 run_specific_messages 通过 log_callback 发几条日志后返回。
        # 先 sleep 0.1s 让 SSE 先连上注册 sub_queue，再发 callback（避免 flaky，
        # 对齐现有 test_check_run_sends_sse 的 _slow_run 模式，见 tests/test_web_check.py:128-158）
        async def _fake_run(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0.1)
            cb = kwargs.get("log_callback")
            if cb:
                cb("log", "▶ 手动重跑 1 条消息")
                cb("done", "✅ 手动重跑完成")

        mock_engine.run_specific_messages = _fake_run

        resp = await client.post(
            "/check/run",
            headers=HTMX_HEADERS,
            data={"since": "24h", "reset_phase": "summarized"},
        )
        assert resp.status_code == 200

        # 读 SSE 流，验证能收到日志和 done 事件
        async with client.stream("GET", "/check/stream") as sse:
            assert sse.status_code == 200
            chunks = []
            async for chunk in sse.aiter_bytes():
                chunks.append(chunk)
                if b"event: done" in b"".join(chunks):
                    break

        text = b"".join(chunks).decode("utf-8")
        assert "手动重跑" in text
        assert "event: done" in text
        client._app.state.check_running = False  # type: ignore[attr-defined]

    async def test_check_run_invalid_phase_returns_error_sync(
        self, client: AsyncClient
    ) -> None:
        """非法 reset_phase 同步返回 status=error，不启动 background task。"""
        resp = await client.post(
            "/check/run",
            headers=HTMX_HEADERS,
            data={"reset_phase": "NONEXISTENT", "title": "x"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "NONEXISTENT" in data["message"]
        # 不应占用锁
        assert client._app.state.check_running is False  # type: ignore[attr-defined]

    async def test_check_run_invalid_since_returns_error_sync(
        self, client: AsyncClient
    ) -> None:
        """非法 since 格式同步返回 status=error，不启动 background task。"""
        resp = await client.post(
            "/check/run",
            headers=HTMX_HEADERS,
            data={"since": "not-a-duration"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "error"
        assert "since" in data["message"] or "解析" in data["message"]
        assert client._app.state.check_running is False  # type: ignore[attr-defined]
