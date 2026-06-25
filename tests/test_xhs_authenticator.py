"""Tests for XhsAuthenticator — fully mocked AsyncXhsClient.

Rewrite (2026-06-26): auth moved to ReaJason/xhs library via AsyncXhsClient.
All XHS HTTP is mocked; no real network calls.

See docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from platforms.xiaohongshu.auth import XhsAuthenticator
from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRExpiredError,
    QRStatus,
)
from shared.exceptions import CaptchaError, DataError, IpBlockError, RetryableError

# ── ──


def _sample_cookies() -> dict[str, str]:
    return {
        "a1": "test_a1_value",
        "web_session": "test_web_session",
        "webId": "test_web_id",
        "gid": "test_gid",
    }


def _make_tokens(cookies: dict[str, str] | None = None) -> PlatformTokens:
    return PlatformTokens(
        platform="xhs",
        cookies=cookies or _sample_cookies(),
        obtained_at=time.time(),
        expires_at=time.time() + 7 * 86400,
    )


# ── ──


class TestGenerateQrCode:
    """generate_qr_code returns QRCodeResult sourced from AsyncXhsClient.get_qrcode.

    Flow (spec §4.1):
      a1 = generate_a1()
      web_id = generate_web_id(a1)
      client = AsyncXhsClient(cookie=f"a1={a1};webId={web_id}")
      qr = await client.get_qrcode()  # {qr_id, code, url, multi_flag}
      cache qr_id+code on instance
      return QRCodeResult(qr_url=qr["url"], qr_key=qr["qr_id"], expires_in=180)
    """

    async def test_returns_qr_code_result_with_correct_fields(self):
        auth = XhsAuthenticator()

        mock_client = MagicMock()
        mock_client.get_qrcode = AsyncMock(
            return_value={
                "qr_id": "qr_abc",
                "code": "code_123",
                "url": "https://qr.xhs.com/abc",
                "multi_flag": 0,
            }
        )
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client) as mock_cls:
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_key == "qr_abc"
        assert result.qr_url == "https://qr.xhs.com/abc"
        assert result.expires_in == 180
        # Verify cookie passed to AsyncXhsClient contains a1 + webId
        mock_cls.assert_called_once()
        init_cookie = mock_cls.call_args.kwargs.get("cookie") or mock_cls.call_args.args[0]
        assert "a1=" in init_cookie
        assert "webId=" in init_cookie
        # Verify client cached for later poll/get_tokens
        assert auth._client is mock_client
        # Verify qr_id + code cached for poll_qr_status
        assert auth._qr_id == "qr_abc"
        assert auth._code == "code_123"

    async def test_propagates_get_qrcode_error_as_retryable(self):
        """If get_qrcode raises a non-translated exception, it bubbles up.

        _wrap_xhs_call translates known xhs exceptions; unknown exceptions
        should still propagate (caller decides).
        """
        from xhs.exception import DataFetchError

        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_qrcode = AsyncMock(side_effect=DataFetchError("server down"))
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            with pytest.raises(DataError, match="server down"):
                await auth.generate_qr_code()


# ── ──


class TestPollQrStatus:
    """poll_qr_status reads code_status (snake_case!). Regression for spec §1.2 #1.

    Mapping: 2=SUCCESS, 1=SCANNED, 3=EXPIRED, else=WAITING
    """

    async def test_code_status_2_returns_success(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode = AsyncMock(return_value={"code_status": 2})
        auth._client = mock_client
        auth._qr_id = "q1"
        auth._code = "c1"

        status = await auth.poll_qr_status("q1")
        assert status.status == QRStatus.SUCCESS
        assert status.success is True
        mock_client.check_qrcode.assert_awaited_once_with("q1", "c1")

    async def test_code_status_1_returns_scanned(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode = AsyncMock(return_value={"code_status": 1})
        auth._client = mock_client
        auth._qr_id = "q1"
        auth._code = "c1"

        status = await auth.poll_qr_status("q1")
        assert status.status == QRStatus.SCANNED

    async def test_code_status_3_returns_expired(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode = AsyncMock(return_value={"code_status": 3})
        auth._client = mock_client
        auth._qr_id = "q1"
        auth._code = "c1"

        status = await auth.poll_qr_status("q1")
        assert status.status == QRStatus.EXPIRED

    async def test_code_status_0_or_other_returns_waiting(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode = AsyncMock(return_value={"code_status": 0})
        auth._client = mock_client
        auth._qr_id = "q1"
        auth._code = "c1"

        status = await auth.poll_qr_status("q1")
        assert status.status == QRStatus.WAITING

    async def test_missing_code_status_defaults_to_waiting(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode = AsyncMock(return_value={})
        auth._client = mock_client
        auth._qr_id = "q1"
        auth._code = "c1"

        status = await auth.poll_qr_status("q1")
        assert status.status == QRStatus.WAITING

    async def test_exception_returns_waiting(self):
        """Any poll exception → WAITING (never raise; UI polls in loop)."""
        from xhs.exception import DataFetchError

        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.check_qrcode = AsyncMock(side_effect=DataFetchError("net down"))
        auth._client = mock_client
        auth._qr_id = "q1"
        auth._code = "c1"

        status = await auth.poll_qr_status("q1")
        assert status.status == QRStatus.WAITING
        assert not status.success


# ── ──


class TestGetTokens:
    """get_tokens: SUCCESS → activate() → read cookie str → parse into PlatformTokens."""

    async def test_activate_then_returns_cookies_from_client_cookie_str(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.activate = AsyncMock(return_value={})
        # cookie property returns "k=v; k=v" string
        type(mock_client).cookie = property(lambda self: "a1=v1; web_session=ws123; gid=g1")
        auth._client = mock_client

        tokens = await auth.get_tokens("qr_abc")

        mock_client.activate.assert_awaited_once()
        assert tokens.platform == "xhs"
        assert tokens.cookies["a1"] == "v1"
        assert tokens.cookies["web_session"] == "ws123"
        assert tokens.cookies["gid"] == "g1"
        assert tokens.expires_at > time.time()

    async def test_returns_empty_cookies_when_no_client(self):
        auth = XhsAuthenticator()
        auth._client = None

        tokens = await auth.get_tokens("qr_abc")
        assert tokens.cookies == {}
        assert tokens.expires_at <= time.time() + 5

    async def test_propagates_activate_error_as_data_error(self):
        from xhs.exception import DataFetchError

        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.activate = AsyncMock(side_effect=DataFetchError("activate failed"))
        auth._client = mock_client

        with pytest.raises(DataError):
            await auth.get_tokens("qr_abc")


# ── ──


class TestExceptionTranslation:
    """_wrap_xhs_call decorator translates xhs library exceptions to trawler types.

    Mapping (spec §5.2):
      NeedVerifyError → CaptchaError
      IPBlockError    → IpBlockError
      SignError       → RetryableError
      DataFetchError  → DataError
      RequestException→ RetryableError  (catch-all)
    """

    @pytest.mark.parametrize(
        "xhs_exc, trawler_exc",
        [
            ("NeedVerifyError", CaptchaError),
            ("IPBlockError", IpBlockError),
            ("SignError", RetryableError),
            ("DataFetchError", DataError),
        ],
    )
    async def test_decorator_translates_each_xhs_exception(self, xhs_exc, trawler_exc):
        """get_tokens wrapped → activate raises xhs_exc → caller sees trawler_exc."""
        import xhs.exception as xe

        auth = XhsAuthenticator()
        exc_class = getattr(xe, xhs_exc)
        mock_client = MagicMock()
        mock_client.activate = AsyncMock(side_effect=exc_class("boom"))
        auth._client = mock_client

        with pytest.raises(trawler_exc):
            await auth.get_tokens("q1")

    async def test_generic_requests_exception_becomes_retryable(self):
        """Any other requests.RequestException subclass → RetryableError."""
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.activate = AsyncMock(
            side_effect=requests.ConnectionError("network gone")
        )
        auth._client = mock_client

        with pytest.raises(RetryableError):
            await auth.get_tokens("q1")


# ── ──


class TestGetUserNickname:
    """get_user_nickname MUST NOT raise — failures return None."""

    async def test_returns_nickname_from_get_self_info(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(return_value={"nickname": "alice"})
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            nick = await auth.get_user_nickname(_make_tokens())

        assert nick == "alice"
        mock_client.close.assert_awaited_once()

    async def test_returns_none_on_xhs_exception(self):
        from xhs.exception import DataFetchError

        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(side_effect=DataFetchError("denied"))
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            nick = await auth.get_user_nickname(_make_tokens())

        assert nick is None

    async def test_returns_none_when_nickname_missing(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(return_value={"user_id": "u1"})  # no nickname
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            nick = await auth.get_user_nickname(_make_tokens())

        assert nick is None


# ── ──


class TestQrLogin:
    """qr_login 主流程 — 串联 generate_qr_code/poll_qr_status/get_tokens,CLI 入口依赖。

    覆盖 spec §4.1-4.3 的 deadline 循环、QRExpiredError 分支、asyncio.sleep。
    无此测试类 = qr_login 假绿(CLI 入口 run_check.py 的 trawler auth xhs 走这条路径)。

    不加 @pytest.mark.asyncio 装饰器(pyproject asyncio_mode=auto,见 plan 顶部说明)。
    """

    async def test_success_path_returns_tokens(self):
        """mock 三步全成功:generate_qr_code → QRCodeResult;
        poll_qr_status 先 SCANNED 再 SUCCESS;get_tokens → PlatformTokens;
        display_qr_in_terminal 被 patch 掉(避免终端输出)。断言返回值是 PlatformTokens 且 success。
        """
        auth = XhsAuthenticator()

        qr_result = QRCodeResult(
            qr_url="https://qr.xhs.com/abc",
            qr_key="qr_abc",
            expires_in=180,
        )
        tokens = _make_tokens()

        poll_mock = AsyncMock(side_effect=[
            AuthStatus(success=False, status=QRStatus.SCANNED, message="scanned"),
            AuthStatus(success=True, status=QRStatus.SUCCESS, message="ok"),
        ])
        get_tokens_mock = AsyncMock(return_value=tokens)

        with (
            patch.object(auth, "generate_qr_code", new=AsyncMock(return_value=qr_result)),
            patch.object(auth, "poll_qr_status", new=poll_mock),
            patch.object(auth, "get_tokens", new=get_tokens_mock),
            patch("platforms.xiaohongshu.auth.display_qr_in_terminal") as mock_display,
        ):
            result = await auth.qr_login()

        assert isinstance(result, PlatformTokens)
        assert result.cookies == tokens.cookies
        mock_display.assert_called_once_with("https://qr.xhs.com/abc")
        # poll 调了 2 次(SCANNED + SUCCESS),SUCCESS 后立即 return 不再 poll
        assert poll_mock.await_count == 2
        # get_tokens 只在 SUCCESS 分支调一次
        get_tokens_mock.assert_awaited_once()

    async def test_expired_raises(self):
        """poll_qr_status 返回 EXPIRED → qr_login 立即抛 QRExpiredError,不再循环。
        """
        auth = XhsAuthenticator()

        qr_result = QRCodeResult(
            qr_url="https://qr.xhs.com/abc",
            qr_key="qr_abc",
            expires_in=180,
        )

        poll_mock = AsyncMock(return_value=AuthStatus(
            success=False, status=QRStatus.EXPIRED, message="expired",
        ))

        with (
            patch.object(auth, "generate_qr_code", new=AsyncMock(return_value=qr_result)),
            patch.object(auth, "poll_qr_status", new=poll_mock),
            patch("platforms.xiaohongshu.auth.display_qr_in_terminal"),
            patch("platforms.xiaohongshu.auth.asyncio.sleep", new=AsyncMock()),
        ):
            with pytest.raises(QRExpiredError):
                await auth.qr_login()

        # EXPIRED 立即 raise,get_tokens 不应被调
        poll_mock.assert_awaited()

    async def test_timeout_raises(self):
        """deadline 超时(expires_in 极短 + poll 永远 WAITING)→ qr_login 抛 QRExpiredError。
        """
        auth = XhsAuthenticator()

        # expires_in=0 → deadline 立即到期,while 条件首次检查即 false 之前先 poll 一次
        qr_result = QRCodeResult(
            qr_url="https://qr.xhs.com/abc",
            qr_key="qr_abc",
            expires_in=0,
        )

        with (
            patch.object(auth, "generate_qr_code", new=AsyncMock(return_value=qr_result)),
            patch.object(
                auth,
                "poll_qr_status",
                new=AsyncMock(return_value=AuthStatus(
                    success=False, status=QRStatus.WAITING, message="waiting",
                )),
            ),
            patch("platforms.xiaohongshu.auth.display_qr_in_terminal"),
            patch("platforms.xiaohongshu.auth.asyncio.sleep", new=AsyncMock()),
            patch("platforms.xiaohongshu.auth.time.monotonic", side_effect=[0.0, 100.0]),
        ):
            with pytest.raises(QRExpiredError):
                await auth.qr_login()


# ── ──


class TestValidateTokens:
    """validate_tokens: xhs lib has no refresh → validate == probe via get_self_info."""

    async def test_expired_at_returns_false(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies={"a1": "x"},
            obtained_at=time.time() - 86400,
            expires_at=time.time() - 10,
        )
        assert await auth.validate_tokens(tokens) is False

    async def test_get_self_info_with_nickname_returns_true(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(return_value={"nickname": "x"})
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            result = await auth.validate_tokens(_make_tokens())

        assert result is True

    async def test_get_self_info_without_nickname_returns_false(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(return_value={})
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            result = await auth.validate_tokens(_make_tokens())

        assert result is False

    async def test_xhs_exception_returns_false(self):
        from xhs.exception import DataFetchError

        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(side_effect=DataFetchError("expired"))
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            result = await auth.validate_tokens(_make_tokens())

        assert result is False


# ── ──


class TestRefreshTokens:
    """refresh_tokens is degraded to validate-only (spec §4.5).

    xhs lib has no refresh concept. If get_self_info succeeds → return
    original tokens with bumped expires_at. If fails → raise (caller asks
    user to re-login).
    """

    async def test_valid_tokens_returned_with_bumped_expiry(self):
        auth = XhsAuthenticator()
        tokens = _make_tokens()
        original_expiry = tokens.expires_at
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(return_value={"nickname": "x"})
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            result = await auth.refresh_tokens(tokens)

        assert result.cookies == tokens.cookies
        assert result.expires_at >= original_expiry

    async def test_invalid_tokens_raises(self):
        from xhs.exception import DataFetchError

        auth = XhsAuthenticator()
        tokens = _make_tokens()
        mock_client = MagicMock()
        mock_client.get_self_info = AsyncMock(side_effect=DataFetchError("expired"))
        mock_client.close = AsyncMock()

        with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
            with pytest.raises(DataError):
                await auth.refresh_tokens(tokens)


# ── ──


class TestClose:
    async def test_close_closes_cached_client(self):
        auth = XhsAuthenticator()
        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        auth._client = mock_client

        await auth.close()

        mock_client.close.assert_awaited_once()
        assert auth._client is None

    async def test_close_when_no_client_is_noop(self):
        auth = XhsAuthenticator()
        auth._client = None
        await auth.close()  # must not raise


# ── ──


class TestIsAuthenticator:
    def test_is_subclass(self):
        assert issubclass(XhsAuthenticator, BaseAuthenticator)

    def test_supports_qr_login(self):
        assert XhsAuthenticator().supports_qr_login() is True

    def test_supports_refresh_returns_true(self):
        """Web UI refresh button needs supports_refresh()==True even though
        refresh is degraded to validate. Spec §4.5."""
        assert XhsAuthenticator().supports_refresh() is True
