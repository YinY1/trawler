from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Trawler Web UI", version="0.1.0")

    # Mount static files
    static_dir = HERE / "static"
    static_dir.mkdir(exist_ok=True)
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
