from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


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
