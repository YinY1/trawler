"""B站认证管理 - QR 登录 + Token 续期"""

from __future__ import annotations

import logging
import time

from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)
from shared.config import Config

logger = logging.getLogger(__name__)


# ── 向后兼容 helper ───────────────────────────────────────────


def get_credential(config: Config):
    """从 config.bilibili.auth 构建 Credential。"""
    import bilibili_api

    auth = config.bilibili.auth
    if auth.sessdata and auth.bili_jct:
        return bilibili_api.Credential(
            sessdata=auth.sessdata,
            bili_jct=auth.bili_jct,
            buvid3=auth.buvid3 or "",
            dedeuserid=auth.dedeuserid or "",
        )
    logger.warning("未配置 B 站凭证，将以未登录状态运行")
    return bilibili_api.Credential()


# ── BilibiliAuthenticator ─────────────────────────────────────


class BilibiliAuthenticator(BaseAuthenticator):
    """B站 QR 扫码登录 + Cookie 续期"""

    def __init__(self, config_path: str = "config.toml") -> None:
        self._config_path = config_path
        self._last_ac_time_value: str = ""
        self._saved_cookies: dict[str, str] = {}
        self._refresh_token: str = ""

    @property
    def ac_time_value(self) -> str | None:
        return self._last_ac_time_value or None

    async def _get_http_session(self):
        from shared.http import get_session
        return await get_session()

    # ── BaseAuthenticator 接口 ────────────────────────────

    async def generate_qr_code(self) -> QRCodeResult:
        """纯手写 HTTP 申请二维码，不依赖任何库。"""
        session = await self._get_http_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
        }

        async with session.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            headers=headers,
        ) as resp:
            body = await resp.json()
            data = body.get("data", {})
            qr_url = data.get("url", "")
            qr_key = data.get("qrcode_key", "")
            return QRCodeResult(qr_url=qr_url, qr_key=qr_key, expires_in=180)

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        """纯手写 HTTP 轮询扫码状态，获取 Set-Cookie + refresh_token。"""
        session = await self._get_http_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
        }

        async with session.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qr_key},
            headers=headers,
        ) as resp:
            # 从 Set-Cookie 提取所有 cookie
            for cookie in resp.headers.getall("set-cookie", []):
                name = cookie.split("=", 1)[0]
                value = cookie.split(";")[0].split("=", 1)[1] if "=" in cookie else ""
                self._saved_cookies[name] = value

            body = await resp.json()
            data = body.get("data", {})
            code = data.get("code", -1)

            if code == 0:
                # 成功！保存 refresh_token 和 url
                self._refresh_token = data.get("refresh_token", "")
                self._saved_cookies["url"] = data.get("url", "")
                return AuthStatus(
                    success=True,
                    status=QRStatus.SUCCESS,
                    message="登录成功",
                )
            elif code == 86038:
                return AuthStatus(
                    success=False,
                    status=QRStatus.EXPIRED,
                    message="二维码已过期",
                )
            elif code == 86090:
                return AuthStatus(
                    success=False,
                    status=QRStatus.SCANNED,
                    message="已扫码，等待确认",
                )
            else:
                return AuthStatus(
                    success=False,
                    status=QRStatus.WAITING,
                    message="等待扫码",
                )

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        now = time.time()
        self._last_ac_time_value = self._refresh_token

        cookies: dict[str, str] = {}
        for key in ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "sid"):
            val = self._saved_cookies.get(key, "")
            if val:
                lower_key = key.lower()
                cookies[lower_key] = val

        return PlatformTokens(
            platform="bilibili",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 180 * 86400,
        )

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        import bilibili_api

        from shared.config import load_config

        cfg = load_config(self._config_path)
        ac_time_value = cfg.bilibili.auth.ac_time_value
        if not ac_time_value:
            raise RefreshFailedError("缺少 ac_time_value，无法续期，请重新扫码登录")

        cred = bilibili_api.Credential(
            sessdata=tokens.cookies.get("sessdata", ""),
            bili_jct=tokens.cookies.get("bili_jct", ""),
            buvid3=tokens.cookies.get("buvid3", ""),
            dedeuserid=tokens.cookies.get("dedeuserid", ""),
            ac_time_value=ac_time_value,
        )

        need = await cred.check_refresh()
        if not need:
            return tokens

        await cred.refresh()  # in-place mutation

        now = time.time()
        cookies: dict[str, str] = {}
        if cred.sessdata:
            cookies["sessdata"] = cred.sessdata
        if cred.bili_jct:
            cookies["bili_jct"] = cred.bili_jct
        if cred.dedeuserid:
            cookies["dedeuserid"] = cred.dedeuserid
        # 确保 buvid3 始终有值
        buvid3 = cred.buvid3 or tokens.cookies.get("buvid3", "") or (await bilibili_api.get_buvid())[0]
        cookies["buvid3"] = buvid3

        # 保留 ac_time_value 供下次续期
        self._last_ac_time_value = cred.ac_time_value or ac_time_value

        return PlatformTokens(
            platform="bilibili",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 180 * 86400,
        )

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        import bilibili_api

        if tokens.expires_at < time.time():
            return False
        cred = bilibili_api.Credential(
            sessdata=tokens.cookies.get("sessdata", ""),
            bili_jct=tokens.cookies.get("bili_jct", ""),
        )
        try:
            return await cred.check_valid()
        except Exception as e:
            logger.warning("B站 token 有效性检查失败: %s", e)
            return False

    def supports_refresh(self) -> bool:
        return True


def build_tokens_from_config(config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config.bilibili.auth. Returns None if not configured."""
    import time as _time
    auth = config.bilibili.auth
    if not auth.sessdata or not auth.bili_jct:
        return None
    if auth.expires_at <= 0:
        return None
    return PlatformTokens(
        platform="bilibili",
        cookies={
            "sessdata": auth.sessdata,
            "bili_jct": auth.bili_jct,
            "buvid3": auth.buvid3 or "",
            "dedeuserid": auth.dedeuserid or "",
        },
        obtained_at=_time.time(),
        expires_at=auth.expires_at,
    )
