"""Tests for XhsAuthenticator — fully mocked, no real XHS API calls."""

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
    RefreshFailedError,
)

# ── ──


def _make_mock_response(status: int = 200, json_data: dict | None = None) -> MagicMock:
    """Create a MagicMock that works as an async context manager for aiohttp responses."""
    resp = MagicMock()
    resp.status = status
    resp.cookies = MagicMock()
    resp.cookies.items.return_value = []
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    return resp


def _sample_cookies() -> dict[str, str]:
    return {
        "a1": "test_a1_value",
        "web_session": "test_web_session",
        "webId": "test_web_id",
        "gid": "test_gid",
    }


def _cookie_str() -> str:
    return "; ".join(f"{k}={v}" for k, v in _sample_cookies().items())


# ── ──


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        auth = XhsAuthenticator()

        mock_resp = _make_mock_response(
            status=200,
            json_data={"success": True, "data": {"qr_id": "qr_abc", "code": "code_123", "url": "https://qr.xhs.com/abc"}},
        )

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        _sec = {"sec_poison_id": "s1", "gid": "g1"}

        with (
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
            patch("platforms.xiaohongshu.auth.generate_a1", return_value="test_a1"),
            patch("platforms.xiaohongshu.auth.generate_web_id", return_value="test_web_id"),
            patch("platforms.xiaohongshu.auth._fetch_sec_cookies", new_callable=AsyncMock, return_value=_sec),
        ):
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_key == "qr_abc"
        assert result.qr_url == "https://qr.xhs.com/abc"

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self):
        auth = XhsAuthenticator()

        mock_resp = _make_mock_response(
            status=200,
            json_data={"success": False, "msg": "rate limited"},
        )

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        _sec = {"sec_poison_id": "s1", "gid": "g1"}

        with (
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
            patch("platforms.xiaohongshu.auth.generate_a1", return_value="test_a1"),
            patch("platforms.xiaohongshu.auth.generate_web_id", return_value="test_web_id"),
            patch("platforms.xiaohongshu.auth._fetch_sec_cookies", new_callable=AsyncMock, return_value=_sec),
        ):
            with pytest.raises(RuntimeError, match="rate limited"):
                await auth.generate_qr_code()


# ── ──


class TestPollQrStatus:
    @pytest.mark.asyncio
    async def test_waiting(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_resp = _make_mock_response(
            status=200,
            json_data={"data": {"codeStatus": 0}},
        )

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        with (
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_scanned(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_resp = _make_mock_response(
            status=200,
            json_data={"data": {"codeStatus": 1}},
        )

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        with (
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SCANNED
        assert not status.success

    @pytest.mark.asyncio
    async def test_success(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        # First call: poll status returns codeStatus=2
        mock_resp1 = _make_mock_response(
            status=200,
            json_data={"data": {"codeStatus": 2}},
        )

        # Second call: login/qrcode/status returns web_session
        mock_resp2 = _make_mock_response(
            status=200,
            json_data={"success": True, "data": {"login_info": {"session": "ws_session"}}},
        )

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp1)
        mock_session.get = MagicMock(return_value=mock_resp2)
        auth._session = mock_session

        with (
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SUCCESS
        assert status.success
        assert "ws_session" in auth._init_cookies.get("web_session", "")

    @pytest.mark.asyncio
    async def test_expired(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_resp = _make_mock_response(
            status=200,
            json_data={"data": {"codeStatus": 3}},
        )

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        with (
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.EXPIRED
        assert not status.success


# ── ──


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1", "web_session": "ws", "webId": "wid", "gid": "gid1"}
        auth._qr_code = "code_123"

        mock_session = MagicMock()
        auth._session = mock_session

        tokens = await auth.get_tokens("qr_abc")

        assert tokens.platform == "xhs"
        assert tokens.cookies["a1"] == "test_a1"
        assert tokens.cookies["web_session"] == "ws"
        assert tokens.expires_at > time.time()

    @pytest.mark.asyncio
    async def test_raises_without_session(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_session = MagicMock()
        auth._session = mock_session

        with pytest.raises(RefreshFailedError, match="web_session"):
            await auth.get_tokens("qr_abc")


# ── ──


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_keepalive_updates_expiry(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_resp = _make_mock_response(status=200)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        result = await auth.refresh_tokens(tokens)
        assert result.expires_at > tokens.expires_at
        # Cookies preserved
        assert result.cookies["a1"] == "test_a1_value"

    @pytest.mark.asyncio
    async def test_keepalive_failure_returns_original(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=Exception("network error"))
        auth._session = mock_session

        result = await auth.refresh_tokens(tokens)
        assert result is tokens


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
    async def test_valid_returns_true(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_resp = _make_mock_response(status=200)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        auth._session = mock_session

        assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_redirect_returns_false(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=MagicMock(status=302))
        auth._session = mock_session

        assert await auth.validate_tokens(tokens) is False


# ── ──


class TestIsAuthenticator:
    def test_is_subclass(self):
        assert issubclass(XhsAuthenticator, BaseAuthenticator)

    def test_supports_qr_login(self):
        assert XhsAuthenticator().supports_qr_login() is True

    def test_supports_refresh(self):
        assert XhsAuthenticator().supports_refresh() is True
