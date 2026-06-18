from __future__ import annotations

# pyright: basic
import logging
import time
from dataclasses import dataclass
from typing import Any

from shared.auth.base import BaseAuthenticator, PlatformTokens
from shared.config import Config, RenewalConfig
from shared.protocols import RenewalResult


@dataclass
class RenewalDecision:
    """Decision result from should_renew()."""

    should_renew: bool
    reason: str  # "expired" | "force_soon" | "within_interval" | "not_needed"


def should_renew(
    tokens: PlatformTokens,
    config: RenewalConfig,
    last_refresh_at: float = 0.0,
) -> RenewalDecision:
    """Pure function: decide whether to renew tokens.

    Decision logic:
    1. Expired (time_to_expire <= 0) → don't renew, need re-login
    2. Force soon (time_to_expire < force_before_days * 86400) → force renew
    3. Within interval (time_to_expire < min_interval_hours * 3600) → renew
    4. Max interval exceeded (last_refresh_at > max_interval_hours ago) → renew
    5. Not needed → don't renew
    """
    now = time.time()
    time_to_expire = tokens.expires_at - now

    if time_to_expire <= 0:
        return RenewalDecision(False, "expired")

    force_threshold = config.force_before_days * 86400
    if time_to_expire < force_threshold:
        return RenewalDecision(True, "force_soon")

    min_interval = config.min_interval_hours * 3600
    if time_to_expire < min_interval:
        return RenewalDecision(True, "within_interval")

    # 距上次刷新尝试超过 max_interval_hours 也触发
    if last_refresh_at > 0 and config.max_interval_hours > 0:
        hours_since = (now - last_refresh_at) / 3600
        if hours_since >= config.max_interval_hours:
            return RenewalDecision(True, "max_interval_exceeded")

    return RenewalDecision(False, "not_needed")


logger = logging.getLogger(__name__)


async def check_and_renew_tokens(
    platform: str, config: Config, config_path: str = "config/config.toml"
) -> RenewalResult:
    """Check platform tokens and renew if needed.

    Called at the start of each trawler check run.
    Detects decayed tokens and refreshes them before content monitoring.
    """
    authenticator = _get_authenticator_for_platform(platform, config, config_path)
    if authenticator is None:
        return RenewalResult(platform, "not_configured", f"{platform}: 平台未配置或凭证缺失")

    tokens = _build_tokens_from_config(platform, config)
    if tokens is None:
        return RenewalResult(platform, "not_configured", f"{platform}: 凭证未配置")

    # 获取上次刷新尝试时间
    last_refresh_at = _get_last_refresh_at(platform, config)

    decision = should_renew(tokens, config.auth.renewal, last_refresh_at)
    if not decision.should_renew:
        if decision.reason == "expired":
            logger.warning(
                "%s token 已过期 (过期时间: %s)，请执行 trawler login --platform %s 重新登录",
                platform,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(tokens.expires_at)),
                platform,
            )
            return RenewalResult(platform, "expired", f"{platform}: token 已过期，请重新登录")
        return RenewalResult(platform, "skipped", f"{platform}: token 无需续期 ({decision.reason})")

    logger.info("%s token 需要续期 (%s)", platform, decision.reason)

    try:
        new_tokens = await authenticator.refresh_tokens(tokens)

        # 检查是否真的刷新了（obtained_at 更新说明 refresh_tokens 返回了新 tokens）
        if new_tokens.obtained_at > tokens.obtained_at:
            from shared.auth import update_auth_section

            await _update_last_refresh_at(platform, config, new_tokens.obtained_at, config_path)
            auth_dict = _tokens_to_auth_dict(platform, new_tokens, authenticator)
            await update_auth_section(platform, auth_dict, config_path=config_path)
            _update_config_memory(platform, config, new_tokens, authenticator)
            logger.info("%s token 续期成功", platform)
            return RenewalResult(platform, "renewed", f"{platform}: token 续期成功")
        else:
            logger.info("%s token 无需续期 (refresh_tokens 返回原始 tokens)", platform)
            return RenewalResult(platform, "skipped", f"{platform}: token 无需续期")
    except Exception as e:
        logger.warning("%s token 续期失败: %s", platform, e)
        return RenewalResult(platform, "expired", f"{platform}: token 续期失败 ({e})")


def _build_tokens_from_config(platform: str, config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config for the given platform."""
    import importlib

    module_map = {
        "bilibili": "platforms.bilibili.auth",
        "weibo": "platforms.weibo.auth",
        "xhs": "platforms.xiaohongshu.auth",
    }
    module_name = module_map.get(platform)
    if module_name is None:
        return None
    try:
        mod = importlib.import_module(module_name)
        return mod.build_tokens_from_config(config)
    except (ImportError, AttributeError):
        return None


def _get_authenticator_for_platform(
    platform: str, config: Config, config_path: str = "config/config.toml"
) -> BaseAuthenticator | None:
    """Get authenticator instance for the given platform."""
    if platform == "bilibili":
        from platforms.bilibili.auth import BilibiliAuthenticator

        return BilibiliAuthenticator(config_path=config_path)
    if platform == "weibo":
        from platforms.weibo.auth import WeiboAuthenticator

        return WeiboAuthenticator()
    if platform == "xhs":
        from platforms.xiaohongshu.auth import XhsAuthenticator

        return XhsAuthenticator()
    return None


def _tokens_to_auth_dict(platform: str, tokens: PlatformTokens, authenticator: Any) -> dict:
    """Convert PlatformTokens to config auth dict for token_store."""

    if platform == "bilibili":
        d: dict[str, object] = {
            "sessdata": tokens.cookies.get("sessdata", ""),
            "bili_jct": tokens.cookies.get("bili_jct", ""),
            "buvid3": tokens.cookies.get("buvid3", ""),
            "dedeuserid": tokens.cookies.get("dedeuserid", ""),
            "expires_at": tokens.expires_at,
        }
        if hasattr(authenticator, "_last_refresh_token") and authenticator._last_refresh_token:
            d["refresh_token"] = authenticator._last_refresh_token
        return d
    elif platform in ("weibo", "xhs"):
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        return {"cookie": cookie_str, "expires_at": tokens.expires_at}
    return {"expires_at": tokens.expires_at}


def _update_config_memory(platform: str, config: Config, tokens: PlatformTokens, authenticator=None) -> None:
    """Update in-memory config with renewed tokens so current run uses fresh credentials."""
    if platform == "bilibili":
        config.bilibili.auth.sessdata = tokens.cookies.get("sessdata", "")
        config.bilibili.auth.bili_jct = tokens.cookies.get("bili_jct", "")
        config.bilibili.auth.buvid3 = tokens.cookies.get("buvid3", "")
        config.bilibili.auth.dedeuserid = tokens.cookies.get("dedeuserid", "")
        config.bilibili.auth.expires_at = tokens.expires_at
        if authenticator and hasattr(authenticator, "_last_refresh_token") and authenticator._last_refresh_token:
            config.bilibili.auth.refresh_token = authenticator._last_refresh_token
    elif platform == "weibo":
        config.weibo.auth.cookie = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        config.weibo.auth.expires_at = tokens.expires_at
    elif platform == "xhs":
        config.xiaohongshu.auth.cookie = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        config.xiaohongshu.auth.expires_at = tokens.expires_at


def _get_last_refresh_at(platform: str, config: Config) -> float:
    """获取上次刷新尝试的时间戳。"""
    if platform == "bilibili":
        return config.bilibili.auth.last_refresh_at
    return 0.0


async def _update_last_refresh_at(platform: str, config: Config, timestamp: float, config_path: str) -> None:
    """更新上次刷新尝试时间到配置文件和内存。"""
    from shared.auth import update_auth_section

    if platform == "bilibili":
        config.bilibili.auth.last_refresh_at = timestamp
        await update_auth_section(platform, {"last_refresh_at": timestamp}, config_path=config_path)
