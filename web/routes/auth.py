from __future__ import annotations

import io
import logging
import time
from typing import Any, cast

import qrcode
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from shared.auth import clear_auth_section, get_authenticator, update_auth_section
from shared.auth.base import BaseAuthenticator, PlatformTokens, QRStatus
from shared.config import Config, load_config
from web.app import TEMPLATES

router = APIRouter()
logger = logging.getLogger(__name__)

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

# In-memory QR session storage (single-user, so one session per platform).
# Holds qr_key for the poll endpoint to consume.
_qr_sessions: dict[str, dict[str, str]] = {}

# Module-level cache of authenticator instances, keyed by platform_key.
# XhsAuthenticator holds internal state (XhsClient, init cookies, qr_code)
# created during generate_qr_code() that poll_qr_status / get_tokens rely on.
# Without sharing, the new instance on each /poll request loses that state.
# Safe for all three platforms: bili/weibo authenticators are stateless.
_auth_instances: dict[str, BaseAuthenticator] = {}


def _get_auth_status(config: Config, platform_key: str) -> tuple[str, str, bool]:
    """Get token status for a platform.

    Returns (status_text, expires_text, has_auth) where has_auth indicates
    whether any auth section is present (controls logout button visibility).
    """
    section, _ = CONFIG_AUTH_KEYS[platform_key]
    auth = getattr(config, section).auth
    has_auth = auth.expires_at > 0
    if auth.expires_at <= 0:
        return "未配置", "", has_auth
    elif auth.expires_at < time.time():
        return "已过期", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at)), has_auth
    else:
        remaining = int((auth.expires_at - time.time()) // 86400)
        return f"有效 (剩余 {remaining} 天)", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at)), has_auth


# Nickname 缓存：避免每次访问 /auth 都 probe 3 个平台 API
# key: platform_key, value: (nickname: str | None, fetched_at: float)
# TTL 10 分钟——nickname 几乎不变，长 TTL 可接受。None 也缓存，避免已知失败反复重试。
_NICKNAME_TTL_SECONDS = 600
_nickname_cache: dict[str, tuple[str | None, float]] = {}


async def _fetch_nickname(config: Config, platform_key: str) -> str | None:
    """获取带 TTL 缓存的账号昵称；失败/未登录返回 None。

    不会抛异常——调用方用 None 表示"显示 —"，不影响 status 渲染。
    """
    cached = _nickname_cache.get(platform_key)
    if cached is not None and (time.time() - cached[1]) < _NICKNAME_TTL_SECONDS:
        return cached[0]

    # 从 config 构造 tokens；未配置 → 缓存 None 后返回
    tokens = _build_tokens_from_config(platform_key, config)
    if tokens is None:
        _nickname_cache[platform_key] = (None, time.time())
        return None

    # 通过 authenticator probe；异常降级 None
    auth = get_authenticator(platform_key)
    try:
        try:
            nick = await auth.get_user_nickname(tokens)
        except Exception as exc:
            logger.warning("🔑 %s nickname 获取异常: %s", platform_key, exc)
            nick = None
        _nickname_cache[platform_key] = (nick, time.time())
        return nick
    finally:
        # bili/weibo authenticators 无状态——每次 close 安全。
        # xhs 持有内部 _client；get_user_nickname 内部已通过 _ensure_client 重建，
        # close 这里会强制下次重连，10min TTL 下可接受。
        try:
            await auth.close()
        except Exception as exc:
            logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)


@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request) -> HTMLResponse:
    """Login management page."""
    config = await load_config()
    platforms: list[dict[str, Any]] = []
    for p in PLATFORM_INFO:
        status, expires, has_auth = _get_auth_status(config, p["key"])
        nickname = await _fetch_nickname(config, p["key"]) if has_auth else None
        platforms.append(
            {
                **p,
                "token_status": status,
                "expires": expires,
                "has_auth": has_auth,
                "nickname": nickname,
            }
        )
    return TEMPLATES.TemplateResponse(
        request,
        "platform_auth.html",
        {"active_nav": "auth", "platforms": platforms},
    )


@router.get("/auth/card/{platform_key}", response_class=HTMLResponse)
async def auth_card(request: Request, platform_key: str) -> HTMLResponse:
    """Return a single platform auth card fragment (HTMX partial refresh)."""
    info = next((p for p in PLATFORM_INFO if p["key"] == platform_key), None)
    if info is None:
        return HTMLResponse("not found", status_code=404)
    config = await load_config()
    status, expires, has_auth = _get_auth_status(config, platform_key)
    nickname = await _fetch_nickname(config, platform_key) if has_auth else None
    p: dict[str, Any] = {
        **info,
        "token_status": status,
        "expires": expires,
        "has_auth": has_auth,
        "nickname": nickname,
    }
    return TEMPLATES.TemplateResponse(
        request,
        "_auth_card.html",
        {"p": p},
    )


@router.post("/auth/logout/{platform_key}")
async def auth_logout(platform_key: str) -> dict[str, Any]:
    """Clear the [platform.auth] section for the given platform.

    Returns a JSON result consumed by the frontend logout handler.
    """
    logger.info("🔑 %s 注销", platform_key)
    logger.warning("⚠️ 清除 %s 平台的登录凭证", platform_key)
    # Drop any in-flight QR session + cached authenticator for this platform
    _qr_sessions.pop(platform_key, None)
    _nickname_cache.pop(platform_key, None)
    auth = _auth_instances.pop(platform_key, None)
    if auth is not None:
        try:
            await auth.close()
        except Exception as exc:
            logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)
    removed = await clear_auth_section(platform_key)
    if not removed:
        return {"ok": False, "message": "该平台未登录，无需注销"}
    return {"ok": True, "message": "已注销"}


@router.post("/auth/refresh/{platform_key}")
async def auth_refresh(platform_key: str) -> dict[str, Any]:
    """Refresh tokens for a platform via its refresh_tokens() method.

    Reads current tokens from config, calls authenticator.refresh_tokens(),
    and persists the result on success. Unlike QR login, this does not open
    a QR modal — it uses the existing refresh_token / cookie keepalive flow.
    """
    logger.info("🔑 %s 续期开始...", platform_key)
    try:
        config = await load_config()
    except Exception as exc:
        logger.warning("🔑 %s 续期失败：加载配置失败: %s", platform_key, exc)
        return {"ok": False, "message": f"续期失败：加载配置失败: {exc}"}

    # Build current PlatformTokens from config; if absent, ask user to re-login.
    tokens = _build_tokens_from_config(platform_key, config)
    if tokens is None:
        logger.warning("🔑 %s 续期失败：未配置有效凭证", platform_key)
        return {
            "ok": False,
            "message": "未配置有效凭证，请重新扫码登录",
        }

    auth = get_authenticator(platform_key)
    try:
        try:
            if not auth.supports_refresh():
                logger.warning("🔑 %s 续期失败：平台不支持 refresh", platform_key)
                return {"ok": False, "message": "该平台不支持续期，请重新扫码登录"}
            new_tokens = await auth.refresh_tokens(tokens)
        except Exception as exc:
            logger.warning("🔑 %s 续期失败: %s", platform_key, exc)
            return {
                "ok": False,
                "message": f"续期失败，请重新扫码登录: {exc}",
            }

        # If refresh_tokens returned the same tokens unchanged (obtained_at
        # not advanced), there is nothing to persist — treat as no-op success.
        if new_tokens.obtained_at <= tokens.obtained_at:
            logger.info("🔑 %s 续期无需更新（refresh_tokens 未变更）", platform_key)
            return {"ok": True, "message": "凭证无需更新"}

        auth_dict = _tokens_to_auth_dict(platform_key, new_tokens, auth)
        try:
            await update_auth_section(platform_key, auth_dict)
        except Exception as exc:
            logger.warning("🔑 %s 续期后保存失败: %s", platform_key, exc)
            return {"ok": False, "message": f"续期成功但保存失败: {exc}"}

        logger.info("🔑 %s 续期成功", platform_key)
        return {"ok": True, "message": "续期成功"}
    finally:
        try:
            await auth.close()
        except Exception as exc:
            logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)


def _build_tokens_from_config(platform_key: str, config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config for the given web platform_key.

    Thin wrapper around per-platform build_tokens_from_config() helpers.
    Returns None if the platform is not configured.
    """
    import importlib

    module_map = {
        "bili": "platforms.bilibili.auth",
        "weibo": "platforms.weibo.auth",
        "xhs": "platforms.xiaohongshu.auth",
    }
    module_name = module_map.get(platform_key)
    if module_name is None:
        return None
    try:
        mod = importlib.import_module(module_name)
        return mod.build_tokens_from_config(config)
    except (ImportError, AttributeError):
        return None


def _tokens_to_auth_dict(platform_key: str, tokens: PlatformTokens, auth: BaseAuthenticator) -> dict[str, Any]:
    """Convert PlatformTokens to the config auth dict for token_store.

    Mirrors shared.auth.scheduler._tokens_to_auth_dict but keyed by
    the web platform_key (bili/xhs/weibo).
    """
    if platform_key == "bili":
        d: dict[str, Any] = {
            "sessdata": tokens.cookies.get("sessdata", ""),
            "bili_jct": tokens.cookies.get("bili_jct", ""),
            "buvid3": tokens.cookies.get("buvid3", ""),
            "dedeuserid": tokens.cookies.get("dedeuserid", ""),
            "expires_at": tokens.expires_at,
        }
        rt_val = getattr(auth, "refresh_token", None)
        if rt_val:
            d["refresh_token"] = rt_val
        return d
    # xhs / weibo share the cookie-string shape
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    return {
        "cookie": cookie_str,
        "expires_at": tokens.expires_at,
        "nickname": tokens.nickname or "",
    }


@router.get("/auth/qr/{platform_key}")
async def auth_qr(platform_key: str) -> Response:
    """Generate QR code image for platform login.

    Stores the qr_key server-side so the poll endpoint can use it.
    The authenticator instance is cached in _auth_instances so that
    stateful authenticators (e.g. XHS) can be reused across poll requests.
    Any previously cached instance for this platform is closed first to
    release resources from a prior abandoned session.
    """
    # Close and replace any stale authenticator from a previous QR attempt
    stale = _auth_instances.pop(platform_key, None)
    if stale is not None:
        try:
            await stale.close()
        except Exception as exc:
            logger.warning("🔑 %s 关闭旧 authenticator 失败: %s", platform_key, exc)
    _qr_sessions.pop(platform_key, None)

    auth = get_authenticator(platform_key)
    logger.info("🔑 %s 生成二维码...", platform_key)
    try:
        qr_result = await auth.generate_qr_code()
        # Cache qr_key AND the authenticator instance for subsequent polls.
        _qr_sessions[platform_key] = {"qr_key": qr_result.qr_key}
        _auth_instances[platform_key] = auth
        logger.info("🔑 %s 二维码生成成功 (qr_key=%s)", platform_key, qr_result.qr_key)
        # Render QR code to PNG bytes
        img = qrcode.make(qr_result.qr_url)
        buf = io.BytesIO()
        # PIL.Image.save lacks type stubs; route kwargs through Any.
        save = cast(Any, img.save)
        save(buf, format="PNG")
        buf.seek(0)
        return Response(content=buf.getvalue(), media_type="image/png")
    except Exception:
        # Generation failed — clean up both caches and close the instance
        _qr_sessions.pop(platform_key, None)
        _auth_instances.pop(platform_key, None)
        await auth.close()
        raise


@router.get("/auth/poll/{platform_key}")
async def auth_poll(platform_key: str) -> dict[str, Any]:
    """Poll QR scan status and auto-complete on success.

    Reuses the authenticator instance cached by /auth/qr/{platform} so that
    stateful authenticators (e.g. XhsAuthenticator) preserve their internal
    XhsClient + init cookies across requests. The instance is only closed
    on terminal states (success / error) — closing after every poll would
    tear down the underlying HTTP session mid-flow.
    """
    logger.info("🔑 %s 轮询扫码状态...", platform_key)
    session = _qr_sessions.get(platform_key)
    if session is None:
        logger.warning("🔑 %s 无有效 QR session", platform_key)
        return {"status": "no_session"}

    # Reuse the cached authenticator if present (preserves XHS internal state);
    # otherwise construct a fresh one. Owned-by-this-block indicates whether
    # we are responsible for closing the instance if something fails.
    auth = _auth_instances.get(platform_key)
    owned = False
    if auth is None:
        auth = get_authenticator(platform_key)
        owned = True

    try:
        try:
            status = await auth.poll_qr_status(session["qr_key"])
        except Exception as exc:
            logger.warning("🔑 %s 轮询异常: %s", platform_key, exc)
            return {"status": "error", "message": f"轮询失败: {exc}"}

        if status.status != QRStatus.SUCCESS:
            return {"status": status.status.value}

        # Status is SUCCESS — get tokens
        logger.info("🔑 %s 扫码成功，获取凭证...", platform_key)
        try:
            tokens = await auth.get_tokens(session["qr_key"])
        except Exception as exc:
            # QR consumed but tokens not obtained — session will expire naturally
            logger.warning("🔑 %s 凭证获取失败: %s", platform_key, exc)
            await _cleanup_session_async(platform_key)
            return {"status": "error", "message": f"获取凭证失败: {exc}"}

        # Build auth_dict
        if platform_key in ("weibo", "xhs"):
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict: dict[str, Any] = {
                "cookie": cookie_str,
                "expires_at": tokens.expires_at,
                "nickname": tokens.nickname or "",
            }
        else:
            auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
        rt_val = getattr(auth, "refresh_token", None)
        if platform_key == "bili" and rt_val:
            auth_dict["refresh_token"] = rt_val
        try:
            await update_auth_section(platform_key, auth_dict)
        except Exception as exc:
            logger.warning("🔑 %s 凭证保存失败: %s", platform_key, exc)
            await _cleanup_session_async(platform_key)
            return {"status": "error", "message": f"保存凭证失败: {exc}"}
        logger.info("🔑 %s 凭证已保存", platform_key)
        await _cleanup_session_async(platform_key)
        return {"status": "success", "message": "登录成功"}
    finally:
        # Only close if WE constructed the instance AND it was not cached
        # (i.e. cached instance was missing — likely a poll-without-qr edge case).
        # For cached instances we must NOT close: subsequent polls need them.
        if owned:
            try:
                await auth.close()
            except Exception as exc:
                logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)


async def _cleanup_session_async(platform_key: str) -> None:
    """Drop the QR session, close + remove the cached authenticator."""
    _qr_sessions.pop(platform_key, None)
    auth = _auth_instances.pop(platform_key, None)
    if auth is None:
        return
    try:
        await auth.close()
    except Exception as exc:
        logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)
