"""D 组: 全局日志页 — SSE 实时日志流。

完全独立于 check 页的 log_queue/log_history，使用 fan-out 模式
(LogBus.subscribe()) 避免单 queue 多消费者的竞争问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from web.app import TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    """Global log page — real-time log output."""
    return TEMPLATES.TemplateResponse(
        request,
        "logs.html",
        {
            "active_nav": "logs",
        },
    )


@router.get("/logs/stream")
async def logs_stream(request: Request) -> StreamingResponse:
    """SSE endpoint: stream all application logs to the browser.

    Uses LogBus.subscribe() to get a dedicated queue (fan-out pattern).
    New subscribers first receive the current history snapshot, then
    continue with live entries.
    """
    bus = request.app.state.log_bus

    # Subscribe before reading history to avoid missing entries between
    # the snapshot and the live loop.
    queue = bus.subscribe()
    history_snapshot = list(bus.history)

    async def event_generator() -> AsyncIterator[bytes]:
        # 1) Replay history
        for entry in history_snapshot:
            data = json.dumps(entry.to_dict(), ensure_ascii=False)
            yield f"event: log\ndata: {data}\n\n".encode("utf-8")

        # 2) Stream live events
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                # None sentinel is not used in the current design, but
                # handle it gracefully for future extensibility.
                if item is None:
                    break
                data = json.dumps(item.to_dict(), ensure_ascii=False)
                yield f"event: log\ndata: {data}\n\n".encode("utf-8")
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
