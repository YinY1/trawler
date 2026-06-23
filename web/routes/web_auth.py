"""Web 站点访问鉴权路由（Web 用户登录）。

- ``/setup`` — 首次设管理员密码
- ``/login`` — 登录
- ``/logout`` — 登出
- ``/settings/account`` — 改密码

与 :mod:`web.routes.auth`（平台凭证登录管理）严格区分。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.app import TEMPLATES
from web.auth import (
    WEB_ADMIN_USERNAME,
    is_setup_complete,
    load_auth_config,
    set_password,
    verify_password,
)

router = APIRouter()
logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 8


# ── /setup ──────────────────────────────────────────────────────


@router.get("/setup", response_class=HTMLResponse, response_model=None)
async def setup_page(request: Request) -> HTMLResponse | RedirectResponse:
    """首次设密码页面。已 setup 则跳 /login。"""
    if is_setup_complete():
        return RedirectResponse("/login", status_code=302)
    return TEMPLATES.TemplateResponse(request, "setup.html", {})


@router.post("/setup", response_model=None)
async def setup_submit(
    request: Request,
    password: str = Form(...),
    password_confirm: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """提交首次密码。校验失败原地返回 400 + 错误，成功写盘后 303 → /login。

    **不**自动登录（强制再走一次 /login 表单，防止 setup 流程被中间人利用）。
    """
    if is_setup_complete():
        return RedirectResponse("/login", status_code=302)
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"密码至少 {MIN_PASSWORD_LENGTH} 个字符")
    if password != password_confirm:
        errors.append("两次输入的密码不一致")
    if errors:
        return TEMPLATES.TemplateResponse(
            request, "setup.html", {"errors": errors}, status_code=400
        )
    set_password(password)
    logger.info("🔑 Web 管理员密码已初始化")
    return RedirectResponse("/login", status_code=303)


# ── /login ──────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request) -> HTMLResponse | RedirectResponse:
    """登录页。已登录访问 /login 跳 /，避免重复登录。"""
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=302)
    return TEMPLATES.TemplateResponse(request, "login.html", {})


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """登录提交。密码错 401 + 错误；成功写 session 后 303 → next（默认 /）。"""
    cfg = load_auth_config()
    if not cfg.admin_password_hash or not verify_password(password, cfg.admin_password_hash):
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": "密码错误"}, status_code=401
        )
    request.session["logged_in"] = True
    request.session["username"] = WEB_ADMIN_USERNAME
    logger.info("🔑 Web 管理员登录成功")
    next_url = request.query_params.get("next") or "/"
    return RedirectResponse(next_url, status_code=303)


# ── /logout ─────────────────────────────────────────────────────


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """登出：清 session，303 → /login。"""
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── /settings/account ───────────────────────────────────────────


@router.get("/settings/account", response_class=HTMLResponse)
async def account_page(request: Request) -> HTMLResponse:
    """改密码页。"""
    return TEMPLATES.TemplateResponse(
        request, "account.html", {"active_nav": "account"}
    )


@router.post("/settings/account", response_model=None)
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """改密码。成功后清 session（强制登出）+ 轮转 session_secret。"""
    cfg = load_auth_config()
    errors: list[str] = []
    if not verify_password(current_password, cfg.admin_password_hash):
        errors.append("当前密码错误")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        errors.append(f"新密码至少 {MIN_PASSWORD_LENGTH} 个字符")
    if new_password != new_password_confirm:
        errors.append("两次输入的新密码不一致")
    if errors:
        return TEMPLATES.TemplateResponse(
            request, "account.html", {"active_nav": "account", "errors": errors}, status_code=400
        )
    set_password(new_password)  # 同时轮转 session_secret
    logger.info("🔑 Web 管理员密码已修改，所有旧 session 已失效")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
