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


class TestGenerateQrCodeApiError:
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
        mock_resp.headers = MagicMock()
        mock_resp.headers.get.return_value = None  # default
        mock_resp.headers.getall.return_value = [
            "SUB=fake_sub; Path=/; Domain=.weibo.com; HttpOnly",
            "SUBP=fake_subp; Path=/; Domain=.weibo.com",
            "WBPSESS=fake_wbpsess; Path=/; Domain=.weibo.com",
            "SSOLoginState=1735689600; Path=/; Domain=.weibo.com",
        ]

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
        mock_resp.headers = MagicMock()
        mock_resp.headers.getall.return_value = []  # No Set-Cookie

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
        mock_resp.headers = MagicMock()
        mock_resp.headers.getall.return_value = [
            "SUB=new_sub; Path=/; Domain=.weibo.com",
            "SUBP=new_subp; Path=/; Domain=.weibo.com",
        ]
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
