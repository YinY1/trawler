"""小红书认证模块 — 基于 ReaJason/xhs 库的 QR 登录 + Token Keepalive

设计:
- HTTP 通过 ``AsyncXhsClient`` (异步包装 xhs 库), 不再持有 aiohttp session
- QR 登录使用 xhs 库 get_qrcode / check_qrcode (内置 sign + 自动补 gid)
- xhs 库异常通过 ``_wrap_xhs_call`` 装饰器转译到 trawler 异常体系
- refresh_tokens 退化为 validate-only (xhs 库无 refresh 概念)

See docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md
"""

from __future__ import annotations

# pyright: basic
import asyncio
import binascii
import functools
import hashlib
import logging
import os
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

import requests
from xhs.exception import DataFetchError, IPBlockError, NeedVerifyError, SignError

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
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
from shared.dump import DUMP_ENABLED, dump_response

# NOTE: trawler 的 IpBlockError(小写 p) 与 xhs.exception 的 IPBlockError(大写 P)
# 拼写不同,本模块顶部同时 import 两者:xhs 版在 line 22 的 xhs.exception import,
# trawler 版在此处。_wrap_xhs_call 里 except 块按名字区分(IPBlockError=捕获,
# IpBlockError=raise)。
from shared.exceptions import CaptchaError, DataError, IpBlockError, RetryableError

logger = logging.getLogger("trawler.xiaohongshu.auth")

_A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"

_F = TypeVar("_F", bound=Callable[..., Any])


# ═══════════════════════════════════════════════════════════
# Helper functions (UNCHANGED from previous auth.py)
# ═══════════════════════════════════════════════════════════

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
# Exception translation decorator (spec §5)
# ═══════════════════════════════════════════════════════════

def _wrap_xhs_call(func: _F) -> _F:
    """Translate xhs library exceptions to trawler's exception hierarchy.

    Mapping (spec §5.2) — except 顺序不可调,具体在前,RequestException 兜底最后:
      NeedVerifyError → CaptchaError
      IPBlockError    → IpBlockError
      SignError       → RetryableError
      DataFetchError  → DataError
      RequestException→ RetryableError  (catch-all, ordered LAST)

    签名说明(最终版,不要再让 fixer 改):
    - 用 `_F = TypeVar("_F", bound=Callable[..., Any])` + `def _wrap_xhs_call(func: _F) -> _F`
      保证被装饰函数签名透传,pyright strict 通过。
    - `return wrapper  # type: ignore[return-value]` 是必须的:wrapper 是新 async 函数,
      与 _F 不同型,这是异步装饰器的标准 pyright 兜底,无需绕开。
    - 不用 ParamSpec(P.args/P.kwargs 在 pyright strict 下对 async 装饰器报错更多)。
    """
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except NeedVerifyError as e:
            raise CaptchaError(f"XHS captcha challenge: {e}") from e
        except IPBlockError as e:
            # NOTE: 这里 raise 的是 trawler 的 IpBlockError(shared.exceptions),
            # 不是 xhs 库的 IPBlockError(xhs.exception)。两者拼法不同
            # (trawler: Ip / xhs: IP)。顶部已 `from shared.exceptions import
            # IpBlockError`,本 except 块捕获的 IPBlockError 来自 xhs.exception
            # 的顶部 import,无需 lazy import。
            raise IpBlockError(f"XHS IP blocked: {e}") from e
        except SignError as e:
            raise RetryableError(f"XHS sign error: {e}") from e
        except DataFetchError as e:
            raise DataError(f"XHS data fetch error: {e}") from e
        except requests.RequestException as e:
            raise RetryableError(f"XHS network error: {e}") from e

    return wrapper  # type: ignore[return-value]


# ═══════════════════════════════════════════════════════════
# XhsAuthenticator — QR 登录 + validate-only refresh (via xhs lib)
# ═══════════════════════════════════════════════════════════

class XhsAuthenticator(BaseAuthenticator):
    """小红书 QR 扫码登录 (通过 ReaJason/xhs 库)。

    xhs 库无 refresh 概念, refresh_tokens 退化为 validate-only:
    成功 → 返回原 tokens + bumped expires_at; 失败 → 抛异常。
    """

    def __init__(self) -> None:
        self._client: AsyncXhsClient | None = None
        self._qr_id: str = ""
        self._code: str = ""

    @_wrap_xhs_call
    async def generate_qr_code(self) -> QRCodeResult:
        """生成 QR 二维码 (spec §4.1)."""
        logger.info("🔑 XhsAuthenticator 生成二维码...")
        a1 = generate_a1()
        web_id = generate_web_id(a1)
        init_cookie = f"a1={a1}; webId={web_id}"

        # New AsyncXhsClient per QR session; close any prior cached one.
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.debug("close prior client failed", exc_info=True)
        self._client = AsyncXhsClient(cookie=init_cookie)

        qr_data = await self._client.get_qrcode()
        self._qr_id = qr_data.get("qr_id", "")
        self._code = qr_data.get("code", "")

        return QRCodeResult(
            qr_url=qr_data.get("url", ""),
            qr_key=self._qr_id,
            expires_in=180,
        )

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        """轮询 QR 状态 (spec §4.2). 字段名 code_status (snake_case!)."""
        logger.info("🔑 XhsAuthenticator 轮询扫码状态...")
        if self._client is None:
            return AuthStatus(success=False, status=QRStatus.WAITING, message="无 client")
        try:
            result = await self._client.check_qrcode(self._qr_id, self._code)
        except Exception as e:
            logger.warning("🔑 轮询异常: %s", e)
            return AuthStatus(success=False, status=QRStatus.WAITING, message=f"轮询失败: {e}")

        # TEMP DEBUG DUMP: check_qrcode 完整返回落盘
        if DUMP_ENABLED:
            dump_response("xhs_poll", result)

        code_status = result.get("code_status", 0)  # ← 关键修复: snake_case
        if code_status == 2:
            return AuthStatus(success=True, status=QRStatus.SUCCESS, message="登录成功")
        elif code_status == 1:
            return AuthStatus(success=False, status=QRStatus.SCANNED, message="已扫描,请确认")
        elif code_status == 3:
            return AuthStatus(success=False, status=QRStatus.EXPIRED, message="二维码已过期")
        else:
            return AuthStatus(success=False, status=QRStatus.WAITING, message="等待扫描")

    @_wrap_xhs_call
    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        """SUCCESS 后提取登录后 cookies (spec §4.3)."""
        logger.info("🔑 XhsAuthenticator 获取凭证...")
        now = time.time()
        if self._client is None:
            return PlatformTokens(platform="xhs", cookies={}, obtained_at=now, expires_at=now)

        await self._client.activate()
        full_cookie_str = self._client.cookie

        # TEMP DEBUG DUMP: activate() 后 cookie 字符串落盘
        if DUMP_ENABLED:
            dump_response("xhs_get_tokens", {"cookie": full_cookie_str})

        cookie_dict = parse_cookie_str(full_cookie_str)
        return PlatformTokens(
            platform="xhs",
            cookies={k: v for k, v in cookie_dict.items() if v},
            obtained_at=now,
            expires_at=now + 7 * 86400,
        )

    async def qr_login(
        self,
        on_status: Callable[[AuthStatus], None] | None = None,
    ) -> PlatformTokens:
        """QR 扫码登录全流程 (spec §4.1-4.3 串联).

        Note: 复用 BaseAuthenticator.qr_login 默认实现也行, 但这里覆盖以
        保留 generate_qr_code 内的 client caching 语义 + display_qr_in_terminal.
        """
        qr = await self.generate_qr_code()
        display_qr_in_terminal(qr.qr_url)

        deadline = time.monotonic() + qr.expires_in
        while time.monotonic() < deadline:
            status = await self.poll_qr_status(qr.qr_key)
            if on_status is not None:
                on_status(status)
            if status.status == QRStatus.SUCCESS:
                return await self.get_tokens(qr.qr_key)
            if status.status == QRStatus.EXPIRED:
                raise QRExpiredError("二维码已过期")
            await asyncio.sleep(2)
        raise QRExpiredError("二维码轮询超时")

    @_wrap_xhs_call
    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        """Refresh 退化为 validate-only (spec §4.5).

        成功 → 返回原 tokens + bumped expires_at
        失败 → 抛异常 (caller 让用户重扫码)
        """
        logger.info("🔑 XhsAuthenticator 续期 token (validate-only)...")
        client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
        try:
            info = await client.get_self_info()
            if not info.get("nickname"):
                raise DataError("cookie 无效: get_self_info 返回空 nickname")
        finally:
            await client.close()

        return PlatformTokens(
            platform="xhs",
            cookies=dict(tokens.cookies),
            obtained_at=time.time(),
            expires_at=time.time() + 7 * 86400,
        )

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        """验证 cookie 有效性 (spec §4.5)."""
        if tokens.expires_at < time.time():
            return False
        client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
        try:
            try:
                info = await client.get_self_info()
                return bool(info.get("nickname"))
            except Exception as e:
                logger.debug("validate_tokens probe failed: %s", e)
                return False
        finally:
            await client.close()

    async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
        """获取当前用户昵称. MUST NOT raise — 失败返回 None (spec §4.4)."""
        client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
        try:
            try:
                info = await client.get_self_info()
                nick = info.get("nickname") if isinstance(info, dict) else None
                return nick or None
            except Exception as e:
                logger.warning("XHS nickname 获取失败: %s", e)
                return None
        finally:
            await client.close()

    def supports_refresh(self) -> bool:
        return True

    async def close(self) -> None:
        """关闭缓存的 client (如有)."""
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                logger.debug("close client failed: %s", e)
            self._client = None


# ═══════════════════════════════════════════════════════════
# build_tokens_from_config (UNCHANGED from previous auth.py)
# ═══════════════════════════════════════════════════════════

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
