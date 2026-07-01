"""Tests for shared.auth.scheduler — should_renew decision logic + check_and_renew writeback."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.auth.base import PlatformTokens
from shared.auth.scheduler import RenewalDecision, check_and_renew_tokens, should_renew
from shared.config import Config, RenewalConfig
from shared.exceptions import DataError

# ── Helpers ─────────────────────────────────────────────────────


def _make_tokens(
    platform: str = "bilibili",
    expires_offset: float = 0,
) -> PlatformTokens:
    """Create a PlatformTokens instance with expiry relative to now."""
    now = time.time()
    return PlatformTokens(
        platform=platform,
        cookies={"test": "cookie"},
        obtained_at=now - 1000,
        expires_at=now + expires_offset,
    )


@pytest.fixture
def default_config() -> RenewalConfig:
    return RenewalConfig()  # min_interval_hours=24, force_before_days=7


# ── Tests ───────────────────────────────────────────────────────


class TestShouldRenew:
    def test_expired_tokens(self, default_config: RenewalConfig):
        """Negative remaining time → expired, don't renew."""
        tokens = _make_tokens(expires_offset=-100)
        decision = should_renew(tokens, default_config)
        assert decision == RenewalDecision(False, "expired")

    def test_exactly_at_expiry(self, default_config: RenewalConfig):
        """Zero remaining time → expired (<=0 check)."""
        tokens = _make_tokens(expires_offset=0)
        decision = should_renew(tokens, default_config)
        assert decision == RenewalDecision(False, "expired")

    def test_force_threshold(self, default_config: RenewalConfig):
        """6 days remaining with force_before=7 days → force_soon."""
        tokens = _make_tokens(expires_offset=6 * 86400)
        decision = should_renew(tokens, default_config)
        assert decision == RenewalDecision(True, "force_soon")

    def test_exactly_at_force_threshold(self, default_config: RenewalConfig):
        """Exactly 7 days remaining → not_needed (not strictly less than).
        We use a tiny buffer above the exact threshold to account for time
        elapsed between token creation and should_renew() call.
        """
        tokens = _make_tokens(expires_offset=7 * 86400 + 1)  # just above threshold
        decision = should_renew(tokens, default_config)
        assert decision == RenewalDecision(False, "not_needed")

    def test_far_future(self, default_config: RenewalConfig):
        """30 days remaining → not_needed."""
        tokens = _make_tokens(expires_offset=30 * 86400)
        decision = should_renew(tokens, default_config)
        assert decision == RenewalDecision(False, "not_needed")

    def test_within_interval_zone(self):
        """Custom config: force_before=3d, min_interval=12d, token at 10d → within_interval."""
        config = RenewalConfig(min_interval_hours=12 * 24, force_before_days=3)
        tokens = _make_tokens(expires_offset=10 * 86400)
        decision = should_renew(tokens, config)
        assert decision == RenewalDecision(True, "within_interval")


class TestRenewalDecisionEquality:
    def test_equal(self):
        a = RenewalDecision(True, "force_soon")
        b = RenewalDecision(True, "force_soon")
        assert a == b

    def test_not_equal_reason(self):
        a = RenewalDecision(True, "force_soon")
        b = RenewalDecision(True, "within_interval")
        assert a != b

    def test_not_equal_should_renew(self):
        a = RenewalDecision(True, "expired")
        b = RenewalDecision(False, "expired")
        assert a != b


class TestDifferentPlatforms:
    @pytest.mark.parametrize("platform", ["bilibili", "xiaohongshu", "weibo"])
    def test_platforms_behave_same(self, default_config: RenewalConfig, platform: str):
        """All platforms use the same decision logic."""
        tokens = _make_tokens(platform=platform, expires_offset=6 * 86400)
        decision = should_renew(tokens, default_config)
        assert decision == RenewalDecision(True, "force_soon")


class TestCheckAndRenewSessionExpired:
    """check_and_renew_tokens: session-expired (-100) 时写回 expires_at=0。"""

    async def test_xhs_minus_100_writes_back_expires_zero(self) -> None:
        """XHS 服务端返回 -100 → expires_at=0 + 写回 cookies.toml。"""
        config = Config()
        config.xiaohongshu.auth.cookie = "fake=1"
        config.xiaohongshu.auth.expires_at = time.time() + 86400 * 6

        mock_auth = MagicMock()
        mock_auth.refresh_tokens = AsyncMock(
            side_effect=DataError("XHS data fetch error: {'code': -100, 'msg': '登录已过期'}")
        )
        mock_auth.close = AsyncMock()

        with (
            patch(
                "shared.auth.scheduler._get_authenticator_for_platform",
                return_value=mock_auth,
            ),
            patch(
                "shared.auth.scheduler._build_tokens_from_config",
                return_value=_make_tokens("xhs", expires_offset=86400 * 6),
            ),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await check_and_renew_tokens(
                "xhs", config, config_path="config/config.toml"
            )

        assert result.action == "expired"
        assert config.xiaohongshu.auth.expires_at == 0.0
        mock_update.assert_awaited_once_with(
            "xhs",
            {"expires_at": 0.0},
            config_path="config/config.toml",
        )

    async def test_generic_error_no_writeback(self) -> None:
        """非 -100 错误不写回 expires_at=0。"""
        config = Config()
        config.xiaohongshu.auth.cookie = "fake=1"
        config.xiaohongshu.auth.expires_at = time.time() + 86400 * 6

        mock_auth = MagicMock()
        mock_auth.refresh_tokens = AsyncMock(
            side_effect=RuntimeError("some random failure")
        )
        mock_auth.close = AsyncMock()

        with (
            patch(
                "shared.auth.scheduler._get_authenticator_for_platform",
                return_value=mock_auth,
            ),
            patch(
                "shared.auth.scheduler._build_tokens_from_config",
                return_value=_make_tokens("xhs", expires_offset=86400 * 6),
            ),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await check_and_renew_tokens(
                "xhs", config, config_path="config/config.toml"
            )

        assert result.action == "expired"
        # expires_at 未被修改（仍约 6 天后）
        assert config.xiaohongshu.auth.expires_at > time.time() + 86400 * 5
        mock_update.assert_not_awaited()

    async def test_bilibili_minus_100_writes_back_expires_zero(self) -> None:
        """bilibili 平台若抛 -100 DataError（罕见），也应写回 expires_at=0。"""
        config = Config()
        config.bilibili.auth.sessdata = "fake"
        config.bilibili.auth.expires_at = time.time() + 86400 * 6

        mock_auth = MagicMock()
        mock_auth.refresh_tokens = AsyncMock(
            side_effect=DataError("XHS data fetch error: {'code': -100, 'msg': '登录已过期'}")
        )
        mock_auth.close = AsyncMock()

        with (
            patch(
                "shared.auth.scheduler._get_authenticator_for_platform",
                return_value=mock_auth,
            ),
            patch(
                "shared.auth.scheduler._build_tokens_from_config",
                return_value=_make_tokens("bilibili", expires_offset=86400 * 6),
            ),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await check_and_renew_tokens(
                "bilibili", config, config_path="config/config.toml"
            )

        assert result.action == "expired"
        assert config.bilibili.auth.expires_at == 0.0
        mock_update.assert_awaited_once()
