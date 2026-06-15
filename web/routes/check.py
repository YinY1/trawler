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

# In-memory queue for streaming logs
_log_queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
_running = False


@router.get("/check", response_class=HTMLResponse)
async def check_page(request: Request) -> HTMLResponse:
    """Check page — trigger button + log output."""
    return TEMPLATES.TemplateResponse(
        request,
        "check.html",
        {"active_nav": "check", "running": _running},
    )


@router.post("/check/run")
async def check_run() -> dict[str, str]:
    """Trigger a check run in the background."""
    global _running
    if _running:
        return {"status": "already_running"}

    _running = True

    def _log_callback(event_type: str, message: str) -> None:
        # Sync callback invoked from core/engine.py without await.
        # put_nowait is safe: we run inside the event loop thread.
        _log_queue.put_nowait(
            {"type": event_type, "message": message, "time": time.strftime("%H:%M:%S")}
        )

    async def _run() -> None:
        global _running
        try:
            config = await load_config()
            await run_check_once(config, platform="all", log_callback=_log_callback)
        except Exception as exc:
            await _log_queue.put({"type": "error", "message": f"检查失败: {exc}", "time": time.strftime("%H:%M:%S")})
        finally:
            await _log_queue.put(None)  # Signal EOF
            _running = False

    asyncio.create_task(_run())
    return {"status": "started"}


@router.get("/check/stream")
async def check_stream() -> StreamingResponse:
    """SSE endpoint: stream log events to the browser."""

    async def event_generator() -> AsyncIterator[bytes]:
        while True:
            item = await _log_queue.get()
            if item is None:
                yield b"event: done\ndata: \n\n"
                break
            data = json.dumps(item, ensure_ascii=False)
            yield f"event: log\ndata: {data}\n\n".encode("utf-8")

    return StreamingResponse(event_generator(), media_type="text/event-stream")
