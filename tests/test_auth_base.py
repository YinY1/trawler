from __future__ import annotations

import enum
import time
from unittest.mock import AsyncMock

import pytest

from shared.auth.base import (
    AuthError,
    AuthStatus,
    BaseAuthenticator,
    NetworkError,
    PlatformTokens,
    QRExpiredError,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
    TokenInvalidError,
)


class TestQRStatus:
    def test_is_str_enum(self) -> None:
        assert issubclass(QRStatus, enum.StrEnum)

    def test_has_five_members(self) -> None:
        assert len(QRStatus) == 5

    def test_waiting_value(self) -> None:
        assert QRStatus.WAITING == "waiting"

    def test_scanned_value(self) -> None:
        assert QRStatus.SCANNED == "scanned"

    def test_confirmed_value(self) -> None:
        assert QRStatus.CONFIRMED == "confirmed"

    def test_expired_value(self) -> None:
        assert QRStatus.EXPIRED == "expired"

    def test_success_value(self) -> None:
        assert QRStatus.SUCCESS == "success"

    def test_string_comparison(self) -> None:
        assert QRStatus.WAITING == "waiting"
        assert str(QRStatus.SCANNED) == "scanned"


class TestQRCodeResult:
    def test_creation_with_all_fields(self) -> None:
        result = QRCodeResult(qr_url="https://example.com/qr", qr_key="abc123", expires_in=300)
        assert result.qr_url == "https://example.com/qr"
        assert result.qr_key == "abc123"
        assert result.expires_in == 300

    def test_default_expires_in(self) -> None:
        result = QRCodeResult(qr_url="https://example.com/qr", qr_key="abc123")
        assert result.expires_in == 180

    def test_is_dataclass(self) -> None:
        from dataclasses import is_dataclass

        assert is_dataclass(QRCodeResult)


class TestAuthStatus:
    def test_creation_success(self) -> None:
        status = AuthStatus(success=True, status=QRStatus.SUCCESS, message="OK")
        assert status.success is True
        assert status.status == QRStatus.SUCCESS
        assert status.message == "OK"

    def test_creation_failure(self) -> None:
        status = AuthStatus(success=False, status=QRStatus.EXPIRED, message="QR expired")
        assert status.success is False
        assert status.status == QRStatus.EXPIRED
        assert status.message == "QR expired"

    def test_is_dataclass(self) -> None:
        from dataclasses import is_dataclass

        assert is_dataclass(AuthStatus)


class TestPlatformTokens:
    def test_creation(self) -> None:
        tokens = PlatformTokens(
            platform="bilibili",
            cookies={"session": "abc", "token": "xyz"},
            obtained_at=1000.0,
            expires_at=2000.0,
        )
        assert tokens.platform == "bilibili"
        assert tokens.cookies == {"session": "abc", "token": "xyz"}
        assert tokens.obtained_at == 1000.0
        assert tokens.expires_at == 2000.0

    def test_is_flat_dataclass(self) -> None:
        """PlatformTokens should be a plain dataclass, not inheriting from other dataclasses."""
        from dataclasses import is_dataclass

        assert is_dataclass(PlatformTokens)
        # Verify no non-standard bases (only object implicitly via dataclass)
        bases = PlatformTokens.__bases__
        assert bases == (object,)

    def test_empty_cookies(self) -> None:
        tokens = PlatformTokens(
            platform="test",
            cookies={},
            obtained_at=0.0,
            expires_at=0.0,
        )
        assert tokens.cookies == {}


class TestErrorHierarchy:
    @pytest.mark.parametrize(
        "error_cls",
        [QRExpiredError, NetworkError, TokenInvalidError, RefreshFailedError],
    )
    def test_specific_errors_are_subclass_of_auth_error(self, error_cls: type) -> None:
        assert issubclass(error_cls, AuthError)

    @pytest.mark.parametrize(
        "error_cls",
        [QRExpiredError, NetworkError, TokenInvalidError, RefreshFailedError],
    )
    def test_catchable_via_base_class(self, error_cls: type) -> None:
        with pytest.raises(AuthError):
            raise error_cls("test error")

    def test_auth_error_is_exception(self) -> None:
        assert issubclass(AuthError, Exception)

    def test_error_message_propagation(self) -> None:
        err = QRExpiredError("QR code has expired")
        assert str(err) == "QR code has expired"

    def test_raise_and_catch_specific(self) -> None:
        with pytest.raises(NetworkError):
            raise NetworkError("connection timeout")


class TestPackageExports:
    def test_all_exports_accessible_from_package(self) -> None:
        import shared.auth

        for name in shared.auth.__all__:
            assert hasattr(shared.auth, name), f"Missing export: {name}"

    def test_all_list_matches_actual_exports(self) -> None:
        import shared.auth

        expected = {
            "AuthError",
            "AuthStatus",
            "BaseAuthenticator",
            "NetworkError",
            "PlatformTokens",
            "QRExpiredError",
            "QRCodeResult",
            "QRStatus",
            "RefreshFailedError",
            "TokenInvalidError",
            "display_qr_in_terminal",
            "get_authenticator",
            "update_auth_section",
        }
        assert set(shared.auth.__all__) == expected


def _make_dummy_authenticator() -> type[BaseAuthenticator]:
    """Return a concrete subclass of BaseAuthenticator for testing."""

    class _DummyAuthenticator(BaseAuthenticator):
        generate_qr_code = AsyncMock()  # type: ignore[assignment]
        poll_qr_status = AsyncMock()  # type: ignore[assignment]
        get_tokens = AsyncMock()  # type: ignore[assignment]
        refresh_tokens = AsyncMock()  # type: ignore[assignment]
        validate_tokens = AsyncMock()  # type: ignore[assignment]

    return _DummyAuthenticator


class TestBaseAuthenticator:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            BaseAuthenticator()  # type: ignore[abstract]

    def test_supports_qr_login_default_true(self) -> None:
        cls = _make_dummy_authenticator()
        assert cls().supports_qr_login() is True

    def test_supports_refresh_default_false(self) -> None:
        cls = _make_dummy_authenticator()
        assert cls().supports_refresh() is False

    @pytest.mark.asyncio
    async def test_qr_login_success_flow(self) -> None:
        cls = _make_dummy_authenticator()
        auth = cls()

        qr_result = QRCodeResult(
            qr_url="https://example.com/qr", qr_key="key1", expires_in=180
        )
        auth.generate_qr_code = AsyncMock(return_value=qr_result)  # type: ignore[assignment]

        waiting_status = AuthStatus(
            success=False, status=QRStatus.WAITING, message="waiting"
        )
        success_status = AuthStatus(
            success=True, status=QRStatus.SUCCESS, message="OK"
        )
        auth.poll_qr_status = AsyncMock(  # type: ignore[assignment]
            side_effect=[waiting_status, success_status]
        )

        expected_tokens = PlatformTokens(
            platform="test",
            cookies={"session": "abc"},
            obtained_at=1000.0,
            expires_at=2000.0,
        )
        auth.get_tokens = AsyncMock(return_value=expected_tokens)  # type: ignore[assignment]

        result = await auth.qr_login()
        assert result is expected_tokens
        auth.get_tokens.assert_called_once_with("key1")

    @pytest.mark.asyncio
    async def test_qr_login_on_status_callback(self) -> None:
        cls = _make_dummy_authenticator()
        auth = cls()

        qr_result = QRCodeResult(
            qr_url="https://example.com/qr", qr_key="key1", expires_in=180
        )
        auth.generate_qr_code = AsyncMock(return_value=qr_result)  # type: ignore[assignment]

        waiting_status = AuthStatus(
            success=False, status=QRStatus.WAITING, message="waiting"
        )
        scanned_status = AuthStatus(
            success=False, status=QRStatus.SCANNED, message="scanned"
        )
        success_status = AuthStatus(
            success=True, status=QRStatus.SUCCESS, message="OK"
        )
        auth.poll_qr_status = AsyncMock(  # type: ignore[assignment]
            side_effect=[waiting_status, scanned_status, success_status]
        )

        expected_tokens = PlatformTokens(
            platform="test",
            cookies={"session": "abc"},
            obtained_at=1000.0,
            expires_at=2000.0,
        )
        auth.get_tokens = AsyncMock(return_value=expected_tokens)  # type: ignore[assignment]

        received: list[AuthStatus] = []
        await auth.qr_login(on_status=received.append)

        assert len(received) == 3
        assert received[0].status == QRStatus.WAITING
        assert received[1].status == QRStatus.SCANNED
        assert received[2].status == QRStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_qr_login_expired_raises(self) -> None:
        cls = _make_dummy_authenticator()
        auth = cls()

        qr_result = QRCodeResult(
            qr_url="https://example.com/qr", qr_key="key1", expires_in=180
        )
        auth.generate_qr_code = AsyncMock(return_value=qr_result)  # type: ignore[assignment]

        expired_status = AuthStatus(
            success=False, status=QRStatus.EXPIRED, message="expired"
        )
        auth.poll_qr_status = AsyncMock(return_value=expired_status)  # type: ignore[assignment]

        with pytest.raises(QRExpiredError, match="二维码已过期"):
            await auth.qr_login()

    @pytest.mark.asyncio
    async def test_qr_login_timeout_raises(self) -> None:
        cls = _make_dummy_authenticator()
        auth = cls()

        # Use a very short expiry to trigger timeout quickly
        qr_result = QRCodeResult(
            qr_url="https://example.com/qr", qr_key="key1", expires_in=0
        )
        auth.generate_qr_code = AsyncMock(return_value=qr_result)  # type: ignore[assignment]

        # Always return WAITING so the loop exhausts the deadline
        waiting_status = AuthStatus(
            success=False, status=QRStatus.WAITING, message="waiting"
        )
        auth.poll_qr_status = AsyncMock(return_value=waiting_status)  # type: ignore[assignment]

        # Speed up: mock asyncio.sleep to avoid actual waiting
        import shared.auth.base as base_mod

        original_sleep = base_mod.asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            # Advance monotonic time so the while loop terminates
            pass

        base_mod.asyncio.sleep = fake_sleep  # type: ignore[attr-defined]
        try:
            with pytest.raises(QRExpiredError, match="二维码轮询超时"):
                await auth.qr_login()
        finally:
            base_mod.asyncio.sleep = original_sleep  # type: ignore[attr-defined]

    def test_partial_subclass_cannot_instantiate(self) -> None:
        class PartialImpl(BaseAuthenticator):
            async def generate_qr_code(self) -> QRCodeResult:
                return QRCodeResult(qr_url="", qr_key="")

            # Missing the other 4 abstract methods

        with pytest.raises(TypeError):
            PartialImpl()  # type: ignore[abstract]
