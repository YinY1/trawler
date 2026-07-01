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


# ── Issue #68: max_interval_hours 对 xhs/weibo 生效 ──────────────


class TestMaxIntervalAllPlatforms:
    """max_interval_hours 决策对三平台统一生效（不再只对 bilibili 有效）。

    构造场景：token 还很新（剩余 30 天，远超 min_interval=24h 与 force_before=7d），
    此时 should_renew 唯一可能触发的分支是 max_interval_exceeded。
    """

    EXPIRES_OFFSET = 30 * 86400  # 30 天后过期，避开 force_soon / within_interval

    def test_bilibili_max_interval_still_works(self) -> None:
        """回归：bilibili 的 max_interval 仍生效。"""
        config = RenewalConfig(max_interval_hours=24)
        tokens = _make_tokens("bilibili", expires_offset=self.EXPIRES_OFFSET)
        # last_refresh_at 在 48h 前 → 超过 max_interval=24h
        stale = time.time() - 48 * 3600
        decision = should_renew(tokens, config, last_refresh_at=stale)
        assert decision == RenewalDecision(True, "max_interval_exceeded")

    def test_xhs_max_interval_triggers_renew_when_stale(self) -> None:
        """xhs: last_refresh_at 超过 max_interval_hours → should_renew=True。"""
        config = RenewalConfig(max_interval_hours=24)
        tokens = _make_tokens("xhs", expires_offset=self.EXPIRES_OFFSET)
        stale = time.time() - 48 * 3600
        decision = should_renew(tokens, config, last_refresh_at=stale)
        assert decision == RenewalDecision(True, "max_interval_exceeded")

    def test_weibo_max_interval_triggers_renew_when_stale(self) -> None:
        """weibo: last_refresh_at 超过 max_interval_hours → should_renew=True。"""
        config = RenewalConfig(max_interval_hours=24)
        tokens = _make_tokens("weibo", expires_offset=self.EXPIRES_OFFSET)
        stale = time.time() - 48 * 3600
        decision = should_renew(tokens, config, last_refresh_at=stale)
        assert decision == RenewalDecision(True, "max_interval_exceeded")

    def test_max_interval_not_triggered_when_fresh(self) -> None:
        """last_refresh_at 在 max_interval 内 → not_needed。"""
        config = RenewalConfig(max_interval_hours=24)
        tokens = _make_tokens("xhs", expires_offset=self.EXPIRES_OFFSET)
        fresh = time.time() - 6 * 3600  # 6h 前，未超 24h
        decision = should_renew(tokens, config, last_refresh_at=fresh)
        assert decision == RenewalDecision(False, "not_needed")


class TestGetLastRefreshAtAllPlatforms:
    """_get_last_refresh_at 对三平台都能读出 auth.last_refresh_at。"""

    def test_bilibili(self) -> None:
        from shared.auth.scheduler import _get_last_refresh_at

        config = Config()
        ts = 1700000000.0
        config.bilibili.auth.last_refresh_at = ts
        assert _get_last_refresh_at("bilibili", config) == ts

    def test_xhs(self) -> None:
        from shared.auth.scheduler import _get_last_refresh_at

        config = Config()
        ts = 1700000000.0
        config.xiaohongshu.auth.last_refresh_at = ts
        assert _get_last_refresh_at("xhs", config) == ts

    def test_weibo(self) -> None:
        from shared.auth.scheduler import _get_last_refresh_at

        config = Config()
        ts = 1700000000.0
        config.weibo.auth.last_refresh_at = ts
        assert _get_last_refresh_at("weibo", config) == ts


class TestUpdateLastRefreshAtWritesTimestamp:
    """续期成功后 last_refresh_at 被更新（内存 + 写盘），三平台一致。"""

    async def _run_renew_success(self, platform: str, config: Config) -> None:
        """Helper: 触发一次续期成功路径，断言 last_refresh_at 被写回。

        旧 token 处于 force_soon 区间（剩余 6 天 < force_before=7d）以触发续期；
        refresh_tokens 返回 obtained_at 更新的新 token，validate_tokens 通过。
        """
        now = time.time()
        new_tokens = PlatformTokens(
            platform=platform,
            cookies={"k": "v"},
            obtained_at=now,  # > 旧 tokens.obtained_at，触发"真的刷新了"
            expires_at=now + 30 * 86400,
        )
        mock_auth = MagicMock()
        mock_auth.refresh_tokens = AsyncMock(return_value=new_tokens)
        mock_auth.validate_tokens = AsyncMock(return_value=True)
        mock_auth.close = AsyncMock()

        with (
            patch(
                "shared.auth.scheduler._get_authenticator_for_platform",
                return_value=mock_auth,
            ),
            patch(
                "shared.auth.scheduler._build_tokens_from_config",
                return_value=_make_tokens(platform, expires_offset=6 * 86400),
            ),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await check_and_renew_tokens(
                platform, config, config_path="config/config.toml"
            )

        assert result.action == "renewed"

        # 内存被更新（_update_last_refresh_at 与 update_auth_section 两次调用都会经过 mock）
        # 至少一次 update_auth_section 带 last_refresh_at
        last_refresh_calls = [
            c for c in mock_update.await_args_list
            if c.args and len(c.args) >= 2 and "last_refresh_at" in (c.args[1] or {})
        ]
        assert last_refresh_calls, (
            f"{platform}: 期望至少一次 update_auth_section 带 last_refresh_at"
        )

    async def test_xhs_renew_updates_last_refresh_at(self) -> None:
        config = Config()
        config.xiaohongshu.auth.cookie = "fake=1"
        config.xiaohongshu.auth.expires_at = time.time() + 30 * 86400
        await self._run_renew_success("xhs", config)
        assert config.xiaohongshu.auth.last_refresh_at > 0

    async def test_weibo_renew_updates_last_refresh_at(self) -> None:
        config = Config()
        config.weibo.auth.cookie = "fake=1"
        config.weibo.auth.expires_at = time.time() + 30 * 86400
        await self._run_renew_success("weibo", config)
        assert config.weibo.auth.last_refresh_at > 0

    async def test_bilibili_renew_updates_last_refresh_at(self) -> None:
        """回归：bilibili 续期后仍写回 last_refresh_at。"""
        config = Config()
        config.bilibili.auth.sessdata = "fake"
        config.bilibili.auth.expires_at = time.time() + 30 * 86400
        await self._run_renew_success("bilibili", config)
        assert config.bilibili.auth.last_refresh_at > 0
