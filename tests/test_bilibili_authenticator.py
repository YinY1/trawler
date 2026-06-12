"""Tests for BilibiliAuthenticator — fully mocked, no real B站 API calls."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.bilibili.auth import BilibiliAuthenticator, get_credential
from shared.auth.base import (
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)
from shared.config import BilibiliAuth, BilibiliConfig, Config

# ── Fixtures ──────────────────────────────────────────────────


def _make_config(**auth_overrides) -> Config:
    cfg = Config()
    auth = BilibiliAuth(**auth_overrides)
    cfg.bilibili = BilibiliConfig(auth=auth)
    return cfg


def _sample_tokens(**cookie_overrides) -> PlatformTokens:
    now = time.time()
    cookies = {
        "SESSDATA": "fake_sess",
        "bili_jct": "fake_jct",
        "DedeUserID": "12345",
        "buvid3": "fake_buvid3",
    }
    cookies.update(cookie_overrides)
    return PlatformTokens(
        platform="bilibili",
        cookies=cookies,
        obtained_at=now,
        expires_at=now + 180 * 86400,
    )


# ── get_credential ────────────────────────────────────────────


class TestGetCredential:
    def test_returns_credential_when_configured(self):
        cfg = _make_config(sessdata="s", bili_jct="j", buvid3="b3", dedeuserid="du")
        cred = get_credential(cfg)
        assert cred.sessdata == "s"
        assert cred.bili_jct == "j"
        assert cred.buvid3 == "b3"
        assert cred.dedeuserid == "du"

    def test_returns_empty_credential_when_not_configured(self):
        cfg = _make_config()
        cred = get_credential(cfg)
        # bilibili_api.Credential() defaults to None for fields
        assert cred.sessdata is None or cred.sessdata == ""


# ── BilibiliAuthenticator.generate_qr_code ────────────────────


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.get_qrcode_terminal.return_value = "QR-TERMINAL-STR"
        mock_qr._QrCodeLogin__qr_key = "key123"
        mock_qr.generate_qrcode = AsyncMock()

        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_url == "QR-TERMINAL-STR"
        assert result.qr_key == "key123"
        assert result.expires_in == 180


# ── BilibiliAuthenticator.poll_qr_status ──────────────────────


class TestPollQrStatus:
    @pytest.mark.asyncio
    async def test_waiting(self):
        from bilibili_api import login_v2

        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.check_state = AsyncMock(return_value=login_v2.QrCodeLoginEvents.SCAN)
        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            status = await auth.poll_qr_status("k")
        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_scanned(self):
        from bilibili_api import login_v2

        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.check_state = AsyncMock(return_value=login_v2.QrCodeLoginEvents.CONF)
        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            status = await auth.poll_qr_status("k")
        assert status.status == QRStatus.SCANNED
        assert not status.success

    @pytest.mark.asyncio
    async def test_success(self):
        from bilibili_api import login_v2

        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.check_state = AsyncMock(return_value=login_v2.QrCodeLoginEvents.DONE)
        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            status = await auth.poll_qr_status("k")
        assert status.status == QRStatus.SUCCESS
        assert status.success

    @pytest.mark.asyncio
    async def test_expired(self):
        from bilibili_api import login_v2

        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.check_state = AsyncMock(return_value=login_v2.QrCodeLoginEvents.TIMEOUT)
        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            status = await auth.poll_qr_status("k")
        assert status.status == QRStatus.EXPIRED
        assert not status.success


# ── BilibiliAuthenticator.get_tokens ──────────────────────────


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens_and_stores_ac_time(self):
        from bilibili_api import login_v2

        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.check_state = AsyncMock(return_value=login_v2.QrCodeLoginEvents.DONE)
        mock_cred = MagicMock()
        mock_cred.sessdata = "SD"
        mock_cred.bili_jct = "BJ"
        mock_cred.dedeuserid = "DUID"
        mock_cred.buvid3 = "BV3"
        mock_cred.ac_time_value = "ac123"
        mock_qr.get_credential.return_value = mock_cred

        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            tokens = await auth.get_tokens("k")

        assert tokens.platform == "bilibili"
        assert tokens.cookies["SESSDATA"] == "SD"
        assert tokens.cookies["bili_jct"] == "BJ"
        assert tokens.cookies["DedeUserID"] == "DUID"
        assert tokens.cookies["buvid3"] == "BV3"
        assert auth._last_ac_time_value == "ac123"

    @pytest.mark.asyncio
    async def test_raises_when_not_done(self):
        from bilibili_api import login_v2

        auth = BilibiliAuthenticator()
        mock_qr = MagicMock()
        mock_qr.check_state = AsyncMock(return_value=login_v2.QrCodeLoginEvents.SCAN)
        with patch.object(auth, "_get_qr_login", return_value=mock_qr):
            with pytest.raises(RefreshFailedError):
                await auth.get_tokens("k")


# ── BilibiliAuthenticator.refresh_tokens ──────────────────────


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_refresh_success(self):
        auth = BilibiliAuthenticator(config_path="/tmp/nonexistent.toml")

        tokens = _sample_tokens()

        # Mock load_config to return config with ac_time_value
        mock_cfg = _make_config(ac_time_value="ac_old")
        mock_cred = MagicMock()
        mock_cred.check_refresh = AsyncMock(return_value=True)
        mock_cred.refresh = AsyncMock()
        # After refresh, credential is mutated in-place
        mock_cred.sessdata = "new_sess"
        mock_cred.bili_jct = "new_jct"
        mock_cred.dedeuserid = "new_duid"
        mock_cred.buvid3 = "new_bv3"
        mock_cred.ac_time_value = "ac_new"

        with (
            patch("shared.config.load_config", return_value=mock_cfg),
            patch("platforms.bilibili.auth.bilibili_api.Credential", return_value=mock_cred),
        ):
            result = await auth.refresh_tokens(tokens)

        assert result.cookies["SESSDATA"] == "new_sess"
        assert result.cookies["bili_jct"] == "new_jct"
        assert result.cookies["DedeUserID"] == "new_duid"
        assert result.cookies["buvid3"] == "new_bv3"
        assert auth._last_ac_time_value == "ac_new"

    @pytest.mark.asyncio
    async def test_no_refresh_needed(self):
        auth = BilibiliAuthenticator(config_path="/tmp/nonexistent.toml")
        tokens = _sample_tokens()

        mock_cfg = _make_config(ac_time_value="ac_old")
        mock_cred = MagicMock()
        mock_cred.check_refresh = AsyncMock(return_value=False)

        with (
            patch("shared.config.load_config", return_value=mock_cfg),
            patch("platforms.bilibili.auth.bilibili_api.Credential", return_value=mock_cred),
        ):
            result = await auth.refresh_tokens(tokens)

        assert result is tokens  # same object returned

    @pytest.mark.asyncio
    async def test_raises_without_ac_time_value(self):
        auth = BilibiliAuthenticator(config_path="/tmp/nonexistent.toml")
        tokens = _sample_tokens()

        mock_cfg = _make_config()  # no ac_time_value

        with patch("shared.config.load_config", return_value=mock_cfg):
            with pytest.raises(RefreshFailedError, match="ac_time_value"):
                await auth.refresh_tokens(tokens)


# ── BilibiliAuthenticator.validate_tokens ─────────────────────


class TestValidateTokens:
    @pytest.mark.asyncio
    async def test_expired_tokens_return_false(self):
        auth = BilibiliAuthenticator()
        tokens = PlatformTokens(
            platform="bilibili",
            cookies={"SESSDATA": "x"},
            obtained_at=time.time() - 200 * 86400,
            expires_at=time.time() - 10,  # expired
        )
        assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_valid_tokens_return_true(self):
        auth = BilibiliAuthenticator()
        tokens = _sample_tokens()

        mock_cred = MagicMock()
        mock_cred.check_valid = AsyncMock(return_value=True)

        with patch("platforms.bilibili.auth.bilibili_api.Credential", return_value=mock_cred):
            assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_invalid_tokens_return_false(self):
        auth = BilibiliAuthenticator()
        tokens = _sample_tokens()

        mock_cred = MagicMock()
        mock_cred.check_valid = AsyncMock(side_effect=Exception("fail"))

        with patch("platforms.bilibili.auth.bilibili_api.Credential", return_value=mock_cred):
            assert await auth.validate_tokens(tokens) is False


# ── BilibiliAuthenticator.supports_refresh ────────────────────


class TestSupportsRefresh:
    def test_returns_true(self):
        auth = BilibiliAuthenticator()
        assert auth.supports_refresh() is True
