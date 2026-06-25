"""小红书认证模块 — 纯 Python QR 登录 + Cookie Keepalive

设计：
- 所有 HTTP 调用通过 ``XhsClient``，auth 层不再持有 ``aiohttp.ClientSession``
- QR 登录完全使用 ``XhsClient.create_qrcode`` / ``check_qrcode_status``
- 不再依赖 ``vendor/spider_xhs`` (Node.js + execjs)
"""

from __future__ import annotations

# pyright: basic
import asyncio
import binascii
import hashlib
import logging
import os
import random
import time
from collections.abc import Callable

from platforms.xiaohongshu.client import XhsClient
from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRExpiredError,
    QRStatus,
)
from shared.auth.qr_display import display_qr_in_terminal
from shared.config import Config
from shared.cookie_utils import build_cookie_str, parse_cookie_str

logger = logging.getLogger("trawler.xiaohongshu.auth")

_A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"


def get_xhs_cookie(config: Config) -> str:
    """从配置或环境变量获取小红书 Cookie。

    优先级: config.xiaohongshu.auth.cookie > 环境变量 XHS_COOKIE > 空字符串

    Args:
        config: 全局配置对象

    Returns:
        Cookie 字符串
    """
    cookie = config.xiaohongshu.auth.cookie
    if cookie:
        return cookie.strip()

    cookie = os.environ.get("TRAWLER_XHS_COOKIE", "")
    if cookie:
        return cookie.strip()

    logger.warning("未配置小红书 Cookie，API 请求可能失败")
    logger.warning("⚠ 未配置小红书 Cookie，请在 config/cookies.toml 或环境变量 TRAWLER_XHS_COOKIE 中设置")
    return ""


# ═══════════════════════════════════════════════════════════
# Helper functions for XhsAuthenticator
# ═══════════════════════════════════════════════════════════


def generate_a1() -> str:
    """Generate a random a1 cookie value (same algorithm as Spider_XHS)."""
    ts_hex = hex(int(time.time() * 1000))[2:]
    random_str = "".join(random.choices(_A1_CHARSET, k=30))
    a_part = ts_hex + random_str + "5" + "0" + "000"
    crc = binascii.crc32(a_part.encode()) & 0xFFFFFFFF
    return (a_part + str(crc))[:52]


def generate_web_id(a1: str) -> str:
    """Generate webId from a1 (MD5 hash)."""
    return hashlib.md5(a1.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════
# XhsAuthenticator — QR 登录 + Keepalive 续期 (纯 Python)
# ═══════════════════════════════════════════════════════════


class XhsAuthenticator(BaseAuthenticator):
    """小红书 QR 扫码登录 + Keepalive 保活续期 (纯 Python, 无 vendor 依赖)。"""

    def __init__(self) -> None:
        self._client: XhsClient | None = None
        self._init_cookies: dict[str, str] = {}
        self._qr_code: str = ""

    async def _ensure_client(self, cookie: str = "") -> XhsClient:
        """获取或创建内部 XhsClient。

        Args:
            cookie: 若非空，则强制使用该 cookie 创建/替换内部 client。
                    替换前会关闭旧 client 释放其 aiohttp session，
                    避免 "Unclosed client session" 警告。
        """
        if self._client is None or cookie:
            if cookie and self._client is not None:
                # close old client to release its aiohttp session before replacing
                await self._client.close()
            self._client = XhsClient(cookie=cookie)
        return self._client

    async def generate_qr_code(self) -> QRCodeResult:
        """生成 QR 二维码 (纯 Python, 通过 XhsClient)。"""
        logger.info("🔑 XhsAuthenticator 生成二维码...")
        a1 = generate_a1()
        init_cookies: dict[str, str] = {"a1": a1, "webId": generate_web_id(a1)}

        client = await self._ensure_client()
        sec = await client.fetch_sec_cookies(init_cookies)
        init_cookies.update(sec)

        qr_data = await client.create_qrcode(init_cookies)
        self._init_cookies = dict(init_cookies)
        self._qr_code = qr_data.get("code", "")

        return QRCodeResult(
            qr_url=qr_data.get("url", ""),   # server returns "url", not "qr_url"
            qr_key=qr_data["qr_id"],
            expires_in=180,
        )

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        """通过 XhsClient 轮询 QR 状态。

        状态码映射: 1=waiting, 2=scanned, 3=success, 4=expired
        """
        logger.info("🔑 XhsAuthenticator 轮询扫码状态...")
        client = await self._ensure_client()
        try:
            result = await client.check_qrcode_status(qr_key, self._qr_code)
        except Exception as e:
            return AuthStatus(success=False, status=QRStatus.WAITING, message=f"轮询失败: {e}")

        status_code = result.get("status", 1)
        if status_code == 3:
            return AuthStatus(success=True, status=QRStatus.SUCCESS, message="登录成功")
        elif status_code == 2:
            return AuthStatus(success=False, status=QRStatus.SCANNED, message="已扫描，请确认")
        elif status_code == 4:
            return AuthStatus(success=False, status=QRStatus.EXPIRED, message="二维码已过期")
        else:
            return AuthStatus(success=False, status=QRStatus.WAITING, message="等待扫描")

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        """从 XhsClient 当前 session 提取登录后的 cookies。"""
        logger.info("🔑 XhsAuthenticator 获取凭证...")
        now = time.time()
        client = self._client
        if client is None:
            return PlatformTokens(
                platform="xhs",
                cookies={},
                obtained_at=now,
                expires_at=now,
            )
        cookies = dict(client.cookies)
        return PlatformTokens(
            platform="xhs",
            cookies={k: v for k, v in cookies.items() if v},
            obtained_at=now,
            expires_at=now + 7 * 86400,
        )

    async def qr_login(
        self,
        on_status: Callable[[AuthStatus], None] | None = None,
    ) -> PlatformTokens:
        """纯 Python QR 扫码登录全流程 (通过 XhsClient)。"""
        qr = await self.generate_qr_code()
        display_qr_in_terminal(qr.qr_url)

        client = await self._ensure_client()
        deadline = time.monotonic() + qr.expires_in

        while time.monotonic() < deadline:
            status = await self.poll_qr_status(qr.qr_key)
            if on_status is not None:
                on_status(status)

            if status.status == QRStatus.SUCCESS:
                # 验证登录：调用 user/me 确认 cookies 有效。
                try:
                    await client.get_user_info()
                except Exception:
                    logger.warning("QR 登录后验证失败，但 cookies 可能仍有效")
                return await self.get_tokens(qr.qr_key)

            if status.status == QRStatus.EXPIRED:
                raise QRExpiredError("二维码已过期")

            await asyncio.sleep(2)

        raise QRExpiredError("二维码轮询超时")

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        """通过访问 XHS 主页捕获 Set-Cookie 续期。"""
        logger.info("🔑 XhsAuthenticator 续期 token...")
        client = await self._ensure_client(build_cookie_str(tokens.cookies))
        new_cookies = await client.refresh_cookies()

        cookies = dict(tokens.cookies)
        if new_cookies:
            cookies.update(new_cookies)

        now = time.time()
        logger.info("🔑 XhsAuthenticator token 续期完成")
        return PlatformTokens(
            platform="xhs",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 7 * 86400,
        )

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        """通过 XhsClient.probe() 验证 cookie 有效性。"""
        if tokens.expires_at < time.time():
            return False
        client = await self._ensure_client(build_cookie_str(tokens.cookies))
        try:
            return await client.probe()
        except Exception:
            return False

    async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
        """通过 XhsClient.get_user_info() 拉取当前登录账号昵称。

        复用内部 _client（按 token cookies 懒创建）。任一环节失败返回 None，
        不向上抛异常——保证 web auth 页面不会因 nickname 拉取失败而 500。
        """
        try:
            client = await self._ensure_client(build_cookie_str(tokens.cookies))
            info = await client.get_user_info()
            nick = info.get("nickname") if isinstance(info, dict) else None
            return nick or None
        except Exception as e:
            logger.warning("XHS nickname 获取失败: %s", e)
            return None

    def supports_refresh(self) -> bool:
        return True

    async def close(self) -> None:
        """关闭内部 XhsClient（及其懒创建的 aiohttp session）。"""
        if self._client is not None:
            await self._client.close()
            self._client = None


def build_tokens_from_config(config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config.xiaohongshu.auth. Returns None if not configured."""
    auth = config.xiaohongshu.auth
    if not auth.cookie or auth.expires_at <= 0:
        return None
    cookie_dict = parse_cookie_str(auth.cookie)
    if not cookie_dict:
        return None
    return PlatformTokens(
        platform="xhs",
        cookies=cookie_dict,
        obtained_at=time.time(),
        expires_at=auth.expires_at,
    )
