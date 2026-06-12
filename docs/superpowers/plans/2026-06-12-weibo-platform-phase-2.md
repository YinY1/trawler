# Phase 2: Weibo (微博) Platform Support

**Date**: 2026-06-12
**Author**: Explorer
**Status**: Draft

## Overview

Add full Weibo platform support to trawler: QR login, content monitoring, pipeline integration, and notification. This phase follows the established platform pattern used by bilibili and xiaohongshu.

### Architecture

```
shared/
  protocols.py         # + WeiboPost, WeiboCommentHighlight, WeiboDownloadResult
  constants.py         # + WEIBO_REQUEST_TIMEOUT, WEIBO_POLL_INTERVAL
  auth/
    __init__.py        # + "weibo" in get_authenticator()

platforms/weibo/
  __init__.py          # docstring only
  auth.py              # WeiboAuthenticator (QR login + keepalive renewal)
  api.py               # Weibo HTTP API wrapper
  monitor.py           # check_new_weibo_posts()
  comments.py          # fetch_weibo_comment_highlights()
  downloader.py        # download_weibo_media()
  parser.py            # parse_weibo_post()

core/
  pipeline.py          # + run_weibo_check_once(), process_weibo_post()
  notifier.py          # + notify_new_weibo_post()

run_check.py           # enable weibo in login/token refresh

tests/
  test_weibo_authenticator.py
  test_weibo_api.py
  test_weibo_monitor.py
  test_weibo_comments.py
  test_weibo_downloader.py
  test_weibo_parser.py
```

---

## Task 1: Add Weibo data models to `shared/protocols.py`

**File**: `shared/protocols.py`

Add Weibo-specific dataclasses after the existing Xiaohongshu section (after line 96).

```python
# ═══════════════════════════════════════════════════════════
# 微博数据模型
# ═══════════════════════════════════════════════════════════


@dataclass
class WeiboPost:
    """微博帖子元信息"""

    post_id: str
    text: str
    clean_text: str
    author: str
    user_id: str
    pubdate: int  # Unix 时间戳
    image_urls: list[str] = field(default_factory=list)
    reposts_count: int = 0
    comments_count: int = 0
    likes_count: int = 0
    is_original: bool = True
    reposted_post: Optional[WeiboPost] = None  # 转发时可嵌套


@dataclass
class WeiboCommentHighlight:
    """微博评论亮点"""

    content: str
    user_name: str
    is_author: bool
    like_count: int


@dataclass
class WeiboDownloadResult:
    """微博帖子下载结果"""

    success: bool
    source_id: str  # post_id
    title: str
    text: str = ""
    image_paths: list[Path] = field(default_factory=list)
    error: Optional[str] = None
```

**Why this is correct**:
- `WeiboPost.reposted_post` is `Optional[WeiboPost]` — recursive dataclass, Python 3.12 allows forward references at runtime via `from __future__ import annotations`
- Fields match the two APIs: mobile API `m.weibo.cn` returns `id`, `text`, `user.screen_name`, `user.id`, `created_at`, `pics`, `reposts_count`, `comments_count`, `attitudes_count`, `is_original`, `retweeted_status`
- Uses `field(default_factory=list)` for mutable defaults
- Follows the exact pattern of `VideoInfo` / `NoteInfo` / `CommentHighlight` / `XhsCommentHighlight` / `XhsDownloadResult`

**Test command**: `pytest -x tests/ -k "weibo"` (no tests yet, but verify lint passes: `ruff check shared/protocols.py`)

---

## Task 2: Add Weibo constants to `shared/constants.py`

**File**: `shared/constants.py`

Add after line 10:

```python
WEIBO_REQUEST_TIMEOUT = 15     # 微博 API 请求超时
WEIBO_DOWNLOAD_TIMEOUT = 120   # 微博文件下载超时
WEIBO_POLL_INTERVAL = 2        # 二维码轮询间隔（秒）
WEIBO_POLL_TIMEOUT = 240       # 二维码轮询超时（秒）
WEIBO_KEEPALIVE_INTERVAL = 6   # Cookie keepalive 间隔（小时）
```

---

## Task 3: Create `platforms/weibo/__init__.py`

**File**: `platforms/weibo/__init__.py`

New file, one-line docstring only:

```python
"""微博平台适配层"""
```

---

## Task 4: Write tests for WeiboAuthenticator before implementation

**File**: `tests/test_weibo_authenticator.py`

```python
"""Tests for WeiboAuthenticator — fully mocked, no real Weibo API calls."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.auth import WeiboAuthenticator
from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)


# ── Fixtures ──────────────────────────────────────────────────


def _sample_tokens(**cookie_overrides: str) -> PlatformTokens:
    now = time.time()
    cookies = {
        "SUB": "fake_sub",
        "SUBP": "fake_subp",
        "WBPSESS": "fake_wbpsess",
        "SSOLoginState": "1234567890",
    }
    cookies.update(cookie_overrides)
    return PlatformTokens(
        platform="weibo",
        cookies=cookies,
        obtained_at=now,
        expires_at=now + 7 * 86400,  # 7 days
    )


# ── WeiboAuthenticator.generate_qr_code ────────────────────


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"qrid": "qr_abc123", "image": "data:image/png;base64,..."}}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_key == "qr_abc123"
        assert result.expires_in == 240
        # qr_url should contain the image URL for QR display
        assert "passport.weibo.com" in result.qr_url or "qrcode" in result.qr_url


class TestGenerateQrCode_ApiError:
    @pytest.mark.asyncio
    async def test_raises_on_bad_status(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            pytest.raises(RuntimeError, match="生成二维码失败"),
        ):
            await auth.generate_qr_code()

    @pytest.mark.asyncio
    async def test_raises_on_missing_qrid(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {}}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            pytest.raises(RuntimeError, match="未获取到 qrid"),
        ):
            await auth.generate_qr_code()


# ── WeiboAuthenticator.poll_qr_status ──────────────────────


class TestPollQrStatus:
    @pytest.mark.asyncio
    async def test_waiting(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 0}}  # 0 = waiting

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_scanned(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 1, "nickname": "测试用户"}}  # 1 = scanned

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SCANNED
        assert not status.success

    @pytest.mark.asyncio
    async def test_confirmed(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 2}}  # 2 = confirmed (on phone)

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.CONFIRMED
        assert not status.success

    @pytest.mark.asyncio
    async def test_success(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 3}}  # 3 = success (redirect with cookies)

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SUCCESS
        assert status.success

    @pytest.mark.asyncio
    async def test_expired(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 4}}  # 4 = expired

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.EXPIRED
        assert not status.success

    @pytest.mark.asyncio
    async def test_bad_status_returns_waiting(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 999}}  # Unknown status

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success


# ── WeiboAuthenticator.get_tokens ──────────────────────────


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {
            "Set-Cookie": (
                "SUB=fake_sub; Path=/; Domain=.weibo.com; HttpOnly, "
                "SUBP=fake_subp; Path=/; Domain=.weibo.com, "
                "WBPSESS=fake_wbpsess; Path=/; Domain=.weibo.com, "
                "SSOLoginState=1735689600; Path=/; Domain=.weibo.com"
            )
        }

        async def json_side() -> dict:
            return {"data": {"status": 3, "nickname": "测试用户"}}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            tokens = await auth.get_tokens("qr_abc")

        assert tokens.platform == "weibo"
        assert tokens.cookies["SUB"] == "fake_sub"
        assert tokens.cookies["SUBP"] == "fake_subp"
        assert tokens.cookies["WBPSESS"] == "fake_wbpsess"
        assert tokens.cookies["SSOLoginState"] == "1735689600"
        assert tokens.expires_at > time.time()

    @pytest.mark.asyncio
    async def test_raises_when_not_success(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"data": {"status": 2}}  # confirmed, not yet success

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            pytest.raises(RefreshFailedError, match="二维码未确认"),
        ):
            await auth.get_tokens("qr_abc")

    @pytest.mark.asyncio
    async def test_raises_on_missing_set_cookie(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {}  # No Set-Cookie

        async def json_side() -> dict:
            return {"data": {"status": 3}}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            pytest.raises(RefreshFailedError, match="未获取到 Cookie"),
        ):
            await auth.get_tokens("qr_abc")


# ── WeiboAuthenticator.refresh_tokens ──────────────────────


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_keepalive_updates_expiry(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {
            "Set-Cookie": (
                "SUB=new_sub; Path=/; Domain=.weibo.com, "
                "SUBP=new_subp; Path=/; Domain=.weibo.com"
            )
        }
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            result = await auth.refresh_tokens(tokens)

        # New cookies should be updated
        assert result.cookies["SUB"] == "new_sub"
        assert result.cookies["SUBP"] == "new_subp"
        # Unchanged cookies preserved
        assert result.cookies["WBPSESS"] == "fake_wbpsess"
        # Expiry extended
        assert result.expires_at > tokens.expires_at

    @pytest.mark.asyncio
    async def test_keepalive_failure_returns_original_tokens(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            result = await auth.refresh_tokens(tokens)

        assert result is tokens  # Same object, no change


# ── WeiboAuthenticator.validate_tokens ─────────────────────


class TestValidateTokens:
    @pytest.mark.asyncio
    async def test_expired_tokens_return_false(self):
        auth = WeiboAuthenticator()
        tokens = PlatformTokens(
            platform="weibo",
            cookies={"SUB": "x"},
            obtained_at=time.time() - 10 * 86400,
            expires_at=time.time() - 10,  # expired
        )
        assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_valid_tokens_return_true(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_invalid_tokens_return_false(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_resp = MagicMock()
        mock_resp.status = 302  # Redirect = not logged in
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=Exception("connection error"))

        with patch("shared.http.get_session", return_value=mock_session):
            assert await auth.validate_tokens(tokens) is False


# ── WeiboAuthenticator.supports_refresh ────────────────────


class TestSupportsRefresh:
    def test_returns_true(self):
        auth = WeiboAuthenticator()
        assert auth.supports_refresh() is True


# ── WeiboAuthenticator is a BaseAuthenticator ──────────────


class TestIsAuthenticator:
    def test_is_subclass(self):
        assert issubclass(WeiboAuthenticator, BaseAuthenticator)
```

**Test command**: `pytest -x tests/test_weibo_authenticator.py` (will fail initially — expected)

---

## Task 5: Implement `platforms/weibo/auth.py`

**File**: `platforms/weibo/auth.py`

```python
"""微博认证管理 - QR 登录 + Cookie Keepalive 续期"""

from __future__ import annotations

import http.cookies
import logging
import time
from urllib.parse import parse_qs, urlparse

import aiohttp

from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)
from shared.constants import WEIBO_POLL_INTERVAL, WEIBO_POLL_TIMEOUT, WEIBO_REQUEST_TIMEOUT
from shared.http import get_session

logger = logging.getLogger(__name__)

# 微博 QR 登录 API
QR_IMAGE_URL = "https://passport.weibo.com/sso/v2/qrcode/image?entry=miniblog&size=180"
QR_CHECK_URL = "https://passport.weibo.com/sso/v2/qrcode/check?entry=miniblog&qrid={qrid}"

# Cookie keepalive — 访问微博首页
KEEPALIVE_URL = "https://weibo.com"

# 微博 QR 状态码映射
_QR_STATUS_MAP: dict[int, QRStatus] = {
    0: QRStatus.WAITING,    # 未扫码
    1: QRStatus.SCANNED,    # 已扫码
    2: QRStatus.CONFIRMED,  # 已确认（手机端）
    3: QRStatus.SUCCESS,    # 登录成功
    4: QRStatus.EXPIRED,    # 已过期
}


def _parse_weibo_cookies(set_cookie_header: str) -> dict[str, str]:
    """从 Set-Cookie 响应头解析微博 Cookie 键值对，使用 http.cookies.SimpleCookie。

    Args:
        set_cookie_header: 完整的 Set-Cookie 字符串（可能包含多个 Set-Cookie 条目，
                           以逗号分隔）

    Returns:
        Cookie 键值对字典
    """
    cookies: dict[str, str] = {}
    if not set_cookie_header:
        return cookies

    # SimpleCookie 可以正确解析 Set-Cookie 格式（包括 Path=, Domain= 等属性）
    try:
        sc = http.cookies.SimpleCookie(set_cookie_header)
        for key, morsel in sc.items():
            cookies[key] = morsel.value
    except http.cookies.CookieError:
        # 降级：手动解析逗号分隔的 cookie 片段
        for part in set_cookie_header.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            if part.startswith("Path=") or part.startswith("Domain="):
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.split(";")[0].strip()
            if key and value:
                cookies[key] = value
    return cookies


def _get_user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )


class WeiboAuthenticator(BaseAuthenticator):
    """微博 QR 扫码登录 + Cookie Keepalive 续期"""

    # ── BaseAuthenticator 接口 ────────────────────────────

    async def generate_qr_code(self) -> QRCodeResult:
        session = await get_session()
        async with session.get(
            QR_IMAGE_URL,
            headers={"User-Agent": _get_user_agent()},
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"生成二维码失败，状态码: {resp.status}")
            data = await resp.json(content_type=None)

        qrid = data.get("data", {}).get("qrid", "")
        if not qrid:
            raise RuntimeError("生成二维码失败：未获取到 qrid")

        # 构造可直接用于 QR 显示的 URL
        qr_url = f"https://passport.weibo.com/sso/v2/qrcode/image?entry=miniblog&size=180"
        return QRCodeResult(qr_url=qr_url, qr_key=qrid, expires_in=WEIBO_POLL_TIMEOUT)

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        session = await get_session()
        url = QR_CHECK_URL.format(qrid=qr_key)
        async with session.get(
            url,
            headers={"User-Agent": _get_user_agent()},
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("轮询二维码状态失败，状态码: %s", resp.status)
                return AuthStatus(success=False, status=QRStatus.WAITING, message="请求失败")
            data = await resp.json(content_type=None)

        status_code = data.get("data", {}).get("status", 0)
        status = _QR_STATUS_MAP.get(status_code, QRStatus.WAITING)
        nickname = data.get("data", {}).get("nickname", "")

        msg_map: dict[QRStatus, str] = {
            QRStatus.WAITING: "等待扫码",
            QRStatus.SCANNED: f"已扫码 ({nickname})，等待确认" if nickname else "已扫码，等待确认",
            QRStatus.CONFIRMED: "已确认，即将登录",
            QRStatus.SUCCESS: "登录成功",
            QRStatus.EXPIRED: "二维码已过期",
        }
        return AuthStatus(
            success=status == QRStatus.SUCCESS,
            status=status,
            message=msg_map.get(status, "未知状态"),
        )

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        session = await get_session()
        url = QR_CHECK_URL.format(qrid=qr_key)
        async with session.get(
            url,
            headers={"User-Agent": _get_user_agent()},
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            data = await resp.json(content_type=None)
            status_code = data.get("data", {}).get("status", 0)

        if status_code != 3:
            raise RefreshFailedError("二维码未确认，无法获取 token")

        # 从 Set-Cookie 头提取 cookies（需要重新请求）
        async with session.get(
            url,
            headers={"User-Agent": _get_user_agent()},
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp2:
            set_cookie = resp2.headers.get("Set-Cookie", "")

        if not set_cookie:
            raise RefreshFailedError("未获取到 Set-Cookie 响应头")

        cookies = _parse_weibo_cookies(set_cookie)
        if "SUB" not in cookies:
            raise RefreshFailedError("未获取到 SUB Cookie，登录可能失败")

        now = time.time()
        return PlatformTokens(
            platform="weibo",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 7 * 86400,  # 微博 Cookie 约 7 天
        )

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        """通过访问微博首页来保持 Cookie 活跃（keepalive）。

        访问 weibo.com，如果服务端返回新的 Set-Cookie，则更新 tokens。
        否则保持原有 tokens 不变。
        """
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        session = await get_session()
        try:
            async with session.get(
                KEEPALIVE_URL,
                headers={
                    "User-Agent": _get_user_agent(),
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                set_cookie = resp.headers.get("Set-Cookie", "")

            if set_cookie:
                new_cookies = _parse_weibo_cookies(set_cookie)
                # 仅更新实际有值的字段
                updated_cookies = dict(tokens.cookies)
                updated_cookies.update(new_cookies)
                now = time.time()
                return PlatformTokens(
                    platform="weibo",
                    cookies=updated_cookies,
                    obtained_at=now,
                    expires_at=now + 7 * 86400,
                )

            # 没有新 cookie，返回原有 tokens
            return tokens
        except Exception as e:
            logger.warning("Keepalive 请求失败: %s", e)
            return tokens

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        if tokens.expires_at < time.time():
            return False
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        session = await get_session()
        try:
            async with session.get(
                KEEPALIVE_URL,
                headers={
                    "User-Agent": _get_user_agent(),
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def supports_refresh(self) -> bool:
        return True
```

**Key design decisions**:
- QR image URL is used directly as the QR display URL (the `/image` endpoint returns a QR code PNG)
- Poll check URL expects `qrid` parameter; status codes are: 0=waiting, 1=scanned, 2=confirmed, 3=success, 4=expired
- `get_tokens()` makes the poll request twice: first to verify success, second to capture `Set-Cookie` (the first response may not have the final cookies)
- `refresh_tokens()` implements keepalive by visiting weibo.com and capturing refreshed cookies
- Cookie expiry defaults to 7 days, matching Weibo's typical cookie lifetime
- `allow_redirects=False` on validation to avoid following redirects when not logged in

**Test command**: `pytest -x tests/test_weibo_authenticator.py`

---

## Task 6: Add "weibo" to `shared/auth/__init__.py` factory

**File**: `shared/auth/__init__.py`

Edit the `get_authenticator()` function (line 35-40):

```python
def get_authenticator(platform: str) -> BaseAuthenticator:
    """Factory: get platform authenticator instance."""
    if platform == "bili":
        from platforms.bilibili.auth import BilibiliAuthenticator
        return BilibiliAuthenticator()
    if platform == "weibo":
        from platforms.weibo.auth import WeiboAuthenticator
        return WeiboAuthenticator()
    raise ValueError(f"Unsupported platform: {platform}")
```

**Test command**: Add test to `tests/test_auth_base.py`:

Add to `TestPackageExports.test_all_list_matches_actual_exports` — no change needed (the list matches `__all__`, and we're not changing exports).

Actually, let's write a quick inline test:

```python
# In test_auth_base.py, add to TestPackageExports:
def test_get_authenticator_weibo(self) -> None:
    from shared.auth import get_authenticator
    from platforms.weibo.auth import WeiboAuthenticator
    auth = get_authenticator("weibo")
    assert isinstance(auth, WeiboAuthenticator)
```

**Test command**: `pytest -x tests/test_auth_base.py::TestPackageExports::test_get_authenticator_weibo`

---

## Task 7: Write tests for Weibo API module

**File**: `tests/test_weibo_api.py`

```python
"""Tests for platforms/weibo/api.py — Weibo HTTP API wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.api import (
    WeiboApiClient,
    fetch_user_posts_mobile,
    fetch_user_posts_pc,
    _parse_mobile_post,
    _parse_pc_post,
)


# ── Cookie helper ─────────────────────────────────────────


def _make_cookie_str() -> str:
    return "SUB=fake_sub; SUBP=fake_subp; WBPSESS=fake_wbpsess; SSOLoginState=123"


# ── WeiboApiClient ────────────────────────────────────────


class TestWeiboApiClient:
    def test_init_stores_cookies(self):
        client = WeiboApiClient("SUB=abc")
        assert client._cookie == "SUB=abc"
        assert "User-Agent" in client._headers

    def test_headers_contain_cookie(self):
        client = WeiboApiClient("SUB=abc")
        assert client._headers["Cookie"] == "SUB=abc"
        assert "weibo.com" in client._headers.get("Referer", "")


# ── fetch_user_posts_mobile ───────────────────────────────


class TestFetchUserPostsMobile:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "cards": [
                        {
                            "card_type": 9,
                            "mblog": {
                                "id": "post123",
                                "text": "测试微博内容",
                                "user": {"screen_name": "测试用户", "id": 12345},
                                "created_at": "Tue Jun 11 10:00:00 +0800 2026",
                                "pics": [{"url": "https://wx1.sinaimg.cn/large/001.jpg"}],
                                "reposts_count": 10,
                                "comments_count": 5,
                                "attitudes_count": 100,
                                "is_original": 0,
                            },
                        }
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert len(posts) == 1
        assert posts[0]["id"] == "post123"

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"ok": 0}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert posts == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert posts == []


# ── fetch_user_posts_pc ───────────────────────────────────


class TestFetchUserPostsPc:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "list": [
                        {
                            "id": 456789,
                            "idstr": "456789",
                            "text": "PC端微博内容",
                            "user": {"screen_name": "PC用户", "id": 67890},
                            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
                            "pic_ids": ["001", "002"],
                            "reposts_count": "20",
                            "comments_count": "15",
                            "attitudes_count": "200",
                            "is_original": 0,
                        }
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            posts = await fetch_user_posts_pc(cookie, user_id="67890")

        assert len(posts) == 1
        assert posts[0]["id"] == 456789

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"ok": 0}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("shared.http.get_session", return_value=mock_session):
            posts = await fetch_user_posts_pc(cookie, user_id="67890")

        assert posts == []


# ── _parse_mobile_post ────────────────────────────────────


class TestParseMobilePost:
    def test_parses_basic_post(self):
        raw = {
            "id": "post123",
            "text": "测试微博 <a href='/n/test'>@test</a> 内容",
            "user": {"screen_name": "测试用户", "id": 12345},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pics": [{"url": "https://wx1.sinaimg.cn/large/001.jpg"}],
            "reposts_count": 10,
            "comments_count": 5,
            "attitudes_count": 100,
            "is_original": 1,
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.post_id == "post123"
        assert "测试微博" in result.text
        assert "测试微博" in result.clean_text  # HTML tags stripped
        assert result.author == "测试用户"
        assert result.user_id == "12345"
        assert result.pubdate > 0
        assert len(result.image_urls) == 1
        assert result.reposts_count == 10
        assert result.comments_count == 5
        assert result.likes_count == 100
        assert result.is_original is True

    def test_handles_reposted_post(self):
        raw = {
            "id": "post456",
            "text": "转发微博",
            "user": {"screen_name": "转发者", "id": 999},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "reposts_count": 0,
            "comments_count": 0,
            "attitudes_count": 0,
            "is_original": 0,
            "retweeted_status": {
                "id": "original123",
                "text": "原始微博内容",
                "user": {"screen_name": "原作者", "id": 111},
                "created_at": "Mon Jun 10 08:00:00 +0800 2026",
                "reposts_count": 50,
                "comments_count": 20,
                "attitudes_count": 300,
                "is_original": 1,
            },
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.is_original is False
        assert result.reposted_post is not None
        assert result.reposted_post.post_id == "original123"
        assert result.reposted_post.author == "原作者"

    def test_returns_none_on_missing_id(self):
        raw = {"text": "no id post"}
        result = _parse_mobile_post(raw)
        assert result is None


# ── _parse_pc_post ────────────────────────────────────────


class TestParsePcPost:
    def test_parses_basic_post(self):
        raw = {
            "id": 456789,
            "idstr": "456789",
            "text": "PC端微博 <a href='/n/test'>@test</a>",
            "user": {"screen_name": "PC用户", "id": 67890},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pic_ids": ["001", "002"],
            "pic_infos": {
                "001": {"original": {"url": "https://wx1.sinaimg.cn/large/001.jpg"}},
                "002": {"original": {"url": "https://wx1.sinaimg.cn/large/002.jpg"}},
            },
            "reposts_count": "20",
            "comments_count": "15",
            "attitudes_count": "200",
            "is_original": 1,
        }
        result = _parse_pc_post(raw)
        assert result is not None
        assert result.post_id == "456789"
        assert "PC端微博" in result.text
        assert result.author == "PC用户"
        assert result.user_id == "67890"
        assert len(result.image_urls) == 2
        assert result.reposts_count == 20
        assert result.comments_count == 15
        assert result.likes_count == 200
        assert result.is_original is True

    def test_returns_none_on_missing_id(self):
        raw = {"text": "no id"}
        result = _parse_pc_post(raw)
        assert result is None
```

**Test command**: `pytest -x tests/test_weibo_api.py` (will fail initially)

---

## Task 8: Implement `platforms/weibo/api.py`

**File**: `platforms/weibo/api.py`

```python
"""微博 HTTP API 封装层

提供微博移动端和 PC 端 API 的请求和响应解析。
移动端: m.weibo.cn (不需要签名，请求简单)
PC 端: weibo.com/ajax (需要完整 Cookie，数据更丰富)
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

from shared.constants import WEIBO_REQUEST_TIMEOUT
from shared.http import get_session
from shared.protocols import WeiboPost

logger = logging.getLogger(__name__)

# 移动端 API
MOBILE_USER_POSTS_API = "https://m.weibo.cn/api/container/getIndex?type=uid&value={user_id}&containerid=107603{user_id}"

# PC 端 API
PC_USER_POSTS_API = "https://weibo.com/ajax/statuses/mymblog?uid={user_id}&page=1&feature=0"

# 微博图片 CDN 模板
SINAIMG_URL_TEMPLATE = "https://wx1.sinaimg.cn/large/{pic_id}.jpg"

# 默认 User-Agent
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# 微博时间格式：Tue Jun 11 10:00:00 +0800 2026
_WEIBO_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"

# HTML 清理正则
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")

# 中文时间格式（PC 端可能返回）：刚刚 / N分钟前 / 今天 HH:MM / 月-日 HH:MM / 年-月-日 HH:MM
_CHINESE_TIME_RE = re.compile(
    r"(?:刚刚)|"
    r"(?:(\d+)分钟前)|"
    r"(?:今天\s+(\d+):(\d+))|"
    r"(?:(\d+)-(\d+)\s+(\d+):(\d+))|"
    r"(?:(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d+):(\d+))"
)


class WeiboApiClient:
    """微博 API 客户端（带 Cookie 的 HTTP 会话）"""

    def __init__(self, cookie: str, user_agent: str = _DEFAULT_UA) -> None:
        self._cookie = cookie
        self._headers: dict[str, str] = {
            "User-Agent": user_agent,
            "Referer": "https://weibo.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if cookie:
            self._headers["Cookie"] = cookie


# ── 公共辅助函数 ──────────────────────────────────────────


def _clean_html(text: str) -> str:
    """去除 HTML 标签和实体，保留纯文本。

    Args:
        text: 可能包含 HTML 标记的原始文本

    Returns:
        清理后的纯文本
    """
    if not text:
        return ""
    text = _HTML_TAG_RE.sub("", text)
    text = _HTML_ENTITY_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _parse_weibo_time(time_str: str) -> int:
    """将微博时间字符串解析为 Unix 时间戳。

    支持格式：
    - 标准格式: "Tue Jun 11 10:00:00 +0800 2026"
    - 中文格式: "刚刚", "5分钟前", "今天 10:00", "06-11 10:00", "2026-06-11 10:00"

    Args:
        time_str: 微博时间字符串

    Returns:
        Unix 时间戳
    """
    if not time_str:
        return int(time.time())

    # 尝试标准格式
    try:
        dt = datetime.strptime(time_str, _WEIBO_TIME_FORMAT)
        return int(dt.timestamp())
    except (ValueError, OSError):
        pass

    # 尝试中文格式
    now = datetime.now()
    m = _CHINESE_TIME_RE.match(time_str)
    if m:
        groups = m.groups()
        if time_str == "刚刚":
            return int(time.time())
        if groups[0]:  # N分钟前
            return int(time.time()) - int(groups[0]) * 60
        if groups[1] and groups[2]:  # 今天 HH:MM
            today = now.replace(hour=int(groups[1]), minute=int(groups[2]), second=0, microsecond=0)
            return int(today.timestamp())
        if groups[3] and groups[4]:  # 月-日 HH:MM
            d = now.replace(month=int(groups[3]), day=int(groups[4]), hour=int(groups[5]), minute=int(groups[6]), second=0, microsecond=0)
            return int(d.timestamp())
        if groups[7] and groups[8]:  # 年-月-日 HH:MM
            d = datetime(int(groups[7]), int(groups[8]), int(groups[9]), int(groups[10]), int(groups[11]))
            return int(d.timestamp())

    return int(time.time())


def _parse_mobile_post(raw: dict[str, Any]) -> WeiboPost | None:
    """解析移动端 API 返回的单条微博数据。

    移动端返回结构: {id, text, user, created_at, pics, reposts_count, ...}

    Args:
        raw: 移动端 API 返回的单条微博数据

    Returns:
        WeiboPost 或 None（解析失败时）
    """
    try:
        post_id = str(raw.get("id", ""))
        if not post_id:
            return None

        text = raw.get("text", "")
        clean_text = _clean_html(text)
        user_info = raw.get("user", {})
        author = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        pubdate = _parse_weibo_time(raw.get("created_at", ""))

        # 图片列表
        image_urls: list[str] = []
        pics = raw.get("pics", [])
        if isinstance(pics, list):
            for pic in pics:
                url = ""
                if isinstance(pic, dict):
                    url = pic.get("url", "") or pic.get("large", {}).get("url", "")
                elif isinstance(pic, str):
                    url = pic
                if url:
                    image_urls.append(url)

        # 统计数据
        reposts_count = int(raw.get("reposts_count", 0) or 0)
        comments_count = int(raw.get("comments_count", 0) or 0)
        likes_count = int(raw.get("attitudes_count", 0) or 0)
        is_original = bool(raw.get("is_original", 1))

        # 转发微博（嵌套）
        reposted_post: WeiboPost | None = None
        retweeted = raw.get("retweeted_status")
        if isinstance(retweeted, dict) and retweeted.get("id"):
            reposted_post = _parse_mobile_post(retweeted)

        return WeiboPost(
            post_id=post_id,
            text=text,
            clean_text=clean_text,
            author=author,
            user_id=user_id,
            pubdate=pubdate,
            image_urls=image_urls,
            reposts_count=reposts_count,
            comments_count=comments_count,
            likes_count=likes_count,
            is_original=is_original,
            reposted_post=reposted_post,
        )
    except Exception as e:
        logger.debug("解析移动端微博数据失败: %s", e)
        return None


def _parse_pc_post(raw: dict[str, Any]) -> WeiboPost | None:
    """解析 PC 端 API 返回的单条微博数据。

    PC 端返回结构: {id, idstr, text, user, created_at, pic_ids, pic_infos, ...}

    Args:
        raw: PC 端 API 返回的单条微博数据

    Returns:
        WeiboPost 或 None（解析失败时）
    """
    try:
        post_id = str(raw.get("idstr", "") or raw.get("id", ""))
        if not post_id:
            return None

        text = raw.get("text", "")
        clean_text = _clean_html(text)
        user_info = raw.get("user", {})
        author = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        pubdate = _parse_weibo_time(raw.get("created_at", ""))

        # 图片列表（PC 端使用 pic_ids + pic_infos）
        image_urls: list[str] = []
        pic_ids = raw.get("pic_ids", [])
        pic_infos = raw.get("pic_infos", {})
        if isinstance(pic_ids, list) and pic_ids:
            for pid in pic_ids:
                if isinstance(pic_infos, dict) and pid in pic_infos:
                    info = pic_infos[pid]
                    if isinstance(info, dict):
                        url = info.get("original", {}).get("url", "") or info.get("large", {}).get("url", "")
                        if url:
                            image_urls.append(url)
                            continue
                # 降级：使用模板 URL
                image_urls.append(SINAIMG_URL_TEMPLATE.format(pic_id=pid))

        # 统计数据（PC 端返回字符串）
        reposts_count = int(raw.get("reposts_count", "0") or "0")
        comments_count = int(raw.get("comments_count", "0") or "0")
        likes_count = int(raw.get("attitudes_count", "0") or "0")
        is_original = bool(raw.get("is_original", 1))

        # 转发微博
        reposted_post: WeiboPost | None = None
        retweeted = raw.get("retweeted_status")
        if isinstance(retweeted, dict) and retweeted.get("id"):
            reposted_post = _parse_pc_post(retweeted)

        return WeiboPost(
            post_id=post_id,
            text=text,
            clean_text=clean_text,
            author=author,
            user_id=user_id,
            pubdate=pubdate,
            image_urls=image_urls,
            reposts_count=reposts_count,
            comments_count=comments_count,
            likes_count=likes_count,
            is_original=is_original,
            reposted_post=reposted_post,
        )
    except Exception as e:
        logger.debug("解析 PC 端微博数据失败: %s", e)
        return None


# ── 获取用户微博列表 ──────────────────────────────────────


async def fetch_user_posts_mobile(
    cookie: str,
    user_id: str,
    max_posts: int = 20,
    user_agent: str = _DEFAULT_UA,
) -> list[dict[str, Any]]:
    """通过移动端 API 获取用户微博列表。

    移动端 API 不需要签名，请求简单，是优先使用的数据源。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        user_agent: User-Agent

    Returns:
        原始微博数据字典列表
    """
    url = MOBILE_USER_POSTS_API.format(user_id=user_id)
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }

    session = await get_session()
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("移动端 API 返回状态码: %s", resp.status)
                return []
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("移动端 API 请求失败: %s", e)
        return []

    if not data.get("ok"):
        logger.debug("移动端 API 返回失败: %s", data.get("msg", "unknown"))
        return []

    cards = data.get("data", {}).get("cards", [])
    posts: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        mblog = card.get("mblog")
        if isinstance(mblog, dict) and mblog.get("id"):
            posts.append(mblog)
            if len(posts) >= max_posts:
                break

    return posts


async def fetch_user_posts_pc(
    cookie: str,
    user_id: str,
    max_posts: int = 20,
    user_agent: str = _DEFAULT_UA,
) -> list[dict[str, Any]]:
    """通过 PC 端 API 获取用户微博列表。

    PC 端 API 返回数据更丰富（含 pic_infos），但可能需要完整 Cookie。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        user_agent: User-Agent

    Returns:
        原始微博数据字典列表
    """
    url = PC_USER_POSTS_API.format(user_id=user_id)
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }

    session = await get_session()
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("PC 端 API 返回状态码: %s", resp.status)
                return []
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning("PC 端 API 请求失败: %s", e)
        return []

    if not data.get("ok"):
        logger.debug("PC 端 API 返回失败: %s", data.get("msg", "unknown"))
        return []

    post_list = data.get("data", {}).get("list", [])
    if not isinstance(post_list, list):
        return []

    return post_list[:max_posts]


async def fetch_user_posts(
    cookie: str,
    user_id: str,
    max_posts: int = 20,
    prefer_pc: bool = False,
) -> list[WeiboPost]:
    """获取用户微博列表，自动选择 API 并解析为 WeiboPost。

    优先使用移动端 API（无需签名），降级使用 PC 端 API。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        prefer_pc: 是否优先使用 PC 端 API

    Returns:
        解析后的 WeiboPost 列表
    """
    if prefer_pc:
        raw_posts = await fetch_user_posts_pc(cookie, user_id, max_posts)
        parse_func = _parse_pc_post
        if not raw_posts:
            raw_posts = await fetch_user_posts_mobile(cookie, user_id, max_posts)
            parse_func = _parse_mobile_post
    else:
        raw_posts = await fetch_user_posts_mobile(cookie, user_id, max_posts)
        parse_func = _parse_mobile_post
        if not raw_posts:
            raw_posts = await fetch_user_posts_pc(cookie, user_id, max_posts)
            parse_func = _parse_pc_post

    results: list[WeiboPost] = []
    for raw in raw_posts:
        post = parse_func(raw)
        if post is not None:
            results.append(post)

    return results
```

**Test command**: `pytest -x tests/test_weibo_api.py`

---

## Task 9: Write tests for Weibo monitor

**File**: `tests/test_weibo_monitor.py`

```python
"""Tests for platforms/weibo/monitor.py — Weibo post monitoring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.monitor import WeiboSubscriptionStore, check_new_weibo_posts
from shared.config import Config
from shared.protocols import WeiboPost


class TestWeiboSubscriptionStore:
    def test_init_defaults(self, tmp_path):
        store = WeiboSubscriptionStore(str(tmp_path))
        assert store.known_count == 0

    def test_mark_known_post(self, tmp_path):
        store = WeiboSubscriptionStore(str(tmp_path))
        post = WeiboPost(
            post_id="post123",
            text="test",
            clean_text="test",
            author="u",
            user_id="1",
            pubdate=1000,
        )
        store.mark_known_weibo_post(post)
        assert store.is_known("post123")


class TestCheckNewWeiboPosts:
    @pytest.mark.asyncio
    async def test_returns_new_posts(self):
        cfg = Config()
        cfg.weibo.auth.cookie = "SUB=fake"
        store = MagicMock()
        store.is_known.return_value = False

        mock_post = WeiboPost(
            post_id="new123",
            text="新微博",
            clean_text="新微博",
            author="用户A",
            user_id="12345",
            pubdate=2000,
        )

        with patch("platforms.weibo.monitor.fetch_user_posts", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = [mock_post]
            results = await check_new_weibo_posts(
                user_id="12345", name="用户A", config=cfg, store=store,
            )

        assert len(results) == 1
        assert results[0].post_id == "new123"

    @pytest.mark.asyncio
    async def test_filters_known_posts(self):
        cfg = Config()
        cfg.weibo.auth.cookie = "SUB=fake"
        store = MagicMock()
        store.is_known.side_effect = lambda pid: pid == "old123"

        old_post = WeiboPost(
            post_id="old123", text="旧微博", clean_text="旧微博",
            author="u", user_id="1", pubdate=1000,
        )
        new_post = WeiboPost(
            post_id="new123", text="新微博", clean_text="新微博",
            author="u", user_id="1", pubdate=2000,
        )

        with patch("platforms.weibo.monitor.fetch_user_posts", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = [old_post, new_post]
            results = await check_new_weibo_posts(
                user_id="12345", name="用户A", config=cfg, store=store,
            )

        assert len(results) == 1
        assert results[0].post_id == "new123"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cookie(self):
        cfg = Config()
        cfg.weibo.auth.cookie = ""
        store = MagicMock()

        results = await check_new_weibo_posts(
            user_id="12345", name="用户A", config=cfg, store=store,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_failure(self):
        cfg = Config()
        cfg.weibo.auth.cookie = "SUB=fake"
        store = MagicMock()

        with patch("platforms.weibo.monitor.fetch_user_posts", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = []
            results = await check_new_weibo_posts(
                user_id="12345", name="用户A", config=cfg, store=store,
            )

        assert results == []
```

**Test command**: `pytest -x tests/test_weibo_monitor.py` (will fail initially)

---

## Task 10: Implement `platforms/weibo/monitor.py`

**File**: `platforms/weibo/monitor.py`

```python
"""微博内容监控模块 - 检测用户新微博"""

from __future__ import annotations

import logging

from rich.console import Console

from platforms.weibo.api import fetch_user_posts
from shared.config import Config
from shared.protocols import JsonSetStore, WeiboPost

logger = logging.getLogger(__name__)
console = Console()

# 默认每次检查最大微博数
DEFAULT_MAX_POSTS_PER_CHECK = 10


class WeiboSubscriptionStore(JsonSetStore):
    """微博已知帖子存储，用于去重。

    继承 JsonSetStore，管理 data/known_weibo_posts.json 文件。
    """

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "known_weibo_posts.json")

    def mark_known_weibo_post(self, post: WeiboPost) -> None:
        """将帖子标记为已知（便利方法）。"""
        self.mark_known(post.post_id)


async def check_new_weibo_posts(
    user_id: str,
    name: str,
    config: Config,
    store: WeiboSubscriptionStore,
    max_posts: int = DEFAULT_MAX_POSTS_PER_CHECK,
) -> list[WeiboPost]:
    """检查指定用户的新微博。

    获取用户微博列表，过滤已知微博，返回新增微博列表。

    Args:
        user_id: 微博用户 ID
        name: 用户名称（用于日志）
        config: 全局配置
        store: 已知帖子存储
        max_posts: 单次检查最大返回帖子数

    Returns:
        新增的 WeiboPost 列表（按发布时间降序）
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.error("[%s] 缺少 Cookie，无法检查微博", name)
        return []

    logger.info("检查用户 %s (%s) 的新微博", name, user_id)

    try:
        posts = await fetch_user_posts(cookie, user_id, max_posts)
    except Exception as e:
        logger.error("获取用户 %s 微博失败: %s", user_id, e)
        return []

    if not posts:
        logger.info("[%s] 未获取到任何微博", name)
        return []

    new_posts: list[WeiboPost] = []
    for post in posts:
        if store.is_known(post.post_id):
            continue
        new_posts.append(post)

    # 按发布时间降序
    new_posts.sort(key=lambda p: p.pubdate, reverse=True)

    # 限制数量
    if len(new_posts) > max_posts:
        new_posts = new_posts[:max_posts]

    logger.info("[%s] 发现 %d 条新微博", name, len(new_posts))
    return new_posts
```

**Test command**: `pytest -x tests/test_weibo_monitor.py`

---

## Task 11: Implement `platforms/weibo/comments.py`

**File**: `platforms/weibo/comments.py`

```python
"""微博评论亮点抓取模块 - 获取热门评论"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from rich.console import Console

from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS, WEIBO_REQUEST_TIMEOUT
from shared.http import get_session
from shared.protocols import WeiboCommentHighlight

logger = logging.getLogger(__name__)
console = Console()

# 微博评论 API（移动端）
COMMENT_API = "https://m.weibo.cn/comments/hotflow?id={post_id}&mid={post_id}&max_id_type=0"


def _get_default_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )


def _parse_comment(comment_data: dict[str, Any], author_user_id: str = "") -> WeiboCommentHighlight | None:
    """解析单条评论数据。

    Args:
        comment_data: API 返回的评论数据
        author_user_id: 微博作者 ID（用于判断 is_author）

    Returns:
        WeiboCommentHighlight 或 None
    """
    try:
        content = comment_data.get("text", "")
        if not content:
            return None

        # 去除 HTML 标签
        import re
        content = re.sub(r"<[^>]+>", "", content)
        content = content.strip()
        if not content:
            return None

        user_info = comment_data.get("user", {})
        user_name = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        like_count = int(comment_data.get("like_count", 0) or 0)
        is_author = bool(author_user_id and user_id == author_user_id)

        return WeiboCommentHighlight(
            content=content,
            user_name=user_name,
            is_author=is_author,
            like_count=like_count,
        )
    except Exception as e:
        logger.debug("解析评论数据失败: %s", e)
        return None


async def fetch_weibo_comment_highlights(
    post_id: str,
    config: Config,
    *,
    author_user_id: str = "",
    max_count: int = MAX_COMMENT_HIGHLIGHTS,
) -> list[WeiboCommentHighlight]:
    """获取微博帖子的评论亮点（热门评论）。

    按点赞数降序排列，最多返回 max_count 条。
    失败时返回空列表，不影响主流程。

    Args:
        post_id: 帖子 ID
        config: 全局配置
        author_user_id: 帖子作者 ID（用于过滤作者评论）
        max_count: 最大返回数量

    Returns:
        评论亮点列表
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.debug("[评论] 缺少 Cookie，跳过评论抓取: %s", post_id)
        return []

    url = COMMENT_API.format(post_id=post_id)
    headers = {
        "User-Agent": _get_default_ua(),
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }

    all_comments: list[WeiboCommentHighlight] = []
    session = await get_session()

    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.debug("[评论] API 返回状态码: %s, post_id: %s", resp.status, post_id)
                return []
            data = await resp.json(content_type=None)

        if not data.get("ok"):
            logger.debug("[评论] API 失败: %s, post_id: %s", data.get("msg", "unknown"), post_id)
            return []

        comments_raw = data.get("data", {}).get("data", [])
        if not isinstance(comments_raw, list):
            return []

        for raw in comments_raw:
            comment = _parse_comment(raw, author_user_id)
            if comment is None:
                continue
            all_comments.append(comment)

    except Exception as e:
        logger.warning("[评论] 抓取评论异常: %s, post_id: %s", e, post_id)
        return []

    # 按点赞数降序
    all_comments.sort(key=lambda c: c.like_count, reverse=True)
    result = all_comments[:max_count]

    logger.info("[评论] 获取到 %d 条热门评论, post_id: %s", len(result), post_id)
    return result
```

---

## Task 11b: Write tests for `platforms/weibo/comments.py`

**File**: `tests/test_weibo_comments.py`

```python
"""Tests for platforms/weibo/comments.py — Weibo comment highlights."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.comments import (
    _parse_comment,
    fetch_weibo_comment_highlights,
)
from shared.protocols import WeiboCommentHighlight


# ── _parse_comment ─────────────────────────────────────────


class TestParseComment:
    def test_parses_basic_comment(self):
        raw = {
            "text": "这是一条评论 <a href='/n/user'>@user</a>",
            "user": {"screen_name": "评论用户", "id": 12345},
            "like_count": 42,
        }
        result = _parse_comment(raw, author_user_id="999")
        assert result is not None
        assert "这是一条评论" in result.content
        assert result.user_name == "评论用户"
        assert result.is_author is False
        assert result.like_count == 42

    def test_marks_author_comment(self):
        raw = {
            "text": "作者回复",
            "user": {"screen_name": "博主", "id": 999},
            "like_count": 10,
        }
        result = _parse_comment(raw, author_user_id="999")
        assert result is not None
        assert result.is_author is True

    def test_returns_none_on_empty_text(self):
        raw = {"text": "", "user": {"screen_name": "u", "id": 1}, "like_count": 0}
        assert _parse_comment(raw) is None

    def test_returns_none_on_missing_text(self):
        raw = {"user": {"screen_name": "u", "id": 1}}
        assert _parse_comment(raw) is None

    def test_handles_missing_user(self):
        raw = {"text": "hello", "like_count": 5}
        result = _parse_comment(raw)
        assert result is not None
        assert result.user_name == ""
        assert result.is_author is False
        assert result.like_count == 5


# ── fetch_weibo_comment_highlights ─────────────────────────


class TestFetchWeiboCommentHighlights:
    @pytest.mark.asyncio
    async def test_returns_highlights(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "data": [
                        {
                            "text": "好评论",
                            "user": {"screen_name": "用户A", "id": 1},
                            "like_count": 100,
                        },
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert len(results) == 1
        assert results[0].content == "好评论"
        assert results[0].like_count == 100

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cookie(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = ""

        results = await fetch_weibo_comment_highlights("post123", cfg)
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert results == []

    @pytest.mark.asyncio
    async def test_sorts_by_like_count_descending(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "data": [
                        {"text": "低赞", "user": {"screen_name": "u", "id": 1}, "like_count": 1},
                        {"text": "高赞", "user": {"screen_name": "u", "id": 2}, "like_count": 99},
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert len(results) == 2
        assert results[0].content == "高赞"
        assert results[1].content == "低赞"
```

**Test command**: `pytest -x tests/test_weibo_comments.py`

---

## Task 12: Implement `platforms/weibo/downloader.py`

**File**: `platforms/weibo/downloader.py`

```python
"""微博媒体下载模块 - 下载微博图片"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiohttp

from rich.console import Console

from shared.config import Config
from shared.constants import WEIBO_DOWNLOAD_TIMEOUT
from shared.http import get_session
from shared.protocols import WeiboDownloadResult, WeiboPost

logger = logging.getLogger(__name__)
console = Console()


def _get_post_dir(config: Config, post_id: str) -> Path:
    """获取帖子下载目录。

    Args:
        config: 全局配置
        post_id: 帖子 ID

    Returns:
        帖子专用下载目录路径
    """
    base = Path(config.download.dir) / "weibo" / post_id
    base.mkdir(parents=True, exist_ok=True)
    return base


async def _download_file(url: str, dest: Path) -> bool:
    """下载文件到指定路径。

    Args:
        url: 文件 URL
        dest: 目标路径

    Returns:
        是否成功
    """
    session = await get_session()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=WEIBO_DOWNLOAD_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.debug("下载文件失败，状态码: %s, URL: %s", resp.status, url)
                return False
            content = await resp.read()
        dest.write_bytes(content)
        return True
    except Exception as e:
        logger.debug("下载文件异常: %s, URL: %s", e, url)
        return False


async def download_weibo_media(post: WeiboPost, config: Config) -> WeiboDownloadResult:
    """下载微博帖子的媒体文件（图片）。

    Args:
        post: 微博帖子
        config: 全局配置

    Returns:
        下载结果
    """
    if not post.image_urls:
        return WeiboDownloadResult(
            success=True,
            source_id=post.post_id,
            title=post.clean_text[:50] if post.clean_text else post.post_id,
            text=post.clean_text,
        )

    post_dir = _get_post_dir(config, post.post_id)
    image_paths: list[Path] = []

    for idx, img_url in enumerate(post.image_urls):
        # 从 URL 猜测扩展名
        ext = ".jpg"
        lower_url = img_url.lower()
        if ".png" in lower_url:
            ext = ".png"
        elif ".webp" in lower_url:
            ext = ".webp"
        elif ".gif" in lower_url:
            ext = ".gif"

        img_path = post_dir / f"{idx + 1}{ext}"
        ok = await _download_file(img_url, img_path)
        if ok:
            image_paths.append(img_path)

    success = len(image_paths) > 0 or not post.image_urls
    return WeiboDownloadResult(
        success=success,
        source_id=post.post_id,
        title=post.clean_text[:50] if post.clean_text else post.post_id,
        text=post.clean_text,
        image_paths=image_paths,
        error=None if success else "图片下载全部失败",
    )
```

---

## Task 12b: Write tests for `platforms/weibo/downloader.py`

**File**: `tests/test_weibo_downloader.py`

```python
"""Tests for platforms/weibo/downloader.py — Weibo media download."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.downloader import _download_file, download_weibo_media
from shared.protocols import WeiboPost


# ── _download_file ─────────────────────────────────────────


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_downloads_successfully(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 200

        async def read_side() -> bytes:
            return b"fake_image_data"

        mock_resp.read = AsyncMock(side_effect=read_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.get_session", return_value=mock_session):
            result = await _download_file(url, dest)

        assert result is True
        assert dest.read_bytes() == b"fake_image_data"

    @pytest.mark.asyncio
    async def test_fails_on_bad_status(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.get_session", return_value=mock_session):
            result = await _download_file(url, dest)

        assert result is False
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_fails_on_exception(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=Exception("network error"))

        with patch("platforms.weibo.downloader.get_session", return_value=mock_session):
            result = await _download_file(url, dest)

        assert result is False


# ── download_weibo_media ───────────────────────────────────


def _make_post(image_urls: list[str] | None = None) -> WeiboPost:
    return WeiboPost(
        post_id="post123",
        text="测试微博内容",
        clean_text="测试微博内容",
        author="用户A",
        user_id="12345",
        pubdate=1000,
        image_urls=image_urls or [],
    )


class TestDownloadWeiboMedia:
    @pytest.mark.asyncio
    async def test_returns_success_with_no_images(self):
        cfg = MagicMock()
        cfg.download.dir = "/tmp/downloads"

        post = _make_post(image_urls=[])
        result = await download_weibo_media(post, cfg)

        assert result.success is True
        assert result.source_id == "post123"
        assert result.image_paths == []

    @pytest.mark.asyncio
    async def test_downloads_all_images(self, tmp_path):
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_post(image_urls=[
            "https://example.com/img1.jpg",
            "https://example.com/img2.png",
        ])

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"data")
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.get_session", return_value=mock_session):
            result = await download_weibo_media(post, cfg)

        assert result.success is True
        assert len(result.image_paths) == 2
        # Files should exist on disk
        for path in result.image_paths:
            assert path.exists()

    @pytest.mark.asyncio
    async def test_reports_failure_when_all_downloads_fail(self, tmp_path):
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_post(image_urls=["https://example.com/img1.jpg"])

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.get_session", return_value=mock_session):
            result = await download_weibo_media(post, cfg)

        assert result.success is False
        assert result.error is not None
```

**Test command**: `pytest -x tests/test_weibo_downloader.py`

---

## Task 13: Implement `platforms/weibo/parser.py`

**File**: `platforms/weibo/parser.py`

```python
"""微博内容解析模块 - 提取帖子正文、标签"""

from __future__ import annotations

import re

from shared.protocols import WeiboDownloadResult, WeiboPost

# 话题标签正则：匹配 #话题# 格式
_TOPIC_PATTERN = re.compile(r"#([^#]+)#")


def _extract_topics(text: str) -> list[str]:
    """从文本中提取 #话题# 格式的话题标签。

    Args:
        text: 文本内容

    Returns:
        去重后的话题标签列表
    """
    topics = _TOPIC_PATTERN.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for topic in topics:
        topic = topic.strip()
        if topic and topic not in seen:
            seen.add(topic)
            result.append(topic)
    return result


def parse_weibo_post(post: WeiboPost, download_result: WeiboDownloadResult) -> dict:
    """解析微博帖子，提取正文、话题标签。

    当前微博帖子结构简单（文本+图片），不需要复杂的 ParsedNote。
    返回包含提取结果的字典，便于后续摘要生成和通知。

    Args:
        post: 微博帖子
        download_result: 下载结果

    Returns:
        解析结果字典，包含:
        - post_id: str
        - text: str (clean_text)
        - topics: list[str]
        - image_paths: list[Path]
    """
    text = download_result.text or post.clean_text
    topics = _extract_topics(text)

    return {
        "post_id": post.post_id,
        "text": text,
        "topics": topics,
        "image_paths": list(download_result.image_paths),
    }
```

---

## Task 13b: Write tests for `platforms/weibo/parser.py`

**File**: `tests/test_weibo_parser.py`

```python
"""Tests for platforms/weibo/parser.py — Weibo content parsing."""

from __future__ import annotations

from pathlib import Path

from platforms.weibo.parser import _extract_topics, parse_weibo_post
from shared.protocols import WeiboDownloadResult, WeiboPost


# ── _extract_topics ────────────────────────────────────────


class TestExtractTopics:
    def test_extracts_single_topic(self):
        text = "今天天气真好 #生活记录# 开心"
        assert _extract_topics(text) == ["生活记录"]

    def test_extracts_multiple_topics(self):
        text = "#科技# #AI# 新突破"
        assert _extract_topics(text) == ["科技", "AI"]

    def test_deduplicates_topics(self):
        text = "#科技# 话题 #科技# "
        assert _extract_topics(text) == ["科技"]

    def test_returns_empty_when_no_topics(self):
        text = "普通微博内容"
        assert _extract_topics(text) == []

    def test_handles_empty_text(self):
        assert _extract_topics("") == []

    def test_trims_whitespace(self):
        text = "#  空间话题  #"
        result = _extract_topics(text)
        assert result == ["空间话题"]


# ── parse_weibo_post ───────────────────────────────────────


def _make_post(text: str = "测试内容 #话题#") -> WeiboPost:
    return WeiboPost(
        post_id="post123",
        text=text,
        clean_text=text,
        author="用户A",
        user_id="12345",
        pubdate=1000,
    )


def _make_dl_result(image_paths: list[Path] | None = None) -> WeiboDownloadResult:
    return WeiboDownloadResult(
        success=True,
        source_id="post123",
        title="测试",
        text="",
        image_paths=image_paths or [],
    )


class TestParseWeiboPost:
    def test_returns_post_id_and_text(self):
        post = _make_post()
        dl = _make_dl_result()
        result = parse_weibo_post(post, dl)

        assert result["post_id"] == "post123"
        assert result["text"] == "测试内容 #话题#"

    def test_extracts_topics(self):
        post = _make_post("今天 #科技# 发展 #AI#")
        dl = _make_dl_result()
        result = parse_weibo_post(post, dl)

        assert result["topics"] == ["科技", "AI"]

    def test_prefers_download_result_text(self):
        post = _make_post("原始内容")
        dl = _make_dl_result()
        dl.text = "下载结果文本"
        result = parse_weibo_post(post, dl)

        assert result["text"] == "下载结果文本"

    def test_includes_image_paths(self):
        post = _make_post()
        dl = _make_dl_result(image_paths=[Path("/tmp/img1.jpg"), Path("/tmp/img2.jpg")])
        result = parse_weibo_post(post, dl)

        assert len(result["image_paths"]) == 2
        assert result["image_paths"][0] == Path("/tmp/img1.jpg")

    def test_returns_empty_topics_when_none(self):
        post = _make_post(text="无话题")
        dl = _make_dl_result()
        result = parse_weibo_post(post, dl)

        assert result["topics"] == []
```

**Test command**: `pytest -x tests/test_weibo_parser.py`

---

## Task 14: Add `notify_new_weibo_post()` to `core/notifier.py`

**File**: `core/notifier.py`

Add after line 238 (after `notify_new_xhs_note`):

```python
# ── 微博帖子通知 ───────────────────────────────────────────────


async def notify_new_weibo_post(
    post_id: str,
    title: str,
    author: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
    weibo_noti_config: NotificationConfig | None = None,
    *,
    gotify_url: str = "",
    gotify_token: str = "",
) -> bool:
    """发送微博新帖子通知。

    构造 Markdown 格式的通知消息，包含帖子和 AI 摘要。

    Args:
        post_id: 微博帖子 ID
        title: 帖子标题/摘要
        author: 作者名称
        summary: AI 摘要文本
        keywords: 关键词列表
        comment_highlights: 评论区精选内容（可选）
        weibo_noti_config: 微博通知配置
        gotify_url: Gotify URL（备选参数）
        gotify_token: Gotify Token（备选参数）

    Returns:
        是否发送成功
    """
    if weibo_noti_config is None:
        weibo_noti_config = NotificationConfig(
            enabled=True,
            gotify_url=gotify_url,
            gotify_token=gotify_token,
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    keywords_str = "；".join(keywords) if keywords else "无"
    post_url = f"https://weibo.com/{post_id}"

    parts: list[str] = [
        f"**作者:** {author}",
        f"**链接:** [{post_id}]({post_url})",
        f"**发布时间:** {now}",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend([
            "",
            "**评论区补充:**",
            comment_highlights,
        ])

    message = "\n".join(parts)
    return await send_gotify(
        title=f"🐦 {title}",
        message=message,
        config=weibo_noti_config,
    )
```

Also add the export to the import line in `core/pipeline.py` (line 59):

Update:
```python
from core.notifier import notify_new_video, notify_new_xhs_note, notify_dynamic  # noqa: E402
```
to:
```python
from core.notifier import notify_new_video, notify_new_xhs_note, notify_new_weibo_post, notify_dynamic  # noqa: E402
```

---

## Task 15: Add pipeline `run_weibo_check_once()` and `process_weibo_post()`

**File**: `core/pipeline.py`

### Step 15a: Add imports (after line 72 — the xiaohongshu imports block)

```python
# ── 微博 ──────────────────────────────────────────────────
from platforms.weibo.monitor import (  # noqa: E402
    WeiboSubscriptionStore,
    check_new_weibo_posts,
)
from platforms.weibo.downloader import download_weibo_media  # noqa: E402
from platforms.weibo.parser import parse_weibo_post  # noqa: E402
from platforms.weibo.comments import fetch_weibo_comment_highlights  # noqa: E402
```

### Step 15b: Add Weibo to _Stats (after line 89, notes_failed)

```python
        self.weibo_posts_processed: int = 0
        self.weibo_posts_succeeded: int = 0
        self.weibo_posts_failed: int = 0
```

### Step 15c: Update _Stats.report() (after line 110)

```python
        if self.weibo_posts_processed:
            lines.append(
                f"  微博: {self.weibo_posts_processed} 处理, "
                f"{self.weibo_posts_succeeded} 成功, "
                f"{self.weibo_posts_failed} 失败"
            )
```

### Step 15d: Add `run_weibo_check_once()` and `process_weibo_post()` (after line 601, before `run_check_once`)

```python

# ═══════════════════════════════════════════════════════════
# 微博完整流程
# ═══════════════════════════════════════════════════════════


async def run_weibo_check_once(config: Config) -> None:
    """微博完整检查流程"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None  # 由 run_check_once 初始化

    store = WeiboSubscriptionStore("data")

    console.print("[cyan]🔍 检查微博新帖子…[/cyan]")

    for sub in config.weibo.subscriptions:
        try:
            new_posts = await check_new_weibo_posts(
                user_id=sub.user_id, name=sub.name, config=config, store=store,
            )
            for post in new_posts:
                _run_stats.weibo_posts_processed += 1
                try:
                    await process_weibo_post(post, config, store)
                    _run_stats.weibo_posts_succeeded += 1
                except Exception as exc:
                    _run_stats.weibo_posts_failed += 1
                    console.print(
                        f"[red]✗ 处理微博 {post.post_id} 失败: {exc}[/red]"
                    )
                    logger.exception("Failed to process weibo post %s", post.post_id)
        except Exception as exc:
            console.print(
                f"[yellow]⚠️  检查 {sub.name}({sub.user_id}) 失败: {exc}[/yellow]"
            )
            logger.warning(
                "Weibo check failed for %s(%s): %s", sub.name, sub.user_id, exc
            )

    # 持久化 Store
    store.save()
    console.print("[green]✓ 微博检查完成[/green]")


# ═══════════════════════════════════════════════════════════
# 微博帖子处理流水线
# ═══════════════════════════════════════════════════════════


async def process_weibo_post(
    post: WeiboPost,
    config: Config,
    store: WeiboSubscriptionStore,
) -> None:
    """处理单个微博帖子的完整流水线"""
    display_title = post.clean_text[:50] if post.clean_text else post.post_id
    console.print(f"[bold yellow]▶ 处理微博[/bold yellow] {display_title} ({post.post_id})")

    # Step 1: 下载媒体
    dl_result: WeiboDownloadResult | None = None
    try:
        console.print("  [dim]⬇ 下载图片…[/dim]")
        dl_result = await download_weibo_media(post=post, config=config)
    except Exception as exc:
        console.print(f"  [red]✗ 下载失败: {exc}[/red]")
        logger.exception("Weibo download failed for %s", post.post_id)
        store.mark_known_weibo_post(post)
        return

    if not dl_result.success:
        console.print(f"  [yellow]⚠️  下载未成功: {dl_result.error}[/yellow]")
        store.mark_known_weibo_post(post)
        return

    # Step 2: 解析内容
    parsed: dict = {}
    try:
        console.print("  [dim]📄 解析内容…[/dim]")
        parsed = parse_weibo_post(post=post, download_result=dl_result)
    except Exception as exc:
        console.print(f"  [red]✗ 内容解析失败: {exc}[/red]")
        logger.exception("Weibo parse failed for %s", post.post_id)

    # Step 3: 评论亮点
    highlights: list[WeiboCommentHighlight] = []
    try:
        console.print("  [dim]💬 获取评论亮点…[/dim]")
        highlights = await fetch_weibo_comment_highlights(
            post_id=post.post_id, config=config, author_user_id=post.user_id,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/yellow]")
        logger.warning("Weibo comment highlights failed for %s: %s", post.post_id, exc)

    # Step 4: 生成摘要
    summary_text: str = ""
    content_text = parsed.get("text", "") or post.clean_text or ""

    try:
        console.print("  [dim]🤖 生成摘要…[/dim]")
        summary_text, _source, _is_ai = generate_summary(
            source_id=post.post_id,
            title=display_title,
            author=post.author,
            text=content_text,
            config=config,
        )
    except Exception as exc:
        console.print(f"  [red]✗ 摘要生成失败: {exc}[/red]")
        logger.exception("Weibo summary failed for %s", post.post_id)

    # Step 5: 提取关键词
    keywords: list[str] = []
    topics = parsed.get("topics", [])
    try:
        keywords = extract_keywords(
            text=summary_text, title=display_title, author=post.author, config=config,
        )
        # 合并话题标签
        if topics:
            keywords = list(dict.fromkeys(topics + keywords))  # 去重保序
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/yellow]")
        logger.warning("Weibo keywords failed for %s: %s", post.post_id, exc)
        keywords = topics  # 降级：使用话题标签

    # Step 6: 通知推送
    try:
        comment_md = _format_comment_highlights(highlights)
        await notify_new_weibo_post(
            post_id=post.post_id,
            title=display_title,
            author=post.author,
            summary=summary_text,
            keywords=keywords,
            comment_highlights=comment_md,
            weibo_noti_config=config.weibo.notification,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/yellow]")
        logger.warning("Weibo notify failed for %s: %s", post.post_id, exc)

    # Step 7: 标记已知
    store.mark_known_weibo_post(post)

    console.print("  [green]✓ 微博处理完成[/green]")
```

### Step 15e: Update `run_check_once()` (lines 608-635)

Change:
```python
    if platform in ("all", "bili"):
        await run_bili_check_once(config)

    if platform in ("all", "xhs") and config.xiaohongshu.enabled:
        await run_xhs_check_once(config)
```

To:
```python
    if platform in ("all", "bili"):
        await run_bili_check_once(config)

    if platform in ("all", "xhs") and config.xiaohongshu.enabled:
        await run_xhs_check_once(config)

    if platform in ("all", "weibo") and config.weibo.enabled:
        await run_weibo_check_once(config)
```

Also update the docstring for `run_check_once`:

```python
    Args:
        config: 全局配置
        platform: "all" | "bili" | "xhs" | "weibo"
```

---

## Task 16: Enable "weibo" in `run_check.py` CLI

**File**: `run_check.py`

### Step 16a: Enable `login --platform weibo` with cookie serialization (line 39-41)

First, remove the placeholder guard:

Change:
```python
    if platform in ("xhs", "weibo"):
        console.print(f"[yellow]{platform} 登录功能将在后续版本支持[/yellow]")
        return
```

To:
```python
    if platform == "xhs":
        console.print(f"[yellow]{platform} 登录功能将在后续版本支持[/yellow]")
        return
```

Then, ensure the login flow for `platform == "weibo"` serializes `PlatformTokens.cookies` (individual keys) to `WeiboAuth.cookie` (single string). After calling `authenticator.get_tokens(qr_key)`, add:

```python
    # Serialize individual cookie keys to a single semicolon-delimited string
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
```

This mirrors the serialization used in the token refresh path (Step 16c) and ensures consistency across all weibo auth flows.

### Step 16b: Remove the placeholder guard for `token refresh --platform weibo` (lines 115-117)

Change:
```python
    elif platform in ("xhs", "weibo"):
        console.print(f"[yellow]{platform} 续期功能将在后续版本支持[/yellow]")
        return
```

To:
```python
    elif platform == "xhs":
        console.print(f"[yellow]{platform} 续期功能将在后续版本支持[/yellow]")
        return
```

### Step 16c: Add weibo token refresh logic (after line 114, before the elif we just modified)

Replace the `elif` block from:

```python
    elif platform in ("xhs", "weibo"):
        console.print(f"[yellow]{platform} 续期功能将在后续版本支持[/yellow]")
        return
```

To the full refresh flow mirroring bili:

```python
    elif platform == "xhs":
        console.print(f"[yellow]{platform} 续期功能将在后续版本支持[/yellow]")
        return
    elif platform == "weibo":
        auth = config.weibo.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            console.print("[red]✗ 未配置微博 Cookie 或已过期，请先执行 trawler login --platform weibo[/red]")
            sys.exit(1)
        try:
            from platforms.weibo.auth import WeiboAuthenticator
            from shared.auth.base import PlatformTokens

            authenticator = WeiboAuthenticator()
            # Parse single cookie string into individual keys (SUB, SUBP, WBPSESS, ...)
            cookie_dict: dict[str, str] = {}
            for part in auth.cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    cookie_dict[k] = v

            current_tokens = PlatformTokens(
                platform="weibo",
                cookies=cookie_dict,
                obtained_at=time.time(),
                expires_at=auth.expires_at,
            )
            tokens = asyncio.run(authenticator.refresh_tokens(current_tokens))
            # Serialize individual keys back to a single cookie string
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
            update_auth_section(platform, auth_dict)
            console.print(f"[green]✓ weibo Token 续期成功[/green]")
        except Exception as exc:
            console.print(f"[red]✗ 续期失败: {exc}[/red]")
            sys.exit(1)
```

> **Cookie serialization detail**: `PlatformTokens.cookies` stores individual cookie keys (`SUB`, `SUBP`, `WBPSESS`), but `WeiboAuth.cookie` expects a single semicolon-delimited string like `"SUB=xxx; SUBP=yyy; WBPSESS=zzz"`. The flow above:
> 1. **Parses** the single string into individual keys (for `refresh_tokens` input)
> 2. **Serializes** individual keys back to a single string (for `WeiboAuth.cookie` persistence)
>
> This is consistent across auth → login → refresh → monitor paths.

---

## Task 17: Update `config.toml.example` if needed

The `config.toml.example` file already has the weibo section (lines 137-166). It already includes:

```toml
[weibo]
enabled = false

[weibo.auth]
cookie = ""
expires_at = 0

[weibo.monitor]
mode = "api"
interval_minutes = 10

[[weibo.subscriptions]]
user_id = ""
name = ""

[weibo.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5
```

No changes needed — the example config is already correct.

---

## Task 18: Verify WeiboPost import in pipeline and update imports

**File**: `core/pipeline.py`

Update the imports from `shared.protocols` to include Weibo types (lines 32-42):

```python
from shared.protocols import (
    CommentHighlight,
    DownloadResult,
    DynamicInfo,
    NoteInfo,
    ParsedNote,
    TranscriptResult,
    VideoInfo,
    WeiboCommentHighlight,
    WeiboDownloadResult,
    WeiboPost,
    XhsCommentHighlight,
    XhsDownloadResult,
)
```

---

## Task 19: Run full test suite and lint

```bash
# Format
ruff format .

# Lint
ruff check .

# Type check
pyright .

# Run all weibo tests
pytest -x tests/test_weibo_authenticator.py tests/test_weibo_api.py tests/test_weibo_monitor.py

# Run full test suite
pytest -x

# Run CLI tests specifically (to verify login/token/check commands)
pytest -x tests/test_cli.py
```

---

## Execution Order

The tasks should be executed in this order due to dependencies:

1. **Task 1** — `protocols.py` data models (no deps)
2. **Task 2** — `constants.py` constants (no deps)
3. **Task 3** — `__init__.py` (no deps)
4. **Task 4** — Test for authenticator (writes test, will fail)
5. **Task 5** — Authenticator implementation (makes tests pass)
6. **Task 6** — Factory registration (depends on Task 5)
7. **Task 7** — Tests for API (will fail initially)
8. **Task 8** — API implementation (makes tests pass)
9. **Task 9** — Tests for monitor (will fail initially)
10. **Task 10** — Monitor implementation (makes tests pass)
11. **Task 11** — Comments module (can be done after API)
11b. **Task 11b** — Tests for comments module (after Task 11)
12. **Task 12** — Downloader module (can be done after API)
12b. **Task 12b** — Tests for downloader module (after Task 12)
13. **Task 13** — Parser module (can be done after downloader)
13b. **Task 13b** — Tests for parser module (after Task 13)
14. **Task 14** — Notifier addition (can be done in parallel with 11-13)
15. **Task 15** — Pipeline integration (needs 10-14)
16. **Task 16** — CLI enablement (needs 5-6)
17. **Task 17** — Config example (no changes needed)
18. **Task 18** — Import fixups (needs 1-15)
19. **Task 19** — Full test run (needs everything)

**Parallel groups**:
- Group A (sequential): 1 → 2 → 3
- Group B (sequential): 4 → 5 → 6
- Group C (sequential): 7 → 8
- Group D (sequential): 9 → 10
- Group E (parallel with each other): 11, 12, 14
  - Sub-group E1 (sequential within): 11 → 11b
  - Sub-group E2 (sequential within): 12 → 12b
  - Sub-group E3 (sequential within): 13 → 13b (needs 12 first)
- Group F (sequential): 15 (needs 10, 11, 12, 13, 14)
- Group G (parallel with 15): 16 (needs 5, 6)
- Group H (sequential): 18 (needs 1, 15)
- Group I (final): 19

## Verification Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format .` passes
- [ ] `pyright .` passes
- [ ] `pytest -x tests/test_weibo_authenticator.py` — all green
- [ ] `pytest -x tests/test_weibo_api.py` — all green
- [ ] `pytest -x tests/test_weibo_monitor.py` — all green
- [ ] `pytest -x tests/test_weibo_comments.py` — all green
- [ ] `pytest -x tests/test_weibo_downloader.py` — all green
- [ ] `pytest -x tests/test_weibo_parser.py` — all green
- [ ] `pytest -x tests/test_cli.py` — all green
- [ ] `pytest -x` — full suite green
- [ ] `trawler login --platform weibo` prompts QR display
- [ ] `trawler token status` shows weibo row
- [ ] `trawler check --platform weibo` runs without error
