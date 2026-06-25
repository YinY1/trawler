"""Tests for XhsAuthenticator — fully mocked XhsClient, no real XHS API calls.

P4-1 rewrite: vendor helpers replaced by XhsClient method mocking.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.auth import XhsAuthenticator
from shared.auth.base import (
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
)

# ── ──


def _sample_cookies() -> dict[str, str]:
    return {
        "a1": "test_a1_value",
        "web_session": "test_web_session",
        "webId": "test_web_id",
        "gid": "test_gid",
    }


# ── ──


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        """generate_qr_code returns QRCodeResult with qr_url/qr_key/expires_in."""
        auth = XhsAuthenticator()

        mock_client = MagicMock()
        mock_client.fetch_sec_cookies = AsyncMock(return_value={"sec_poison_id": "sec1", "gid": "gid1"})
        mock_client.create_qrcode = AsyncMock(
            return_value={
                "qr_id": "qr_abc",
                "url": "https://qr.xhs.com/abc",
                "code": "code_123",
            }
        )

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_key == "qr_abc"
        # QRCodeResult.qr_url must be sourced from the server's ``url`` field,
        # NOT ``qr_url`` (which does not exist in real XHS responses).
        assert result.qr_url == "https://qr.xhs.com/abc"
        assert result.expires_in == 180
        # init cookies captured for later polling
        assert auth._init_cookies.get("a1")
        assert auth._init_cookies.get("sec_poison_id") == "sec1"
        assert auth._qr_code == "code_123"

    @pytest.mark.asyncio
    async def test_qr_url_field_must_be_url_not_qr_url(self):
        """Regression: server returns ``url`` only; ``qr_url`` MUST NOT be required.

        Original bug (PR #13): auth.py read ``qr_data["qr_url"]`` but the real
        XHS ``qrcode/create`` response uses ``url`` (verified against
        ReaJason/xhs core.py docstring + project phase-3 captured payload).
        The mock below mirrors the real shape (only ``url``, no ``qr_url``);
        generate_qr_code must not raise KeyError.
        """
        auth = XhsAuthenticator()

        mock_client = MagicMock()
        mock_client.fetch_sec_cookies = AsyncMock(return_value={})
        mock_client.create_qrcode = AsyncMock(
            return_value={
                "qr_id": "qr_xyz",
                "url": "xhsdiscover://login?qr_id=qr_xyz",
                "code": "c9",
            }
        )

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            result = await auth.generate_qr_code()

        assert result.qr_url == "xhsdiscover://login?qr_id=qr_xyz"
        assert result.qr_key == "qr_xyz"

    @pytest.mark.asyncio
    async def test_propagates_create_qrcode_error(self):
        """If create_qrcode raises, generate_qr_code propagates the error."""
        auth = XhsAuthenticator()

        mock_client = MagicMock()
        mock_client.fetch_sec_cookies = AsyncMock(return_value={})
        mock_client.create_qrcode = AsyncMock(side_effect=RuntimeError("rate limited"))

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            with pytest.raises(RuntimeError, match="rate limited"):
                await auth.generate_qr_code()


# ── ──


class TestPollQrStatus:
    @pytest.mark.asyncio
    async def test_waiting_status_code_1(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode_status = AsyncMock(return_value={"status": 1})
        auth._client = mock_client

        status = await auth.poll_qr_status("qr_abc")
        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_scanned_status_code_2(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode_status = AsyncMock(return_value={"status": 2})
        auth._client = mock_client

        status = await auth.poll_qr_status("qr_abc")
        assert status.status == QRStatus.SCANNED
        assert not status.success

    @pytest.mark.asyncio
    async def test_success_status_code_3(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode_status = AsyncMock(return_value={"status": 3})
        auth._client = mock_client

        status = await auth.poll_qr_status("qr_abc")
        assert status.status == QRStatus.SUCCESS
        assert status.success

    @pytest.mark.asyncio
    async def test_expired_status_code_4(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode_status = AsyncMock(return_value={"status": 4})
        auth._client = mock_client

        status = await auth.poll_qr_status("qr_abc")
        assert status.status == QRStatus.EXPIRED
        assert not status.success

    @pytest.mark.asyncio
    async def test_default_status_when_missing(self):
        """Missing 'status' field defaults to WAITING (code 1)."""
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode_status = AsyncMock(return_value={})
        auth._client = mock_client

        status = await auth.poll_qr_status("qr_abc")
        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_poll_exception_returns_waiting(self):
        """Network/poll errors return a WAITING AuthStatus, not raise."""
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode_status = AsyncMock(side_effect=RuntimeError("network down"))
        auth._client = mock_client

        status = await auth.poll_qr_status("qr_abc")
        assert status.status == QRStatus.WAITING
        assert not status.success
        assert "network down" in status.message


# ── ──


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens_from_client(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.cookies = {"a1": "v1", "web_session": "ws", "gid": "g"}
        auth._client = mock_client

        tokens = await auth.get_tokens("qr_abc")

        assert tokens.platform == "xhs"
        assert tokens.cookies["a1"] == "v1"
        assert tokens.cookies["web_session"] == "ws"
        assert tokens.expires_at > time.time()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_client(self):
        """When client is None (init never happened), return empty cookies."""
        auth = XhsAuthenticator()
        auth._client = None

        tokens = await auth.get_tokens("qr_abc")
        assert tokens.cookies == {}
        assert tokens.expires_at <= time.time() + 5

    @pytest.mark.asyncio
    async def test_drops_empty_cookie_values(self):
        """Empty string values are filtered out."""
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.cookies = {"a1": "v1", "empty": "", "gid": "g"}
        auth._client = mock_client

        tokens = await auth.get_tokens("qr_abc")
        assert "empty" not in tokens.cookies
        assert tokens.cookies["a1"] == "v1"


# ── ──


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_updates_cookies_and_expiry(self):
        """refresh_cookies returns new cookies: merged + expires_at bumped."""
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        mock_client.refresh_cookies = AsyncMock(return_value={"a1": "new_a1", "web_session": "new_ws"})
        auth._client = mock_client

        # _ensure_client(cookie) should create a new XhsClient — patch the class
        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            result = await auth.refresh_tokens(tokens)

        assert result.expires_at >= tokens.expires_at
        assert result.cookies["a1"] == "new_a1"
        assert result.cookies["web_session"] == "new_ws"
        # Other cookies preserved
        assert result.cookies["gid"] == "test_gid"

    @pytest.mark.asyncio
    async def test_bumps_expiry_when_no_new_cookies(self):
        """refresh_cookies returns None: cookies preserved, expires_at bumped."""
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 100,
        )

        mock_client = MagicMock()
        mock_client.refresh_cookies = AsyncMock(return_value=None)
        mock_client.close = AsyncMock()
        auth._client = mock_client

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            result = await auth.refresh_tokens(tokens)

        # AC: cookies may differ OR expires_at bumped
        assert result.expires_at >= tokens.expires_at
        assert result.cookies == tokens.cookies


# ── ──


class TestValidateTokens:
    @pytest.mark.asyncio
    async def test_expired_returns_false(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies={"a1": "x"},
            obtained_at=time.time() - 86400,
            expires_at=time.time() - 10,
        )
        assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_probe_true_returns_true(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_client = MagicMock()
        mock_client.probe = AsyncMock(return_value=True)
        mock_client.close = AsyncMock()
        auth._client = mock_client

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_probe_false_returns_false(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_client = MagicMock()
        mock_client.probe = AsyncMock(return_value=False)
        mock_client.close = AsyncMock()
        auth._client = mock_client

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_probe_exception_returns_false(self):
        """probe() raising should be swallowed, returning False."""
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_client = MagicMock()
        mock_client.probe = AsyncMock(side_effect=RuntimeError("boom"))
        mock_client.close = AsyncMock()
        auth._client = mock_client

        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=mock_client):
            assert await auth.validate_tokens(tokens) is False


# ── ──


class TestEnsureClientClosesOldClient:
    """Regression: _ensure_client(cookie) must close the previous client's
    aiohttp session to avoid 'Unclosed client session' warnings.

    Root cause: refresh_tokens and validate_tokens call _ensure_client with a
    non-empty cookie, which replaced self._client without closing the old one.
    """

    @pytest.mark.asyncio
    async def test_replacing_cookie_closes_old_client(self):
        """Passing a non-empty cookie must await close() on the old client."""
        auth = XhsAuthenticator()
        old_client = MagicMock()
        old_client.close = AsyncMock()
        auth._client = old_client

        new_client = MagicMock()
        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=new_client):
            await auth._ensure_client(cookie="a1=new; web_session=fresh")

        # AC: old client's close was awaited exactly once
        old_client.close.assert_awaited_once()
        # AC: self._client now points to the new client
        assert auth._client is new_client

    @pytest.mark.asyncio
    async def test_no_cookie_does_not_close_existing_client(self):
        """Calling _ensure_client() without cookie must keep the current client."""
        auth = XhsAuthenticator()
        existing = MagicMock()
        existing.close = AsyncMock()
        auth._client = existing

        returned = await auth._ensure_client()

        existing.close.assert_not_awaited()
        assert returned is existing
        assert auth._client is existing

    @pytest.mark.asyncio
    async def test_first_call_with_cookie_does_not_close(self):
        """When self._client is None, no close attempt is made on None."""
        auth = XhsAuthenticator()
        new_client = MagicMock()
        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=new_client):
            await auth._ensure_client(cookie="a1=fresh")
        # AC: no exception raised, client assigned
        assert auth._client is new_client

    @pytest.mark.asyncio
    async def test_refresh_tokens_does_not_leak_old_client(self):
        """End-to-end: refresh_tokens replacing cookie should close prior client."""
        auth = XhsAuthenticator()
        old_client = MagicMock()
        old_client.close = AsyncMock()
        old_client.refresh_cookies = AsyncMock(return_value={"a1": "new"})
        auth._client = old_client

        new_client = MagicMock()
        new_client.refresh_cookies = AsyncMock(return_value={"a1": "new_a1"})
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )
        with patch("platforms.xiaohongshu.auth.XhsClient", return_value=new_client):
            await auth.refresh_tokens(tokens)

        old_client.close.assert_awaited_once()


# ── ──


class TestIsAuthenticator:
    def test_is_subclass(self):
        assert issubclass(XhsAuthenticator, BaseAuthenticator)

    def test_supports_qr_login(self):
        assert XhsAuthenticator().supports_qr_login() is True

    def test_supports_refresh(self):
        assert XhsAuthenticator().supports_refresh() is True
