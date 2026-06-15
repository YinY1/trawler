"""Tests for platforms.xiaohongshu.client — XhsClient unified HTTP client."""

from __future__ import annotations

# pyright: basic
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from platforms.xiaohongshu.client import XhsClient

# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_session() -> MagicMock:
    """A fake aiohttp.ClientSession with async context manager support."""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.closed = False
    return session


@pytest.fixture
def client(mock_session: MagicMock) -> XhsClient:
    """XhsClient with a mock session injected."""
    return XhsClient(cookie="a1=abc123; webid=xyz", session=mock_session)  # type: ignore[arg-type]


def _mock_json_response(status: int, data: dict) -> MagicMock:
    """Build a mock aiohttp response with .json() returning data."""
    resp = MagicMock(spec=aiohttp.ClientResponse)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.headers = {}
    return resp


def _mock_raw_response(status: int, text: str = "", headers: dict | None = None) -> MagicMock:
    """Build a mock aiohttp response for raw text reads."""
    resp = MagicMock(spec=aiohttp.ClientResponse)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    # headers needs getall() method (aiohttp CIMultiDict-like)
    h = MagicMock()
    h.getall.side_effect = lambda key, default=None: [headers[key]] if headers and key in headers else default or []
    h.__getitem__.side_effect = lambda key: headers.get(key, "") if headers else ""
    resp.headers = h
    return resp


# ═══════════════════════════════════════════════════════════════
# Initialization
# ═══════════════════════════════════════════════════════════════


class TestInit:
    def test_accepts_str_cookie(self):
        c = XhsClient("a1=abc; webid=xyz")
        assert c._a1 == "abc"

    def test_accepts_dict_cookie(self):
        c = XhsClient({"a1": "abc", "webid": "xyz"})
        assert c._a1 == "abc"

    def test_creates_own_session_when_none(self):
        c = XhsClient("a1=abc")
        assert c._session is None  # lazy creation
        assert c._owns_session is True

    def test_uses_injected_session(self, mock_session):
        c = XhsClient("a1=abc", session=mock_session)  # type: ignore[arg-type]
        assert c._session is mock_session
        assert c._owns_session is False


# ═══════════════════════════════════════════════════════════════
# _request — core signing + error translation
# ═══════════════════════════════════════════════════════════════


class TestRequest:
    async def test_successful_post(self, client, mock_session):
        """POST: signs, sends, returns data dict."""
        resp = _mock_json_response(200, {"success": True, "data": {"notes": []}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full") as mock_sign:
            mock_sign.return_value = {"x-s": "s", "x-t": "t", "x-s-common": "c"}
            result = await client._request("POST", "/api/test", json={"x": 1})

        assert result == {"notes": []}
        mock_session.request.assert_called_once()
        args, kwargs = mock_session.request.call_args
        assert args[0] == "POST"
        assert kwargs["json"] == {"x": 1}
        # Verify signing was called with the right args
        mock_sign.assert_called_once()

    async def test_successful_get(self, client, mock_session):
        """GET: signs with params, returns data."""
        resp = _mock_json_response(200, {"success": True, "data": {"items": []}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full") as mock_sign:
            mock_sign.return_value = {"x-s": "s", "x-t": "t", "x-s-common": "c"}
            result = await client._request("GET", "/api/test", params={"num": "10"})

        assert result == {"items": []}
        args, kwargs = mock_session.request.call_args
        assert args[0] == "GET"
        # Query params are in the URL, not in kwargs["params"]
        assert "num=10" in args[1]

    async def test_sign_headers_merged_into_request(self, client, mock_session):
        """Signing headers are injected into the request."""
        resp = _mock_json_response(200, {"success": True, "data": {}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full") as mock_sign:
            mock_sign.return_value = {
                "x-s": "XYS_test",
                "x-t": "1700000000000",
                "x-s-common": "Cmn",
                "x-b3-traceid": "b3",
            }
            await client._request("GET", "/api/test")

        _, kwargs = mock_session.request.call_args
        assert kwargs["headers"]["x-s"] == "XYS_test"
        assert kwargs["headers"]["x-t"] == "1700000000000"
        assert kwargs["headers"]["x-s-common"] == "Cmn"
        assert kwargs["headers"]["x-b3-traceid"] == "b3"
        assert kwargs["headers"]["Cookie"] == "a1=abc123; webid=xyz"

    async def test_business_error_raises_data_error(self, client, mock_session):
        """200 OK but success=false → DataError."""
        resp = _mock_json_response(200, {"success": False, "msg": "invalid params"})
        mock_session.request.return_value = resp

        from shared.exceptions import DataError

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            with pytest.raises(DataError, match="invalid params"):
                await client._request("GET", "/api/test")

    async def test_http_4xx_raises_data_error(self, client, mock_session):
        """404 (non-special 4xx) → DataError."""
        resp = _mock_json_response(404, {"msg": "not found"})
        mock_session.request.return_value = resp

        from shared.exceptions import DataError

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            with pytest.raises(DataError, match="404"):
                await client._request("GET", "/api/test")

    async def test_http_5xx_raises_retryable_error(self, client, mock_session):
        """5xx → RetryableError."""
        resp = _mock_json_response(502, {"msg": "bad gateway"})
        mock_session.request.return_value = resp

        from shared.exceptions import RetryableError

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            with pytest.raises(RetryableError):
                await client._request("GET", "/api/test")

    async def test_http_429_raises_retryable_error(self, client, mock_session):
        """429 → RetryableError."""
        resp = _mock_json_response(429, {})
        mock_session.request.return_value = resp

        from shared.exceptions import RetryableError

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            with pytest.raises(RetryableError):
                await client._request("GET", "/api/test")

    async def test_captcha_error(self, client, mock_session):
        """461 → CaptchaError."""
        resp = _mock_json_response(461, {})
        mock_session.request.return_value = resp

        from shared.exceptions import CaptchaError

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            with pytest.raises(CaptchaError):
                await client._request("GET", "/api/test")

    async def test_ip_block_error(self, client, mock_session):
        """200 OK, success=true, but code=300012 → IpBlockError."""
        resp = _mock_json_response(200, {"success": True, "data": {}, "code": 300012})
        mock_session.request.return_value = resp

        from shared.exceptions import IpBlockError

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            with pytest.raises(IpBlockError):
                await client._request("GET", "/api/test")


# ═══════════════════════════════════════════════════════════════
# Typed methods
# ═══════════════════════════════════════════════════════════════


class TestSpecificMethods:
    async def test_get_user_notes(self, client, mock_session):
        resp = _mock_json_response(200, {"success": True, "data": {"notes": [{"id": "n1"}]}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.get_user_notes("uid123")

        assert result == [{"id": "n1"}]

    async def test_get_note_detail(self, client, mock_session):
        resp = _mock_json_response(200, {"success": True, "data": {"items": [{"note_card": {"title": "T"}}]}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.get_note_detail("nid", "token_x")

        assert result == {"title": "T"}

    async def test_get_comments(self, client, mock_session):
        resp = _mock_json_response(200, {"success": True, "data": {"comments": [{"id": "c1"}], "cursor": "c2"}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.get_comments("nid")

        assert result == {"comments": [{"id": "c1"}], "cursor": "c2"}

    async def test_get_user_info(self, client, mock_session):
        resp = _mock_json_response(200, {"success": True, "data": {"nickname": "User"}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.get_user_info()

        assert result == {"nickname": "User"}

    async def test_probe_success(self, client, mock_session):
        resp = _mock_json_response(200, {"success": True, "data": {"nickname": "User"}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            assert await client.probe() is True

    async def test_probe_failure(self, client, mock_session):
        resp = _mock_json_response(200, {"success": False})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            assert await client.probe() is False

    async def test_probe_http_error(self, client, mock_session):
        resp = _mock_json_response(500, {})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            assert await client.probe() is False

    async def test_create_qrcode(self, client, mock_session):
        resp = _mock_json_response(
            200, {"success": True, "data": {"qr_id": "q1", "qr_url": "https://xhs.cn/q", "code": "c"}}
        )
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.create_qrcode({"a1": "init"})

        assert result["qr_id"] == "q1"

    async def test_check_qrcode_status(self, client, mock_session):
        resp = _mock_json_response(200, {"success": True, "data": {"status": 3}})
        mock_session.request.return_value = resp

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.check_qrcode_status("q1", "c")

        assert result["status"] == 3

    async def test_fetch_sec_cookies_both_succeed(self, client, mock_session):
        """Two POSTs to sec endpoints return sec_poison_id and gid."""
        resp1 = _mock_json_response(200, {"success": True, "data": {"secPoisonId": "spid1"}})
        resp2 = _mock_raw_response(200, headers={"Set-Cookie": "gid=gid_val"})
        resp2.json = AsyncMock(return_value={"success": True, "data": {}})

        mock_session.request.side_effect = [resp1, resp2]

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.fetch_sec_cookies({"a1": "abc"})

        assert result.get("sec_poison_id") == "spid1"
        assert result.get("gid") == "gid_val"

    async def test_refresh_cookies_returns_updated(self, client, mock_session):
        mock_session.get.return_value = _mock_raw_response(
            200,
            headers={
                "Set-Cookie": "a1=new_val; Path=/",
                "Content-Type": "text/html",
            },
        )

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.refresh_cookies()

        assert result is not None
        assert result["a1"] == "new_val"

    async def test_refresh_cookies_no_set_cookie(self, client, mock_session):
        mock_session.get.return_value = _mock_raw_response(200)

        with patch("platforms.xiaohongshu.client.get_xhs_sign_full"):
            result = await client.refresh_cookies()

        assert result is None


# ═══════════════════════════════════════════════════════════════
# close()
# ═══════════════════════════════════════════════════════════════


class TestClose:
    async def test_close_owned_session(self):
        c = XhsClient("a1=abc")
        c._session = MagicMock(spec=aiohttp.ClientSession)
        c._session.closed = False
        await c.close()
        c._session.close.assert_awaited_once()

    async def test_close_does_not_close_injected_session(self, mock_session):
        c = XhsClient("a1=abc", session=mock_session)  # type: ignore[arg-type]
        c._session = mock_session
        await c.close()
        mock_session.close.assert_not_called()
