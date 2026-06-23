"""Tests for shared.auth.scheduler — validate_tokens gate on renewal.

Covers Bug 1: XHS/Weibo refresh_tokens unconditionally set obtained_at=now
even when no real refresh happened; scheduler must validate before writing
config.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from shared.auth.base import PlatformTokens
from shared.auth.scheduler import check_and_renew_tokens
from shared.config import Config


def _make_config(platform: str) -> Config:
    """Build a minimal Config with a near-expiry token for the platform."""
    config = Config()
    now = time.time()
    if platform == "xhs":
        config.xiaohongshu.auth.cookie = "a1=x; webId=y"
        config.xiaohongshu.auth.expires_at = now + 2 * 86400  # within force_before
        config.xiaohongshu.enabled = True
    elif platform == "weibo":
        config.weibo.auth.cookie = "SUB=abc"
        config.weibo.auth.expires_at = now + 2 * 86400
        config.weibo.enabled = True
    elif platform == "bilibili":
        config.bilibili.auth.sessdata = "s"
        config.bilibili.auth.bili_jct = "j"
        config.bilibili.auth.expires_at = now + 2 * 86400
    return config


@pytest.mark.asyncio
async def test_xhs_renewal_validated_success_does_write(tmp_path):
    """When validate_tokens returns True after refresh, config is written."""
    config = _make_config("xhs")
    config_path = str(tmp_path / "config.toml")

    # fake_new obtained_at 必须严格大于 _build_tokens_from_config 返回的 obtained_at
    # (后者默认 time.time())，用稍后的时间戳保证 >
    past = time.time()
    fake_new = PlatformTokens(
        platform="xhs",
        cookies={"a1": "x_new", "webId": "y_new"},
        obtained_at=past + 60,
        expires_at=past + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(return_value=True)

        result = await check_and_renew_tokens("xhs", config, config_path)

    assert result.action == "renewed"
    mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_xhs_renewal_validation_failure_skips_write(tmp_path):
    """Bug 1 regression: refresh returns tokens but validate_tokens=False →
    do NOT write config, return action='expired'."""
    config = _make_config("xhs")
    config_path = str(tmp_path / "config.toml")

    # XhsAuthenticator.refresh_tokens sets obtained_at=now unconditionally;
    # simulate that the server returned no new cookies → validate fails.
    past = time.time()
    fake_new = PlatformTokens(
        platform="xhs",
        cookies={"a1": "x"},  # unchanged
        obtained_at=past + 60,  # bumped (the bug) — must be > original
        expires_at=past + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(return_value=False)

        result = await check_and_renew_tokens("xhs", config, config_path)

    assert result.action == "expired"
    mock_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_weibo_renewal_validation_failure_skips_write(tmp_path):
    """Same regression coverage for weibo."""
    config = _make_config("weibo")
    config_path = str(tmp_path / "config.toml")

    past = time.time()
    fake_new = PlatformTokens(
        platform="weibo",
        cookies={"SUB": "abc"},
        obtained_at=past + 60,
        expires_at=past + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(return_value=False)

        result = await check_and_renew_tokens("weibo", config, config_path)

    assert result.action == "expired"
    mock_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_tokens_exception_treated_as_failure(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    """If validate_tokens raises, treat as validation failure (skip write,
    return 'expired') rather than crashing the whole check. The warning
    must be logged so operators can see the failure in production logs."""
    config = _make_config("xhs")
    config_path = str(tmp_path / "config.toml")

    past = time.time()
    fake_new = PlatformTokens(
        platform="xhs",
        cookies={"a1": "x"},
        obtained_at=past + 60,
        expires_at=past + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(side_effect=RuntimeError("network down"))

        with caplog.at_level("WARNING", logger="shared.auth.scheduler"):
            result = await check_and_renew_tokens("xhs", config, config_path)

    assert result.action == "expired"
    mock_write.assert_not_awaited()
    # MINOR-9: warning must be recorded so the failure is visible in logs
    assert any("校验异常" in r.message or "校验失败" in r.message for r in caplog.records)
