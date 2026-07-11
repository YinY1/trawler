from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shared.constants import BUILD_DATE, GIT_SHA, VERSION, VERSION_DISPLAY

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

# ── auth_guard 白名单 (Web 站点访问鉴权) ────────────────────────
# 不需要登录/setup 检查的路径前缀/精确路径
_PUBLIC_PATHS = {"/login", "/logout", "/setup"}
# /api/ 整段豁免：API namespace 用 token 鉴权（api.auth.require_scopes），
# 与浏览器 session/CSRF 完全隔离。详见 docs/superpowers/specs/2026-07-04-http-api-design.md
_PUBLIC_PREFIXES = ("/static", "/api/")

# CSRF 豁免路径（未登录 POST，无 session 可盗）
_CSRF_EXEMPT_PATHS = {"/login", "/setup"}


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


def _phase_label(phase: Any) -> str:
    """Map a Phase enum value (or its .name) to a Chinese display label.

    Companion to ``phase_color``: phase_color picks the badge color, phase_label
    picks the human-readable text. Keeps templates free of hardcoded enum names.
    Returns the raw .name as fallback for unknown phases (forward-compat).
    """
    name = phase.name if hasattr(phase, "name") else str(phase)
    mapping = {
        "DISCOVERED": "已发现",
        "DOWNLOADED": "已下载",
        "TRANSCRIBED": "已转写",
        "SUMMARIZED": "已摘要",
        "PUSHED": "已推送",
    }
    return mapping.get(name, name)


TEMPLATES.env.filters["phase_label"] = _phase_label


# ── issue #55: 注入版本常量到所有模板 ────────────────────────────
# 让 base.html sidebar / settings.html 等模板可直接 {{ VERSION_DISPLAY }}，
# 无需每个路由手动传 context。
# 顶部 import 区已 import 了 BUILD_DATE/GIT_SHA/VERSION/VERSION_DISPLAY，
# 此处仅做 globals 赋值，不再重复 import。
TEMPLATES.env.globals["VERSION"] = VERSION
TEMPLATES.env.globals["GIT_SHA"] = GIT_SHA
TEMPLATES.env.globals["BUILD_DATE"] = BUILD_DATE
TEMPLATES.env.globals["VERSION_DISPLAY"] = VERSION_DISPLAY


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
    app = FastAPI(title="Trawler Web UI", version=VERSION, lifespan=lifespan)

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

    # ── CSRF guard (Web 站点访问鉴权) ────────────────────────────────
    # 已登录用户的写操作校验 HTMX 头或同源 referer。
    # 注册顺序（add_middleware 用 insert(0)，后 add 的在外层）：
    #   请求 → SessionMiddleware（最外，注入 session scope）
    #        → auth_guard（setup/login 检查）
    #        → csrf_guard（写操作 CSRF 校验，仅已登录用户能到达）
    #        → 路由
    # 所以 add 顺序：csrf_guard 先（最内）→ auth_guard → SessionMiddleware（最后 add，最外）。

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:  # pyright: ignore[reportUnusedFunction]
        path = request.url.path
        # 豁免：/login /setup /static/*
        if path in _CSRF_EXEMPT_PATHS or path.startswith("/static"):
            return await call_next(request)
        # 豁免：/api/* —— API 走 token 鉴权（Authorization: Bearer），无 session 可盗，
        # CSRF 不适用。路由层 require_scopes 依赖兜底鉴权（health 等无鉴权端点除外）。
        if path.startswith("/api/"):
            return await call_next(request)
        # 仅校验写方法
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return await call_next(request)
        from web.auth import verify_csrf

        if not verify_csrf(request):
            return JSONResponse(status_code=403, content={"detail": "CSRF check failed"})
        return await call_next(request)

    # ── auth_guard: setup guard + login guard (合并) ─────────────────
    from web.auth import is_setup_complete

    @app.middleware("http")
    async def auth_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:  # pyright: ignore[reportUnusedFunction]
        path = request.url.path
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)
        # setup guard：未初始化时强制跳 /setup
        if not is_setup_complete():
            return RedirectResponse("/setup", status_code=302)
        # login guard：未登录时跳 /login?next=<path>
        if not request.session.get("logged_in"):
            login_url = f"/login?next={path}"
            return RedirectResponse(login_url, status_code=302)
        return await call_next(request)

    # SessionMiddleware（最外层，注入 session scope 供 auth_guard 读取）
    from starlette.middleware.sessions import SessionMiddleware

    from web.auth import load_auth_config

    auth_cfg = load_auth_config()
    secret = auth_cfg.session_secret or "SETUP_INCOMPLETE_PLACEHOLDER"
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="trawler_session",
        max_age=auth_cfg.session_max_age_seconds,
        same_site="lax",
        https_only=False,
    )

    # ── 全局异常处理: 让 422 / 500 进入日志链路 ────────────────────────
    # RequestValidationError 在路由 handler 之前抛出, 不进 try/except,
    # 必须注册 exception_handler 才能被 logger 捕获并流到 /logs。
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
        logger.exception("💥 未处理异常: %s %s — %s", request.method, request.url.path, exc)
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
    from web.routes.health import router as health_router
    from web.routes.logs import router as logs_router
    from web.routes.messages import router as messages_router
    from web.routes.settings import router as settings_router
    from web.routes.subscriptions import router as subscriptions_router
    from web.routes.tokens import router as tokens_router
    from web.routes.web_auth import router as web_auth_router

    app.include_router(dashboard_router)
    app.include_router(subscriptions_router)
    app.include_router(tokens_router)
    app.include_router(check_router)
    app.include_router(auth_router)
    app.include_router(logs_router)
    app.include_router(endpoints_router)
    app.include_router(settings_router)
    app.include_router(web_auth_router)
    app.include_router(messages_router)
    app.include_router(health_router)

    # ── API v1 namespace（bot 友好的 JSON 接口）──────────────────────
    # 与 web 路由平级挂载，但走 token 鉴权（api.auth.require_scopes），
    # 中间件层（auth_guard / csrf_guard）对 /api/* 整段豁免。
    from api.v1.router import router as api_v1_router

    app.include_router(api_v1_router, prefix="/api/v1")

    return app


app = create_app()
