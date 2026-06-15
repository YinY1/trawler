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


@router.get("/check", response_class=HTMLResponse)
async def check_page(request: Request) -> HTMLResponse:
    """Check page — trigger button + log output."""
    return TEMPLATES.TemplateResponse(
        request,
        "check.html",
        {"active_nav": "check", "running": request.app.state.check_running},
    )


@router.post("/check/run")
async def check_run(request: Request) -> dict[str, str]:
    """Trigger a check run in the background."""
    if request.app.state.check_running:
        return {"status": "already_running"}

    request.app.state.check_running = True

    def _log_callback(event_type: str, message: str) -> None:
        request.app.state.log_queue.put_nowait(
            {"type": event_type, "message": message, "time": time.strftime("%H:%M:%S")}
        )

    async def _run() -> None:
        try:
            config = await load_config()
            await run_check_once(config, platform="all", log_callback=_log_callback)
        except Exception as exc:
            request.app.state.log_queue.put_nowait(
                {"type": "error", "message": f"检查失败: {exc}", "time": time.strftime("%H:%M:%S")}
            )
        finally:
            await request.app.state.log_queue.put(None)  # Signal EOF
            request.app.state.check_running = False
            request.app.state.check_task = None

    request.app.state.check_task = asyncio.create_task(_run())
    return {"status": "started"}


@router.get("/check/stream")
async def check_stream(request: Request) -> StreamingResponse:
    """SSE endpoint: stream log events to the browser."""
    queue = request.app.state.log_queue

    async def event_generator() -> AsyncIterator[bytes]:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                yield b": heartbeat\n\n"  # SSE keepalive comment
                continue
            if item is None:
                yield b"event: done\ndata: \n\n"
                break
            data = json.dumps(item, ensure_ascii=False)
            yield f"event: log\ndata: {data}\n\n".encode("utf-8")

    return StreamingResponse(event_generator(), media_type="text/event-stream")
