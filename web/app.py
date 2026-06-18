from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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
    app.state.subscribers = []  # list[asyncio.Queue[dict | None]]
    app.state.log_history = []
    app.state.check_running = False
    app.state.check_task = None
    app.state.check_processed_count = 0
    app.state.check_started_at = None

    # ── D 组: 全局日志页 ────────────────────────────────────────────
    from web.logging_bridge import setup_web_logging, teardown_web_logging

    log_bus = setup_web_logging()
    app.state.log_bus = log_bus
    # ──────────────────────────────────────────────────────────────────

    yield

    # ── D 组: 清理 ────────────────────────────────────────────────────
    teardown_web_logging(log_bus)
    # ──────────────────────────────────────────────────────────────────

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
    app.state.subscribers = []  # list[asyncio.Queue[dict | None]]
    app.state.log_history = []
    app.state.check_running = False
    app.state.check_task = None
    app.state.check_processed_count = 0
    app.state.check_started_at = None

    # D 组: 全局日志页 - LogBus (lifespan 里会重新初始化)
    from web.logging_bridge import LogBus

    app.state.log_bus = LogBus()

    # ── 全局异常处理: 让 422 / 500 进入日志链路 ────────────────────────
    # RequestValidationError 在路由 handler 之前抛出, 不进 try/except,
    # 必须注册 exception_handler 才能被 logger 捕获并流到 /logs。
    from fastapi import Request

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.warning(
            "⚠️ 请求参数校验失败: %s %s — %s",
            request.method,
            request.url.path,
            exc.errors(),
        )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(  # pyright: ignore[reportUnusedFunction]
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception(
            "💥 未处理异常: %s %s — %s", request.method, request.url.path, exc
        )
        return JSONResponse(status_code=500, content={"detail": "内部错误"})

    # ──────────────────────────────────────────────────────────────────

    # Mount static files — directory exists in the repo, no need to create
    static_dir = HERE / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register routes
    from web.routes.auth import router as auth_router
    from web.routes.check import router as check_router
    from web.routes.dashboard import router as dashboard_router
    from web.routes.endpoints import router as endpoints_router
    from web.routes.logs import router as logs_router
    from web.routes.settings import router as settings_router
    from web.routes.subscriptions import router as subscriptions_router

    app.include_router(dashboard_router)
    app.include_router(subscriptions_router)
    app.include_router(check_router)
    app.include_router(auth_router)
    app.include_router(logs_router)
    app.include_router(endpoints_router)
    app.include_router(settings_router)

    return app


app = create_app()
