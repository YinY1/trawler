"""微博认证管理 - QR 登录 + Cookie Keepalive 续期"""

from __future__ import annotations

import http.cookies
import logging
import time

import aiohttp

import shared.http  # noqa: E402 — module-level import for mock patching
from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)
from shared.config import Config
from shared.constants import WEIBO_POLL_TIMEOUT, WEIBO_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# 微博 QR 登录 API
QR_IMAGE_URL = "https://passport.weibo.com/sso/v2/qrcode/image?entry=miniblog&size=180"
QR_CHECK_URL = "https://passport.weibo.com/sso/v2/qrcode/check?entry=miniblog&qrid={qrid}"
QR_REFERER = "https://passport.weibo.com/"

# Cookie keepalive — 访问微博首页
KEEPALIVE_URL = "https://weibo.com"

# 微博 QR 状态 retcode 映射
# 50114001 = 未扫码, 50114002 = 已扫码待确认, 20000000 = 登录成功, 50114004 = 已过期
_QR_RETCODE_MAP: dict[int, QRStatus] = {
    50114001: QRStatus.WAITING,
    50114002: QRStatus.SCANNED,
    20000000: QRStatus.SUCCESS,
    50114004: QRStatus.EXPIRED,
}


def _parse_weibo_cookies(set_cookie_header: str | list[str]) -> dict[str, str]:
    """从 Set-Cookie 响应头解析微博 Cookie 键值对。

    支持传入单个字符串（逗号分隔的多 cookie）或字符串列表
    （每个元素一条 Set-Cookie）。每条 cookie 用 http.cookies.SimpleCookie
    独立解析，避免值内逗号导致错误分割。

    Args:
        set_cookie_header: 完整的 Set-Cookie 字符串或列表

    Returns:
        Cookie 键值对字典
    """
    cookies: dict[str, str] = {}
    if not set_cookie_header:
        return cookies

    # Normalize to list of individual cookie header strings
    if isinstance(set_cookie_header, str):
        # SimpleCookie can handle comma-separated multi-cookies
        try:
            sc = http.cookies.SimpleCookie(set_cookie_header)
            for key, morsel in sc.items():
                cookies[key] = morsel.value
            return cookies
        except http.cookies.CookieError:
            # Fallback: split on ", " (comma+space) for simple cases
            parts = [p.strip() for p in set_cookie_header.split(", ") if p.strip()]
            if len(parts) <= 1:
                return cookies
            set_cookie_header = parts

    # Each entry is a single Set-Cookie header
    for part in set_cookie_header:
        if not part or "=" not in part:
            continue
        try:
            sc = http.cookies.SimpleCookie(part)
            for key, morsel in sc.items():
                cookies[key] = morsel.value
        except http.cookies.CookieError:
            kv = part.split("=", 1)
            if len(kv) == 2:
                key = kv[0].strip()
                value = kv[1].split(";")[0].strip()
                if key and value:
                    cookies[key] = value
    return cookies


def _get_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )


class WeiboAuthenticator(BaseAuthenticator):
    """微博 QR 扫码登录 + Cookie Keepalive 续期"""

    def __init__(self) -> None:
        # 最近一次 poll_qr_status 成功返回的响应数据（retcode=20000000 时保存）
        self._last_check_data: dict | None = None

    # ── BaseAuthenticator 接口 ────────────────────────────

    async def generate_qr_code(self) -> QRCodeResult:
        session = await shared.http.get_session()
        resp = await session.get(
            QR_IMAGE_URL,
            headers={
                "User-Agent": _get_user_agent(),
                "Referer": QR_REFERER,
            },
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        )
        try:
            if resp.status != 200:
                raise RuntimeError(f"生成二维码失败，状态码: {resp.status}")
            data = await resp.json()
        finally:
            resp.close()

        qrid = data.get("data", {}).get("qrid", "")
        if not qrid:
            raise RuntimeError("生成二维码失败：未获取到 qrid")

        # 构造登录 URL，手机微博 App 扫描此 URL 后触发登录流程
        qr_url = f"https://passport.weibo.cn/signin/qrcode/scan?qr={qrid}&sinainternalbrowser=topnav&showmenu=0"
        return QRCodeResult(qr_url=qr_url, qr_key=qrid, expires_in=WEIBO_POLL_TIMEOUT)

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        session = await shared.http.get_session()
        url = QR_CHECK_URL.format(qrid=qr_key)
        resp = await session.get(
            url,
            headers={
                "User-Agent": _get_user_agent(),
                "Referer": QR_REFERER,
            },
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        )
        try:
            if resp.status != 200:
                logger.warning("轮询二维码状态失败，状态码: %s", resp.status)
                return AuthStatus(success=False, status=QRStatus.WAITING, message="请求失败")
            try:
                data = await resp.json(content_type=None)
            except Exception as json_err:
                logger.warning("轮询二维码响应非 JSON: %s, 状态码: %s", json_err, resp.status)
                return AuthStatus(success=False, status=QRStatus.WAITING, message="响应格式错误")
        finally:
            resp.close()

        if not isinstance(data, dict):
            logger.warning("轮询二维码响应数据类型异常: %s", type(data).__name__)
            data = {}

        retcode = data.get("retcode", 0)
        status = _QR_RETCODE_MAP.get(retcode, QRStatus.WAITING)

        # 保存成功响应，供 get_tokens 直接使用（避免重复请求 /check 导致 ticket 过期）
        if retcode == 20000000:
            self._last_check_data = data

        nickname = ""
        if isinstance(data.get("data"), dict):
            nickname = data["data"].get("nickname", "")

        msg_map: dict[QRStatus, str] = {
            QRStatus.WAITING: "等待扫码",
            QRStatus.SCANNED: f"已扫码 ({nickname})，等待确认" if nickname else "已扫码，等待确认",
            QRStatus.SUCCESS: "登录成功",
            QRStatus.EXPIRED: "二维码已过期",
        }
        return AuthStatus(
            success=status == QRStatus.SUCCESS,
            status=status,
            message=msg_map.get(status, f"未知状态 (retcode={retcode})"),
        )

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        """获取登录成功后的 Cookie。

        poll_qr_status 保存的 _last_check_data 包含：
        - retcode=20000000
        - data.url: 跨域登录 URL（访问此 URL 获取 Set-Cookie）
        """
        session = await shared.http.get_session()
        data = self._last_check_data

        if not isinstance(data, dict):
            raise RefreshFailedError("登录成功但无可用响应数据")

        retcode = data.get("retcode", 0)
        if retcode != 20000000:
            raise RefreshFailedError(f"二维码未成功登录 (retcode={retcode})")

        # 从 /check 响应中获取登录 URL
        login_url = ""
        if isinstance(data.get("data"), dict):
            login_url = data["data"].get("url", "")
        if not login_url:
            # 降级: 直接请求 /check 尝试捕获 Set-Cookie（如 302 重定向）
            resp = await session.get(
                QR_CHECK_URL.format(qrid=qr_key),
                headers={
                    "User-Agent": _get_user_agent(),
                    "Referer": QR_REFERER,
                },
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            )
            try:
                set_cookie = resp.headers.getall("Set-Cookie", [])
            finally:
                resp.close()
            if not set_cookie:
                raise RefreshFailedError("未获取到 Cookie 响应头")
        else:
            # 直接访问登录 URL 获取 Set-Cookie（不跟随重定向，捕获 302 中的 Set-Cookie）
            resp = await session.get(
                login_url,
                headers={"User-Agent": _get_user_agent()},
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            )
            try:
                set_cookie = resp.headers.getall("Set-Cookie", [])
            finally:
                resp.close()
            if not set_cookie:
                raise RefreshFailedError("未获取到 Cookie 响应头")

        cookies = _parse_weibo_cookies(set_cookie)
        if "SUB" not in cookies:
            raise RefreshFailedError("未获取到 SUB Cookie，登录可能失败")

        now = time.time()
        return PlatformTokens(
            platform="weibo",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 7 * 86400,  # 微博 Cookie 约 7 天
        )

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        """通过访问微博首页来保持 Cookie 活跃（keepalive）。

        访问 weibo.com，如果服务端返回新的 Set-Cookie，则更新 tokens。
        否则保持原有 tokens 不变。
        """
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        session = await shared.http.get_session()
        try:
            resp = await session.get(
                KEEPALIVE_URL,
                headers={
                    "User-Agent": _get_user_agent(),
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            )
            try:
                if resp.status != 200:
                    return tokens
                set_cookie = resp.headers.getall("Set-Cookie", [])
            finally:
                resp.close()

            if set_cookie:
                new_cookies = _parse_weibo_cookies(set_cookie)
                # 仅更新实际有值的字段
                updated_cookies = dict(tokens.cookies)
                updated_cookies.update(new_cookies)
                now = time.time()
                return PlatformTokens(
                    platform="weibo",
                    cookies=updated_cookies,
                    obtained_at=now,
                    expires_at=now + 7 * 86400,
                )

            # 没有新 cookie，返回原有 tokens
            return tokens
        except Exception as e:
            logger.warning("Keepalive 请求失败: %s", e)
            return tokens

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        if tokens.expires_at < time.time():
            return False
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        session = await shared.http.get_session()
        try:
            resp = await session.get(
                KEEPALIVE_URL,
                headers={
                    "User-Agent": _get_user_agent(),
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            )
            try:
                return resp.status == 200
            finally:
                resp.close()
        except Exception as e:
            logger.warning("微博 token 有效性检查失败: %s", e)
            return False

    def supports_refresh(self) -> bool:
        return True


def build_tokens_from_config(config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config.weibo.auth. Returns None if not configured."""
    auth = config.weibo.auth
    if not auth.cookie or auth.expires_at <= 0:
        return None
    cookie_dict: dict[str, str] = {}
    for part in auth.cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookie_dict[k] = v
    if not cookie_dict:
        return None
    return PlatformTokens(
        platform="weibo",
        cookies=cookie_dict,
        obtained_at=time.time(),
        expires_at=auth.expires_at,
    )
