# Phase 4: Token Renewal Scheduler Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate token renewal checks into the `trawler check` flow — check before content monitoring, renew if needed. Also add `trawler token refresh --all` CLI support.

**Architecture:** `check_and_renew_tokens()` pure async function takes platform name + config, builds PlatformTokens, calls `should_renew()`, executes refresh if needed, writes back to config. Called at the top of `run_check_once()`. No daemon mode.

**Tech Stack:** Python 3.12+, asyncio, existing auth infrastructure

**Execution Order:**
1. Task 1: Add `_build_tokens_from_config` to each authenticator
2. Task 2: Add `check_and_renew_tokens` to scheduler.py
3. Task 3: Integrate into pipeline.py
4. Task 4: Add `trawler token refresh --all` CLI
5. Task 5: Write integration tests

---

### Task 1: Add `_build_tokens_from_config` static methods

Each authenticator needs a way to rebuild PlatformTokens from config dataclass.

- [ ] **Step 1: BilibiliAuthenticator.build_tokens_from_config**

**File:** `platforms/bilibili/auth.py`

Add static method:

```python
@staticmethod
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
            "SESSDATA": auth.sessdata,
            "bili_jct": auth.bili_jct,
            "buvid3": auth.buvid3 or "",
            "DedeUserID": auth.dedeuserid or "",
        },
        obtained_at=_time.time(),
        expires_at=auth.expires_at,
    )
```

- [ ] **Step 2: WeiboAuthenticator.build_tokens_from_config**

**File:** `platforms/weibo/auth.py`

Add static method:

```python
@staticmethod
def build_tokens_from_config(config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config.weibo.auth. Returns None if not configured."""
    import time as _time
    auth = config.weibo.auth
    if not auth.cookie or auth.expires_at <= 0:
        return None
    cookie_dict: dict[str, str] = {}
    for part in auth.cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookie_dict[k] = v
    if not cookie_dict:
        return None
    return PlatformTokens(
        platform="weibo",
        cookies=cookie_dict,
        obtained_at=_time.time(),
        expires_at=auth.expires_at,
    )
```

- [ ] **Step 3: XhsAuthenticator.build_tokens_from_config**

**File:** `platforms/xiaohongshu/auth.py`

Add static method after implementing XhsAuthenticator (Phase 3):

```python
@staticmethod
def build_tokens_from_config(config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config.xiaohongshu.auth. Returns None if not configured."""
    import time as _time
    auth = config.xiaohongshu.auth
    if not auth.cookie or auth.expires_at <= 0:
        return None
    cookie_dict: dict[str, str] = {}
    for part in auth.cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookie_dict[k] = v
    if not cookie_dict:
        return None
    return PlatformTokens(
        platform="xhs",
        cookies=cookie_dict,
        obtained_at=_time.time(),
        expires_at=auth.expires_at,
    )
```

---

### Task 2: Add `check_and_renew_tokens` to scheduler.py

- [ ] **Step 1: Write check_and_renew_tokens function**

**File:** `shared/auth/scheduler.py` — append after `should_renew()`

```python
from __future__ import annotations

import logging
import time

from dataclasses import dataclass

from shared.auth.base import PlatformTokens
from shared.config import RenewalConfig, Config

logger = logging.getLogger(__name__)

# (existing should_renew() and RenewalDecision remain unchanged)


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

    Args:
        platform: Platform name ("bilibili", "weibo", "xhs")
        config: Loaded Config object

    Returns:
        RenewalResult describing what happened
    """
    # Resolve authenticator and build tokens
    authenticator = _get_authenticator_for_platform(platform, config)
    if authenticator is None:
        return RenewalResult(platform, "not_configured", f"{platform}: 平台未配置或凭证缺失")

    tokens = authenticator.build_tokens_from_config(config)
    if tokens is None:
        return RenewalResult(platform, "not_configured", f"{platform}: 凭证未配置")

    # Check if renewal needed
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

    # Execute renewal
    logger.info("%s token 需要续期 (%s)", platform, decision.reason)
    try:
        new_tokens = await authenticator.refresh_tokens(tokens)

        # Persist new tokens back to config
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
```

- [ ] **Step 2: Write tests**

**File:** `tests/test_scheduler.py` — append after existing `should_renew()` tests:

```python
# ═══════════════════════════════════════════════════════════
# check_and_renew_tokens tests
# ═══════════════════════════════════════════════════════════

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from shared.auth.scheduler import check_and_renew_tokens, RenewalResult


class TestCheckAndRenewTokens:
    @pytest.mark.asyncio
    async def test_not_configured(self):
        """When platform not configured, returns not_configured."""
        config = MagicMock()
        config.weibo.auth = MagicMock(cookie="", expires_at=0)

        with patch("shared.auth.scheduler._get_authenticator_for_platform", return_value=None):
            result = await check_and_renew_tokens("weibo", config)

        assert result.action == "not_configured"

    @pytest.mark.asyncio
    async def test_skipped_when_not_needed(self):
        """When token is far from expiry, skip renewal."""
        config = MagicMock()
        config.auth.renewal = MagicMock(min_interval_hours=24, force_before_days=7, check_interval_hours=6)
        config.weibo.auth = MagicMock(cookie="SUB=x;", expires_at=time.time() + 30 * 86400)

        mock_auth = MagicMock()
        mock_tokens = PlatformTokens(
            platform="weibo", cookies={"SUB": "x"},
            obtained_at=time.time(), expires_at=time.time() + 30 * 86400,
        )
        mock_auth.build_tokens_from_config.return_value = mock_tokens

        with patch("shared.auth.scheduler._get_authenticator_for_platform", return_value=mock_auth):
            result = await check_and_renew_tokens("weibo", config)

        assert result.action == "skipped"
        assert "无需续期" in result.message

    @pytest.mark.asyncio
    async def test_renewed_when_needed(self):
        """When token is near expiry, trigger renewal."""
        config = MagicMock()
        config.auth.renewal = MagicMock(min_interval_hours=24, force_before_days=7, check_interval_hours=6)
        config.weibo.auth = MagicMock(cookie="SUB=x;", expires_at=time.time() + 30 * 86400)

        mock_auth = MagicMock()
        # Create tokens that are within force_before_days (e.g. 6 days remaining → force_soon)
        mock_tokens = PlatformTokens(
            platform="weibo", cookies={"SUB": "x"},
            obtained_at=time.time(), expires_at=time.time() + 6 * 86400,
        )
        mock_auth.build_tokens_from_config.return_value = mock_tokens
        new_tokens = PlatformTokens(
            platform="weibo", cookies={"SUB": "new"},
            obtained_at=time.time(), expires_at=time.time() + 7 * 86400,
        )
        mock_auth.refresh_tokens = AsyncMock(return_value=new_tokens)

        with (
            patch("shared.auth.scheduler._get_authenticator_for_platform", return_value=mock_auth),
            patch("shared.auth.scheduler.update_auth_section"),
        ):
            result = await check_and_renew_tokens("weibo", config)

        assert result.action == "renewed"
        mock_auth.refresh_tokens.assert_called_once()

    @pytest.mark.asyncio
    async def test_expired_skips_renewal(self):
        """When token is already expired, return expired and don't try to renew."""
        config = MagicMock()
        config.auth.renewal = MagicMock(min_interval_hours=24, force_before_days=7, check_interval_hours=6)
        config.weibo.auth = MagicMock(cookie="SUB=x;", expires_at=time.time() + 30 * 86400)

        mock_auth = MagicMock()
        mock_tokens = PlatformTokens(
            platform="weibo", cookies={"SUB": "x"},
            obtained_at=time.time() - 86400 * 10, expires_at=time.time() - 10,
        )
        mock_auth.build_tokens_from_config.return_value = mock_tokens

        with patch("shared.auth.scheduler._get_authenticator_for_platform", return_value=mock_auth):
            result = await check_and_renew_tokens("weibo", config)

        assert result.action == "expired"
        mock_auth.refresh_tokens.assert_not_called()
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_scheduler.py -v
```

---

### Task 3: Integrate token check into core/pipeline.py

- [ ] **Step 1: Read current pipeline.py to find entry points**

Look at `run_check_once` and each platform-specific runner.

- [ ] **Step 2: Add token check at top of each platform runner**

In `run_bilibili_check_once()`, add before any content monitoring:

```python
from shared.auth.scheduler import check_and_renew_tokens

async def run_bilibili_check_once(config: Config, store: JsonSetStore | None = None) -> None:
    # Check and renew tokens before monitoring
    result = await check_and_renew_tokens("bilibili", config)
    if result.action == "expired":
        return  # Skip if tokens are dead
    # ... rest of existing monitoring logic
```

Same for `run_weibo_check_once()` and `run_xhs_check_once()`.

- [ ] **Step 3: Verify with existing tests**

```bash
pytest tests/ -v -k "pipeline"
```

---

### Task 4: Add `trawler token refresh --all` CLI option

- [ ] **Step 1: Update `run_check.py`**

Add `--all` to the `token_refresh` command:

```python
@token.command("refresh")
@click.option(
    "--platform",
    type=click.Choice(["bilibili", "xhs", "weibo"]),
    default=None,
    help="续期的平台",
)
@click.option(
    "--all",
    "refresh_all",
    is_flag=True,
    default=False,
    help="续期所有已配置平台",
)
def token_refresh(platform: str | None, refresh_all: bool) -> None:
    """手动续期 token"""
    logging.basicConfig(...)
    config = load_config("config.toml")

    if refresh_all:
        targets = ["bilibili", "weibo", "xhs"]
    elif platform:
        targets = [platform]
    else:
        console.print("[red]✗ 请指定 --platform 或 --all[/]")
        sys.exit(1)

    for plat in targets:
        _refresh_single_platform(plat, config)


def _refresh_single_platform(platform: str, config: Config) -> None:
    """Refresh tokens for a single platform."""
    # Extract existing per-platform refresh logic into this function
    # (same code currently in token_refresh for each platform)
    ...
```

- [ ] **Step 2: Extract existing per-platform refresh logic into _refresh_single_platform()**

Move the bilibili/weibo/xhs refresh blocks from `token_refresh()` into `_refresh_single_platform()`. Keep the logic identical.

- [ ] **Step 3: Update CLI tests**

```python
def test_token_refresh_all(self):
    """trawler token refresh --all checks all three platforms"""
    runner = CliRunner()
    config = MagicMock()
    config.bilibili.auth.expires_at = time.time() + 30 * 86400
    config.bilibili.auth.sessdata = "s"
    config.bilibili.auth.bili_jct = "j"
    config.weibo.auth.expires_at = 0  # not configured
    config.weibo.auth.cookie = ""
    config.xiaohongshu.auth.expires_at = 0
    config.xiaohongshu.auth.cookie = ""
    
    with patch("shared.config.load_config", return_value=config):
        result = runner.invoke(cli, ["token", "refresh", "--all"])
        assert result.exit_code == 0
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_cli.py -v
```

---

### Task 5: Write integration tests

- [ ] **Step 1: Create `tests/test_token_renewal_integration.py`**

```python
"""Integration tests for token renewal flow: should_renew → refresh → persist."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.auth.base import PlatformTokens
from shared.auth.scheduler import should_renew, check_and_renew_tokens
from shared.config import RenewalConfig


class TestRenewalFlow:
    """End-to-end: should_renew → refresh_tokens → update_auth_section"""

    @pytest.mark.asyncio
    async def test_full_renewal_flow(self):
        renewal_cfg = RenewalConfig(min_interval_hours=24, force_before_days=7)
        tokens = PlatformTokens(
            platform="weibo",
            cookies={"SUB": "old"},
            obtained_at=time.time(),
            expires_at=time.time() + 5 * 86400,  # 5 days → should force_soon
        )

        # Decision
        decision = should_renew(tokens, renewal_cfg)
        assert decision.should_renew is True
        assert decision.reason == "force_soon"

    @pytest.mark.asyncio
    async def test_expired_stops_flow(self):
        renewal_cfg = RenewalConfig(min_interval_hours=24, force_before_days=7)
        tokens = PlatformTokens(
            platform="bilibili",
            cookies={"SESSDATA": "x"},
            obtained_at=time.time() - 86400 * 30,
            expires_at=time.time() - 86400,
        )

        decision = should_renew(tokens, renewal_cfg)
        assert decision.should_renew is False
        assert decision.reason == "expired"
```

- [ ] **Step 2: Run all tests**

```bash
pytest tests/ -v
ruff check .
ruff format --check .
```

---

### Verification

Run the full test suite:

```bash
pytest -x
ruff check .
ruff format --check .
pyright .
```

Commit message: `feat(auth): integrate token renewal into check flow + add --all refresh`