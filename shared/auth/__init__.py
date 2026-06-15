from __future__ import annotations

# pyright: basic
from shared.auth.base import (
    AuthError,
    AuthStatus,
    BaseAuthenticator,
    NetworkError,
    PlatformTokens,
    QRCodeResult,
    QRExpiredError,
    QRStatus,
    RefreshFailedError,
    TokenInvalidError,
)
from shared.auth.qr_display import display_qr_in_terminal

__all__ = [
    "AuthError",
    "AuthStatus",
    "BaseAuthenticator",
    "NetworkError",
    "PlatformTokens",
    "QRExpiredError",
    "QRCodeResult",
    "QRStatus",
    "RefreshFailedError",
    "TokenInvalidError",
    "display_qr_in_terminal",
    "get_authenticator",
    "update_auth_section",
]


_PLATFORM_TABLE = {"xhs": "xiaohongshu", "bili": "bilibili", "weibo": "weibo"}


def get_authenticator(platform: str) -> BaseAuthenticator:
    """Factory: get platform authenticator instance."""
    if platform == "bili":
        from platforms.bilibili.auth import BilibiliAuthenticator

        return BilibiliAuthenticator()
    if platform == "weibo":
        from platforms.weibo.auth import WeiboAuthenticator

        return WeiboAuthenticator()
    if platform == "xhs":
        from platforms.xiaohongshu.auth import XhsAuthenticator

        return XhsAuthenticator()
    raise ValueError(f"Unsupported platform: {platform}")


async def update_auth_section(platform: str, auth_dict: dict, config_path: str = "config/config.toml") -> None:
    """Update [platform.auth] section in cookies.toml (derived from config_path)."""
    from shared.auth.token_store import update_auth_section as _update

    table = _PLATFORM_TABLE.get(platform, platform)
    await _update(config_path=config_path, platform=table, auth_dict=auth_dict)
