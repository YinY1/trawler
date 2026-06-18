from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from core.pipeline import run_check_once
from shared.config import load_config
from web.app import TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter()

# Cap for in-memory log history replayed to reconnecting clients.
LOG_HISTORY_CAP = 500


@router.get("/check", response_class=HTMLResponse)
async def check_page(request: Request) -> HTMLResponse:
    """Check page — trigger button + log output."""
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "check.html",
        {
            "active_nav": "check",
            "running": state.check_running,
            "processed_count": state.check_processed_count,
            "started_at": state.check_started_at,
            "log_history": list(state.log_history),
        },
    )


@router.get("/check/status")
async def check_status(request: Request) -> dict[str, object]:
    """Live status snapshot — used by client to reconnect after navigation."""
    state = request.app.state
    return {
        "running": state.check_running,
        "processed_count": state.check_processed_count,
        "started_at": state.check_started_at,
        "log_history": list(state.log_history),
    }


@router.post("/check/run")
async def check_run(request: Request) -> dict[str, str]:
    """Trigger a check run in the background."""
    state = request.app.state
    if state.check_running:
        return {"status": "already_running"}

    # Fresh run: reset state
    state.check_running = True
    state.check_processed_count = 0
    state.check_started_at = time.time()
    state.log_history.clear()

    def _log_callback(event_type: str, message: str) -> None:
        now = time.time()
        item = {"type": event_type, "message": message, "time": time.strftime("%H:%M:%S"), "_ts": now}
        # Maintain bounded history for late/reconnecting clients (authoritative
        # source of truth — every subscriber replays from this).
        state.log_history.append(item)
        if len(state.log_history) > LOG_HISTORY_CAP:
            del state.log_history[: len(state.log_history) - LOG_HISTORY_CAP]
        state.check_processed_count += 1
        # Fan-out: deliver a copy to every active subscriber's own queue so
        # concurrent SSE connections each receive the full stream (broadcast).
        # Single-consumer asyncio.Queue cannot serve multiple SSE connections —
        # they would split events between them and leak when None is consumed
        # by only one. Using per-subscriber queues fixes both.
        for sub in list(state.subscribers):
            try:
                sub.put_nowait(item)
            except asyncio.QueueFull:
                # Backpressure: drop rather than block the producer. Log so it
                # is observable, but do not let one slow client stall others.
                logger.warning("SSE subscriber queue full, dropping event")

    async def _run() -> None:
        try:
            config = await load_config()
            await run_check_once(config, platform="all", log_callback=_log_callback)
        except Exception as exc:
            err_item = {
                "type": "error",
                "message": f"检查失败: {exc}",
                "time": time.strftime("%H:%M:%S"),
                "_ts": time.time(),
            }
            state.log_history.append(err_item)
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(err_item)
                except asyncio.QueueFull:
                    pass
        finally:
            state.check_running = False
            # Clear started_at so a later /check/status snapshot (e.g. page
            # reload, "再次运行" reconnect) does not mistake a finished run
            # for an in-progress one and wrongly restart the elapsed timer.
            # log_history is intentionally kept so the client can still show
            # the just-finished run's logs.
            state.check_started_at = None
            state.check_task = None
            # Signal EOF to every active subscriber (broadcast). Each
            # subscriber's generator yields a `done` event and exits cleanly.
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_run())
    return {"status": "started"}


@router.get("/check/stream")
async def check_stream(request: Request) -> StreamingResponse:
    """SSE endpoint: stream log events to the browser.

    Each connection owns a private asyncio.Queue registered in
    ``state.subscribers``; ``_log_callback`` broadcasts every event to all
    subscribers. This avoids the single-consumer race where two concurrent
    SSE connections would split events between them and one would hang on
    EOF.

    Connect-time replay: snapshot ``log_history`` at entry, yield it as
    ``log`` events, then continue with the live queue. The final ``None``
    sentinel is translated into a ``done`` event and the connection closes.
    """
    state = request.app.state
    # Per-connection queue; bounded so a slow client cannot grow memory.
    sub_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(maxsize=2000)
    state.subscribers.append(sub_queue)
    # Snapshot history at connect time, but ONLY items from the current run.
    # If a new run just cleared history but the SSE connect races ahead of
    # the POST response, we could snapshot stale items from the PREVIOUS run.
    # Filtering by check_started_at timestamp ensures we only replay what
    # belongs to this run. time field is "%H:%M:%S" so we compare by
    # sequence: items appended after check_started_at belong to this run.
    current_run_started = state.check_started_at
    history_snapshot = [
        item for item in state.log_history if not current_run_started or item.get("_ts", 0) >= current_run_started
    ]

    async def event_generator() -> AsyncIterator[bytes]:
        try:
            # 1) Replay history captured at connect time (current run only)
            for item in history_snapshot:
                clean = {k: v for k, v in item.items() if k != "_ts"}
                data = json.dumps(clean, ensure_ascii=False)
                yield f"event: log\ndata: {data}\n\n".encode("utf-8")
            # 2) If a finished run's history was already present at connect
            #    time, emit done immediately so the client UI settles. We key
            #    off history_snapshot rather than check_running to avoid a
            #    race: a client that connects in the window between SSE open
            #    and POST /check/run would otherwise be told "done" before the
            #    run even starts. An empty history at connect time means no
            #    run has produced output yet — fall through to the live loop.
            if not state.check_running and history_snapshot:
                yield b"event: done\ndata: \n\n"
                return
            # 3) Stream live events from our private queue
            while True:
                try:
                    item = await asyncio.wait_for(sub_queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"  # SSE keepalive comment
                    continue
                if item is None:
                    yield b"event: done\ndata: \n\n"
                    return
                # Strip internal _ts (used only for history filtering) so the
                # browser-facing payload stays clean — matches history replay.
                clean = {k: v for k, v in item.items() if k != "_ts"}
                data = json.dumps(clean, ensure_ascii=False)
                yield f"event: log\ndata: {data}\n\n".encode("utf-8")
        finally:
            # Always deregister so producer never queues into a dead queue.
            try:
                state.subscribers.remove(sub_queue)
            except ValueError:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")
