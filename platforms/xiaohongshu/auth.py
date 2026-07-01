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
import hashlib
import logging
import os
import random
import time
from collections.abc import Callable

from xhs.exception import NeedVerifyError

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient, _wrap_xhs_call
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
from shared.exceptions import DataError

logger = logging.getLogger("trawler.xiaohongshu.auth")

_A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"


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


def _extract_nickname(info: dict) -> str | None:
    """从 xhs selfinfo 响应提取 nickname。

    xhs 库 /api/sns/web/v1/user/selfinfo 返回嵌套结构:
        {"basic_info": {"nickname": "..."}}
    v2 /api/sns/web/v2/user/me 返回扁平结构:
        {"nickname": "..."}
    双路径兜底。
    """
    basic = info.get("basic_info")
    if isinstance(basic, dict):
        nick = basic.get("nickname")
        if nick:
            return nick
    return info.get("nickname") or None


# ═══════════════════════════════════════════════════════════
# Exception translation: import _wrap_xhs_call from async_xhs_wrapper (spec §3.1.2)
# ═══════════════════════════════════════════════════════════


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
        self._captcha_state: dict[str, str] | None = None

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
        """轮询 QR 状态 (spec §4.2). 字段名 code_status (snake_case!).

        当 check_qrcode 触发 NeedVerifyError 时进入二次验证流程:
        1. 首次调 captcha_init 拿 rid, 构造第二 QR URL 返回 CAPTCHA
        2. 后续调 captcha_query_status 等确认(4=已确认)
        3. 确认后清状态, 重试 check_qrcode
        """
        logger.info("🔑 XhsAuthenticator 轮询扫码状态...")
        if self._client is None:
            return AuthStatus(success=False, status=QRStatus.WAITING, message="无 client")

        # ── 当前轮已在二次验证中 ──
        if self._captcha_state is not None:
            return await self._poll_captcha_status()

        # ── 正常轮询 ──
        try:
            result = await self._client.check_qrcode(self._qr_id, self._code)
        except NeedVerifyError as e:
            return await self._handle_captcha(e)
        except Exception as e:
            logger.warning("🔑 轮询异常: %s", e)
            return AuthStatus(success=False, status=QRStatus.WAITING, message=f"轮询失败: {e}")

        # TEMP DEBUG DUMP: check_qrcode 完整返回落盘
        if DUMP_ENABLED:
            dump_response("xhs_poll", result)

        code_status = result.get("code_status", 0)
        if code_status == 2:
            return AuthStatus(success=True, status=QRStatus.SUCCESS, message="登录成功")
        elif code_status == 1:
            return AuthStatus(success=False, status=QRStatus.SCANNED, message="已扫描,请确认")
        elif code_status == 3:
            return AuthStatus(success=False, status=QRStatus.EXPIRED, message="二维码已过期")
        else:
            return AuthStatus(success=False, status=QRStatus.WAITING, message="等待扫描")

    async def _handle_captcha(self, e: NeedVerifyError) -> AuthStatus:
        """首次触达二次验证码: 调 captcha_init 拿 rid, 构造第二 QR URL。

        即使初始化失败也返回 CAPTCHA 状态(空 qr_url),
        避免前端陷入无重试的死循环。
        """
        assert self._client is not None  # caller 已 guard
        try:
            data = await self._client.captcha_init(
                str(e.verify_type) if e.verify_type else "",
                str(e.verify_uuid) if e.verify_uuid else "",
            )
            rid = ""
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, dict):
                    rid = inner.get("rid", "")
                if not rid and isinstance(data, dict):
                    rid = data.get("rid", "")
            self._captcha_state = {
                "verify_type": str(e.verify_type) if e.verify_type else "",
                "verify_uuid": str(e.verify_uuid) if e.verify_uuid else "",
                "rid": rid,
            }
            web_id = self._get_web_id()
            qr_url = (
                f"https://www.xiaohongshu.com/web-login/qrcode-transfer"
                f"?rid={rid}&verifyUuid={e.verify_uuid}"
                f"&verifyBiz=471&verifyType={e.verify_type}&webid={web_id}"
            )
            return AuthStatus(success=False, status=QRStatus.CAPTCHA, message=qr_url)
        except Exception:
            logger.warning("🔑 二次验证初始化失败", exc_info=True)
            return AuthStatus(success=False, status=QRStatus.CAPTCHA, message="")

    async def _poll_captcha_status(self) -> AuthStatus:
        """二次验证轮询: 查 captcha_query_status, 4=已确认→回正常轮询。"""
        assert self._client is not None  # caller 已 guard
        try:
            s = self._captcha_state
            assert s is not None
            result = await self._client.captcha_query_status(
                s["verify_type"], s["verify_uuid"], s["rid"]
            )
            status_code = 0
            if isinstance(result, dict):
                inner = result.get("data")
                if isinstance(inner, dict):
                    status_code = inner.get("status", 0)
                if not status_code:
                    status_code = result.get("status", 0)

            if status_code == 4:
                # 二次验证已确认 — 清状态, 重试 check_qrcode
                self._captcha_state = None
                logger.info("🔑 二次验证已确认, 重试 check_qrcode")
                try:
                    retry = await self._client.check_qrcode(self._qr_id, self._code)
                except Exception as e:
                    logger.warning("🔑 重试 check_qrcode 异常: %s", e)
                    return AuthStatus(success=False, status=QRStatus.WAITING, message=f"轮询失败: {e}")
                else:
                    return self._code_status_to_auth_status(retry)
            return AuthStatus(success=False, status=QRStatus.CAPTCHA, message="")
        except Exception:
            logger.warning("🔑 二次验证轮询异常", exc_info=True)
            return AuthStatus(success=False, status=QRStatus.CAPTCHA, message="")

    def _code_status_to_auth_status(self, result: dict) -> AuthStatus:
        """将 check_qrcode 的 code_status 翻译成 AuthStatus。"""
        code_status = result.get("code_status", 0)
        if code_status == 2:
            return AuthStatus(success=True, status=QRStatus.SUCCESS, message="登录成功")
        elif code_status == 1:
            return AuthStatus(success=False, status=QRStatus.SCANNED, message="已扫描,请确认")
        elif code_status == 3:
            return AuthStatus(success=False, status=QRStatus.EXPIRED, message="二维码已过期")
        else:
            return AuthStatus(success=False, status=QRStatus.WAITING, message="等待扫描")

    def _get_web_id(self) -> str:
        """从 cookie 提取 webId 值。"""
        cookie = self._client.cookie if self._client else ""
        for part in cookie.split(";"):
            part_stripped = part.strip()
            if part_stripped.lower().startswith("webid="):
                return part_stripped.split("=", 1)[1]
        return ""

    @_wrap_xhs_call
    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        """SUCCESS 后提取登录后 cookies (spec §4.3).

        activate() 已删除:真机证伪它会用设备指纹生成匿名 session,
        覆盖 check_qrcode 写入的真实用户 session(spec v2 §4a)。
        check_qrcode SUCCESS 的 Set-Cookie 已把真实 web_session 写入 jar,
        此处直接读 self._client.cookie 即可。

        best-effort 调 get_self_info 填 tokens.nickname:失败不阻断登录主流程
        (降级为 None,后续可走 get_user_nickname 实时获取)。
        """
        logger.info("🔑 XhsAuthenticator 获取凭证...")
        now = time.time()
        if self._client is None:
            return PlatformTokens(
                platform="xhs", cookies={}, obtained_at=now, expires_at=now,
                nickname=None,
            )

        # check_qrcode 已把真实 session 写入 cookie jar,直接读
        full_cookie_str = self._client.cookie

        # 尝试拿 nickname(失败不阻断登录主流程)
        nickname: str | None = None
        try:
            info = await self._client.get_self_info()
            if isinstance(info, dict):
                nickname = _extract_nickname(info)
        except Exception as e:
            logger.warning("XHS get_self_info 拿 nickname 失败: %s", e)

        cookie_dict = parse_cookie_str(full_cookie_str)
        return PlatformTokens(
            platform="xhs",
            cookies={k: v for k, v in cookie_dict.items() if v},
            obtained_at=now,
            expires_at=now + 7 * 86400,
            nickname=nickname,
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
            if not _extract_nickname(info):
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
                return bool(_extract_nickname(info))
            except Exception as e:
                logger.debug("validate_tokens probe failed: %s", e)
                return False
        finally:
            await client.close()

    async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
        """获取当前用户昵称. MUST NOT raise — 失败返回 None (spec §4.4 / spec v2 §4c).

        优先读 ``tokens.nickname``(登录时或从 config 加载时填入):
        - 命中 → 直接返回,不调 API
        - 缺失(从 config 加载的旧 token) → 降级调 get_self_info
        """
        if tokens.nickname:
            return tokens.nickname
        try:
            # 降级:tokens 里没有(从 config 加载的旧 token),再尝试 API
            client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
            try:
                info = await client.get_self_info()
                nick = _extract_nickname(info) if isinstance(info, dict) else None
                return nick or None
            finally:
                await client.close()
        except Exception as e:
            logger.warning("XHS nickname 获取失败: %s", e)
            return None

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
        nickname=auth.nickname or None,
    )
