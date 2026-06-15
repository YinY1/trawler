from __future__ import annotations

import io
import time
from typing import Any, cast

import qrcode
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from shared.auth import get_authenticator, update_auth_section
from shared.auth.base import QRStatus
from shared.config import Config, load_config
from web.app import TEMPLATES

router = APIRouter()

PLATFORM_INFO = [
    {"key": "bili", "name": "B站"},
    {"key": "xhs", "name": "小红书"},
    {"key": "weibo", "name": "微博"},
]

CONFIG_AUTH_KEYS = {
    "bili": ("bilibili", "auth"),
    "xhs": ("xiaohongshu", "auth"),
    "weibo": ("weibo", "auth"),
}

# In-memory QR session storage (single-user, so one session per platform)
_qr_sessions: dict[str, dict[str, str]] = {}


def _get_auth_status(config: Config, platform_key: str) -> tuple[str, str]:
    """Get token status for a platform."""
    section, _ = CONFIG_AUTH_KEYS[platform_key]
    auth = getattr(config, section).auth
    if auth.expires_at <= 0:
        return "未配置", ""
    elif auth.expires_at < time.time():
        return "已过期", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at))
    else:
        remaining = int((auth.expires_at - time.time()) // 86400)
        return f"有效 (剩余 {remaining} 天)", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at))


@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request) -> HTMLResponse:
    """Login management page."""
    config = await load_config()
    platforms: list[dict[str, str]] = []
    for p in PLATFORM_INFO:
        status, expires = _get_auth_status(config, p["key"])
        platforms.append({**p, "token_status": status, "expires": expires})
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"active_nav": "auth", "platforms": platforms},
    )


@router.get("/auth/qr/{platform_key}")
async def auth_qr(platform_key: str) -> Response:
    """Generate QR code image for platform login.

    Stores the qr_key server-side so the poll endpoint can use it.
    """
    auth = get_authenticator(platform_key)
    qr_result = await auth.generate_qr_code()
    _qr_sessions[platform_key] = {"qr_key": qr_result.qr_key}
    # Render QR code to PNG bytes
    img = qrcode.make(qr_result.qr_url)
    buf = io.BytesIO()
    # PIL.Image.save lacks type stubs; route kwargs through Any.
    save = cast(Any, img.save)
    save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/auth/poll/{platform_key}")
async def auth_poll(platform_key: str) -> dict[str, Any]:
    """Poll QR scan status and auto-complete on success."""
    auth = get_authenticator(platform_key)
    session = _qr_sessions.get(platform_key)
    if session is None:
        return {"status": "no_session"}
    try:
        status = await auth.poll_qr_status(session["qr_key"])
    except Exception as exc:
        return {"status": "error", "message": f"轮询失败: {exc}"}

    if status.status != QRStatus.SUCCESS:
        return {"status": status.status.value}

    # Status is SUCCESS — get tokens
    try:
        tokens = await auth.get_tokens(session["qr_key"])
    except Exception as exc:
        # QR consumed but tokens not obtained — session will expire naturally
        _qr_sessions.pop(platform_key, None)
        return {"status": "error", "message": f"获取凭证失败: {exc}"}

    # Build auth_dict
    if platform_key in ("weibo", "xhs"):
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        auth_dict: dict[str, Any] = {"cookie": cookie_str, "expires_at": tokens.expires_at}
    else:
        auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
    rt_val = getattr(auth, "refresh_token", None)
    if platform_key == "bili" and rt_val:
        auth_dict["refresh_token"] = rt_val
    try:
        await update_auth_section(platform_key, auth_dict)
    except Exception as exc:
        _qr_sessions.pop(platform_key, None)
        return {"status": "error", "message": f"保存凭证失败: {exc}"}
    _qr_sessions.pop(platform_key, None)
    return {"status": "success", "message": "登录成功"}
