"""Tests for WeiboAuthenticator — fully mocked, no real Weibo API calls."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.auth import WeiboAuthenticator
from shared.auth.base import (
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


def _mock_resp(
    status: int = 200,
    json_data: dict | None = None,
    set_cookie: list[str] | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    resp.headers = MagicMock()
    resp.headers.getall.return_value = set_cookie or []
    return resp


# ── WeiboAuthenticator.generate_qr_code ────────────────────


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side(*args, **kwargs) -> dict:
            return {"data": {"qrid": "qr_abc123", "image": "data:image/png;base64,..."}}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_key == "qr_abc123"
        assert result.expires_in == 240


class TestGenerateQrCodeApiError:
    @pytest.mark.asyncio
    async def test_raises_on_bad_status(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls,
            pytest.raises(RuntimeError, match="生成二维码失败"),
        ):
            mock_cls.return_value.__aenter__.return_value = mock_session
            await auth.generate_qr_code()

    @pytest.mark.asyncio
    async def test_raises_on_missing_qrid(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side(*args, **kwargs) -> dict:
            return {"data": {}}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with (
            patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls,
            pytest.raises(RuntimeError, match="未获取到 qrid"),
        ):
            mock_cls.return_value.__aenter__.return_value = mock_session
            await auth.generate_qr_code()


# ── WeiboAuthenticator.poll_qr_status ──────────────────────


class TestPollQrStatus:
    _RETCODE_MAP: dict[str, tuple[int, QRStatus, bool]] = {
        "waiting": (50114001, QRStatus.WAITING, False),
        "scanned": (50114002, QRStatus.SCANNED, False),
        "success": (20000000, QRStatus.SUCCESS, True),
        "expired": (50114004, QRStatus.EXPIRED, False),
    }

    @pytest.mark.asyncio
    async def test_waiting(self):
        auth = WeiboAuthenticator()
        mock = _mock_resp(json_data={"retcode": 50114001})
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_scanned(self):
        auth = WeiboAuthenticator()
        mock = _mock_resp(json_data={"retcode": 50114002, "data": {"nickname": "测试用户"}})
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SCANNED
        assert not status.success

    @pytest.mark.asyncio
    async def test_success(self):
        auth = WeiboAuthenticator()
        mock = _mock_resp(json_data={"retcode": 20000000, "data": {"alt": "ST-xxx"}})
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SUCCESS
        assert status.success

    @pytest.mark.asyncio
    async def test_expired(self):
        auth = WeiboAuthenticator()
        mock = _mock_resp(json_data={"retcode": 50114004})
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.EXPIRED
        assert not status.success

    @pytest.mark.asyncio
    async def test_bad_status_returns_waiting(self):
        auth = WeiboAuthenticator()
        mock = _mock_resp(json_data={"retcode": 99999})  # Unknown retcode
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_non_json_response(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(side_effect=Exception("not json"))
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_http_error_returns_waiting(self):
        auth = WeiboAuthenticator()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success


# ── WeiboAuthenticator.get_tokens ──────────────────────────


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens(self):
        auth = WeiboAuthenticator()
        # Pre-populate last check data (as poll_qr_status would)
        auth._last_check_data = {
            "retcode": 20000000,
            "data": {"url": "https://passport.weibo.com/sso/v2/login?alt=TEST-ALT"},
        }
        # Mock the login URL call
        login_resp = _mock_resp(
            status=200,
            json_data={"retcode": 20000000},
            set_cookie=[
                "SUB=fake_sub; Path=/; Domain=.weibo.com; HttpOnly",
                "SUBP=fake_subp; Path=/; Domain=.weibo.com",
                "WBPSESS=fake_wbpsess; Path=/; Domain=.weibo.com",
                "SSOLoginState=1735689600; Path=/; Domain=.weibo.com",
            ],
        )
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=login_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            tokens = await auth.get_tokens("qr_abc")

        assert tokens.platform == "weibo"
        assert tokens.cookies["SUB"] == "fake_sub"
        assert tokens.cookies["SUBP"] == "fake_subp"
        assert tokens.cookies["WBPSESS"] == "fake_wbpsess"
        assert tokens.cookies["SSOLoginState"] == "1735689600"
        assert tokens.expires_at > time.time()

    @pytest.mark.asyncio
    async def test_raises_when_no_check_data(self):
        """No _last_check_data → raise."""
        auth = WeiboAuthenticator()
        auth._last_check_data = None

        with pytest.raises(RefreshFailedError, match="无可用响应数据"):
            await auth.get_tokens("qr_abc")

    @pytest.mark.asyncio
    async def test_raises_when_retcode_not_success(self):
        auth = WeiboAuthenticator()
        auth._last_check_data = {"retcode": 50114002}  # scanned, not success

        with pytest.raises(RefreshFailedError, match="二维码未成功登录"):
            await auth.get_tokens("qr_abc")

    @pytest.mark.asyncio
    async def test_raises_when_no_url_and_no_set_cookie(self):
        """No url in response and no Set-Cookie from fallback → raise."""
        auth = WeiboAuthenticator()
        auth._last_check_data = {
            "retcode": 20000000,
            "data": {"status": 3},  # no url
        }
        # Fallback /check returns no Set-Cookie
        fallback_resp = _mock_resp(status=302, set_cookie=[])
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=fallback_resp)

        with (
            patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls,
            pytest.raises(RefreshFailedError, match="未获取到 Cookie"),
        ):
            mock_cls.return_value.__aenter__.return_value = mock_session
            await auth.get_tokens("qr_abc")

    @pytest.mark.asyncio
    async def test_fallback_to_redirect_cookies(self):
        """No url, but /check (with allow_redirects=False) returns Set-Cookie."""
        auth = WeiboAuthenticator()
        auth._last_check_data = {
            "retcode": 20000000,
            "data": {"status": 3},  # no url
        }
        fallback_resp = _mock_resp(
            status=302,
            set_cookie=["SUB=fallback_sub; Path=/; Domain=.weibo.com"],
        )
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=fallback_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            tokens = await auth.get_tokens("qr_abc")

        assert tokens.cookies["SUB"] == "fallback_sub"

    @pytest.mark.asyncio
    async def test_raises_on_missing_set_cookie_with_url(self):
        """url exists but login URL returns no Set-Cookie."""
        auth = WeiboAuthenticator()
        auth._last_check_data = {
            "retcode": 20000000,
            "data": {"url": "https://passport.weibo.com/sso/v2/login?alt=TEST"},
        }
        login_resp = _mock_resp(status=200, set_cookie=[])
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=login_resp)

        with (
            patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls,
            pytest.raises(RefreshFailedError, match="未获取到 Cookie"),
        ):
            mock_cls.return_value.__aenter__.return_value = mock_session
            await auth.get_tokens("qr_abc")


# ── WeiboAuthenticator.refresh_tokens ──────────────────────


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_keepalive_updates_expiry(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = MagicMock()
        mock_resp.headers.getall.return_value = [
            "SUB=new_sub; Path=/; Domain=.weibo.com",
            "SUBP=new_subp; Path=/; Domain=.weibo.com",
        ]
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
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

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
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

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_invalid_tokens_return_false(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_resp = MagicMock()
        mock_resp.status = 302  # Redirect = not logged in
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false(self):
        auth = WeiboAuthenticator()
        tokens = _sample_tokens()

        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=Exception("connection error"))

        with patch("platforms.weibo.auth.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
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
