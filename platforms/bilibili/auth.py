"""B站认证管理 - QR 登录 + Token 续期"""

from __future__ import annotations

import logging
import time

import bilibili_api
from bilibili_api import login_v2

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

# ── bilibili_api QrCodeLoginEvents → QRStatus 映射 ────────────
_EVENT_MAP: dict[login_v2.QrCodeLoginEvents, QRStatus] = {
    login_v2.QrCodeLoginEvents.SCAN: QRStatus.WAITING,
    login_v2.QrCodeLoginEvents.CONF: QRStatus.SCANNED,
    login_v2.QrCodeLoginEvents.DONE: QRStatus.SUCCESS,
    login_v2.QrCodeLoginEvents.TIMEOUT: QRStatus.EXPIRED,
}


# ── 向后兼容 helper ───────────────────────────────────────────


def get_credential(config: Config) -> bilibili_api.Credential:
    """从 config.bilibili.auth (NEW path) 构建 Credential。

    保持原有调用签名不变，供现有业务代码直接使用。
    """
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
        self._qr_login: login_v2.QrCodeLogin | None = None
        self._last_ac_time_value: str = ""

    # ── 内部工具 ──────────────────────────────────────────

    def _get_qr_login(self) -> login_v2.QrCodeLogin:
        if self._qr_login is None:
            self._qr_login = login_v2.QrCodeLogin()
        return self._qr_login

    # ── BaseAuthenticator 接口 ────────────────────────────

    async def generate_qr_code(self) -> QRCodeResult:
        qr = self._get_qr_login()
        await qr.generate_qrcode()
        # generate_qrcode 将 url/key 存入内部属性
        qr_url: str = qr.get_qrcode_terminal()  # 终端可渲染字符串
        qr_key: str = qr._QrCodeLogin__qr_key  # noqa: SLF001
        return QRCodeResult(qr_url=qr_url, qr_key=qr_key, expires_in=180)

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        qr = self._get_qr_login()
        event = await qr.check_state()
        status = _EVENT_MAP.get(event, QRStatus.WAITING)
        msg_map: dict[QRStatus, str] = {
            QRStatus.WAITING: "等待扫码",
            QRStatus.SCANNED: "已扫码，等待确认",
            QRStatus.SUCCESS: "登录成功",
            QRStatus.EXPIRED: "二维码已过期",
        }
        return AuthStatus(
            success=status == QRStatus.SUCCESS,
            status=status,
            message=msg_map.get(status, "未知状态"),
        )

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        qr = self._get_qr_login()
        # 确保 DONE 状态
        event = await qr.check_state()
        if event != login_v2.QrCodeLoginEvents.DONE:
            raise RefreshFailedError("二维码未确认，无法获取 token")

        cred = qr.get_credential()  # 非异步，返回 Credential
        now = time.time()
        # 提取 ac_time_value 供 CLI 持久化
        self._last_ac_time_value = cred.ac_time_value or ""

        cookies: dict[str, str] = {}
        if cred.sessdata:
            cookies["SESSDATA"] = cred.sessdata
        if cred.bili_jct:
            cookies["bili_jct"] = cred.bili_jct
        if cred.dedeuserid:
            cookies["DedeUserID"] = cred.dedeuserid
        if cred.buvid3:
            cookies["buvid3"] = cred.buvid3

        return PlatformTokens(
            platform="bilibili",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 180 * 86400,  # ~6 months
        )

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        from shared.config import load_config

        cfg = load_config(self._config_path)
        ac_time_value = cfg.bilibili.auth.ac_time_value
        if not ac_time_value:
            raise RefreshFailedError("缺少 ac_time_value，无法续期，请重新扫码登录")

        cred = bilibili_api.Credential(
            sessdata=tokens.cookies.get("SESSDATA", ""),
            bili_jct=tokens.cookies.get("bili_jct", ""),
            buvid3=tokens.cookies.get("buvid3", ""),
            dedeuserid=tokens.cookies.get("DedeUserID", ""),
            ac_time_value=ac_time_value,
        )

        need = await cred.check_refresh()
        if not need:
            return tokens

        await cred.refresh()  # in-place mutation

        now = time.time()
        cookies: dict[str, str] = {}
        if cred.sessdata:
            cookies["SESSDATA"] = cred.sessdata
        if cred.bili_jct:
            cookies["bili_jct"] = cred.bili_jct
        if cred.dedeuserid:
            cookies["DedeUserID"] = cred.dedeuserid
        if cred.buvid3:
            cookies["buvid3"] = cred.buvid3

        # 保留 ac_time_value 供下次续期
        self._last_ac_time_value = cred.ac_time_value or ac_time_value

        return PlatformTokens(
            platform="bilibili",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 180 * 86400,
        )

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        if tokens.expires_at < time.time():
            return False
        cred = bilibili_api.Credential(
            sessdata=tokens.cookies.get("SESSDATA", ""),
            bili_jct=tokens.cookies.get("bili_jct", ""),
        )
        try:
            return await cred.check_valid()
        except Exception:
            return False

    def supports_refresh(self) -> bool:
        return True
