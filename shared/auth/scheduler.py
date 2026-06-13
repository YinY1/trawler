from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from shared.auth.base import PlatformTokens
from shared.config import Config, RenewalConfig


@dataclass
class RenewalDecision:
    """Decision result from should_renew()."""

    should_renew: bool
    reason: str  # "expired" | "force_soon" | "within_interval" | "not_needed"


def should_renew(tokens: PlatformTokens, config: RenewalConfig) -> RenewalDecision:
    """Pure function: decide whether to renew tokens.

    Decision logic:
    1. Expired (time_to_expire <= 0) → don't renew, need re-login
    2. Force soon (time_to_expire < force_before_days * 86400) → force renew
    3. Within interval (time_to_expire < min_interval_hours * 3600) → renew
    4. Not needed → don't renew
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

    return RenewalDecision(False, "not_needed")


logger = logging.getLogger(__name__)


@dataclass
class RenewalResult:
    """Result of a token check-and-renew operation."""

    platform: str
    action: str  # "skipped" | "renewed" | "expired" | "not_configured"
    message: str


async def check_and_renew_tokens(platform: str, config: Config) -> RenewalResult:
    """Check platform tokens and renew if needed.

    Called at the start of each trawler check run.
    Detects decayed tokens and refreshes them before content monitoring.
    """
    authenticator = _get_authenticator_for_platform(platform, config)
    if authenticator is None:
        return RenewalResult(platform, "not_configured", f"{platform}: 平台未配置或凭证缺失")

    tokens = authenticator.build_tokens_from_config(config)
    if tokens is None:
        return RenewalResult(platform, "not_configured", f"{platform}: 凭证未配置")

    decision = should_renew(tokens, config.auth.renewal)
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
        from shared.auth import update_auth_section

        auth_dict = _tokens_to_auth_dict(platform, new_tokens, authenticator)
        update_auth_section(platform, auth_dict)
        logger.info("%s token 续期成功", platform)
        return RenewalResult(platform, "renewed", f"{platform}: token 续期成功")
    except Exception as e:
        logger.warning("%s token 续期失败: %s", platform, e)
        return RenewalResult(platform, "expired", f"{platform}: token 续期失败 ({e})")


def _get_authenticator_for_platform(platform: str, config: Config):
    """Get authenticator instance with build_tokens_from_config method."""
    if platform == "bilibili":
        from platforms.bilibili.auth import BilibiliAuthenticator

        return BilibiliAuthenticator()
    if platform == "weibo":
        from platforms.weibo.auth import WeiboAuthenticator

        return WeiboAuthenticator()
    if platform == "xhs":
        from platforms.xiaohongshu.auth import XhsAuthenticator

        return XhsAuthenticator()
    return None


def _tokens_to_auth_dict(platform: str, tokens: PlatformTokens, authenticator) -> dict:
    """Convert PlatformTokens to config auth dict for token_store."""

    if platform == "bilibili":
        d = {**tokens.cookies, "expires_at": tokens.expires_at}
        if hasattr(authenticator, "_last_ac_time_value") and authenticator._last_ac_time_value:
            d["ac_time_value"] = authenticator._last_ac_time_value
        return d
    elif platform in ("weibo", "xhs"):
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        return {"cookie": cookie_str, "expires_at": tokens.expires_at}
    return {"expires_at": tokens.expires_at}
