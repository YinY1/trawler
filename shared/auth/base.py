from __future__ import annotations

import asyncio
import enum
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class QRStatus(enum.StrEnum):
    WAITING = "waiting"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    SUCCESS = "success"


class AuthError(Exception): ...


class QRExpiredError(AuthError): ...


class NetworkError(AuthError): ...


class TokenInvalidError(AuthError): ...


class RefreshFailedError(AuthError): ...


@dataclass
class QRCodeResult:
    qr_url: str
    qr_key: str
    expires_in: int = 180


@dataclass
class AuthStatus:
    success: bool
    status: QRStatus
    message: str


@dataclass
class PlatformTokens:
    platform: str
    cookies: dict[str, str]
    obtained_at: float
    expires_at: float


class BaseAuthenticator(ABC):
    @abstractmethod
    async def generate_qr_code(self) -> QRCodeResult: ...

    @abstractmethod
    async def poll_qr_status(self, qr_key: str) -> AuthStatus: ...

    @abstractmethod
    async def get_tokens(self, qr_key: str) -> PlatformTokens: ...

    @abstractmethod
    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens: ...

    @abstractmethod
    async def validate_tokens(self, tokens: PlatformTokens) -> bool: ...

    def supports_qr_login(self) -> bool:
        return True

    def supports_refresh(self) -> bool:
        return False

    @property
    def ac_time_value(self) -> str | None:
        """Platform-specific additional auth value (e.g. B站 ac_time_value). Returns None by default."""
        return None

    @staticmethod
    def build_tokens_from_config(config: Any) -> PlatformTokens | None:
        """Build PlatformTokens from platform config. Returns None if not configured."""
        return None

    async def qr_login(self, on_status: Callable[[AuthStatus], None] | None = None) -> PlatformTokens:
        # Lazy import to avoid circular dependency with qr_display
        from shared.auth.qr_display import display_qr_in_terminal

        qr = await self.generate_qr_code()
        display_qr_in_terminal(qr.qr_url)
        deadline = time.monotonic() + qr.expires_in
        while time.monotonic() < deadline:
            status = await self.poll_qr_status(qr.qr_key)
            if on_status is not None:
                on_status(status)
            if status.status == QRStatus.SUCCESS:
                return await self.get_tokens(qr.qr_key)
            if status.status == QRStatus.EXPIRED:
                raise QRExpiredError("二维码已过期")
            await asyncio.sleep(2)
        raise QRExpiredError("二维码轮询超时")
