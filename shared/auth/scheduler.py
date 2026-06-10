from __future__ import annotations

import time
from dataclasses import dataclass

from shared.auth.base import PlatformTokens
from shared.config import RenewalConfig


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
