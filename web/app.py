from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def _timeago(ts: float | int | None) -> str:
    """Format a Unix timestamp as a human-friendly relative time string.

    Returns "—" when ts is falsy (0 / None). Used by the dashboard's
    "上次更新" subtitle and the recent messages table.
    """
    if not ts:
        return "—"
    ts_f = float(ts)
    now = datetime.now().timestamp()
    diff = now - ts_f
    if diff < 0:
        return "刚刚"
    if diff < 3600:
        minutes = int(diff // 60)
        return f"{minutes} 分钟前" if minutes > 0 else "刚刚"
    elif diff < 7200:
        return "1 小时前"
    elif diff < 86400:
        return f"{int(diff // 3600)} 小时前"
    elif diff < 172800:
        return "昨天 " + datetime.fromtimestamp(ts_f).strftime("%H:%M")
    elif diff < 259200:
        return "2 天前"
    else:
        return datetime.fromtimestamp(ts_f).strftime("%m-%d %H:%M")


TEMPLATES.env.filters["timeago"] = _timeago


def _phase_color(phase: Any) -> str:
    """Map a Phase enum value (or its .name) to a badge color name."""
    name = phase.name if hasattr(phase, "name") else str(phase)
    mapping = {
        "PUSHED": "green",
        "SUMMARIZED": "blue",
        "TRANSCRIBED": "blue",
        "DOWNLOADED": "blue",
        "DISCOVERED": "gray",
    }
    return mapping.get(name, "gray")


TEMPLATES.env.filters["phase_color"] = _phase_color


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: initialize async resources.

    Refreshed on real startup so each server run gets a fresh queue and a
    clean running flag, independent of any state set at module import time.
    """
    app.state.log_queue = asyncio.Queue()
    app.state.check_running = False
    app.state.check_task = None
    yield
    # Cancel any running check on shutdown
    current_task = app.state.check_task
    if current_task is not None and not current_task.done():
        current_task.cancel()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Trawler Web UI", version="0.1.0", lifespan=lifespan)

    # Initialize async resources on app.state so they exist even when the
    # lifespan handler is not executed (e.g. httpx ASGITransport in tests).
    # The lifespan handler re-initializes them on real startup.
    app.state.log_queue = asyncio.Queue()
    app.state.check_running = False
    app.state.check_task = None

    # Mount static files — directory exists in the repo, no need to create
    static_dir = HERE / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register routes
    from web.routes.auth import router as auth_router
    from web.routes.check import router as check_router
    from web.routes.dashboard import router as dashboard_router
    from web.routes.settings import router as settings_router
    from web.routes.subscriptions import router as subscriptions_router

    app.include_router(dashboard_router)
    app.include_router(subscriptions_router)
    app.include_router(check_router)
    app.include_router(auth_router)
    app.include_router(settings_router)

    return app


app = create_app()
