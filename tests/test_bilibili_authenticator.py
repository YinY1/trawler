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
        "sessdata": "fake_sess",
        "bili_jct": "fake_jct",
        "dedeuserid": "12345",
        "buvid3": "fake_buvid3",
    }
    cookies.update(cookie_overrides)
    return PlatformTokens(
        platform="bilibili",
        cookies=cookies,
        obtained_at=now,
        expires_at=now + 180 * 86400,
    )


def _mock_aiohttp_response(json_data: dict, set_cookies: list[str] | None = None) -> MagicMock:
    """创建一个模拟的 aiohttp ClientResponse，支持 async with 和 .json()。"""
    resp = MagicMock()
    resp.json = AsyncMock(return_value=json_data)

    # 模拟 headers.getall("set-cookie")
    headers_mock = MagicMock()
    headers_mock.getall = MagicMock(return_value=set_cookies or [])
    resp.headers = headers_mock

    # async with session.get(...) as resp: 需要 __aenter__
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session() -> MagicMock:
    """创建一个模拟的 aiohttp ClientSession。"""
    session = MagicMock()
    session.get = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


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
        assert cred.sessdata is None or cred.sessdata == ""


# ── BilibiliAuthenticator.generate_qr_code ────────────────────


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        auth = BilibiliAuthenticator()
        session = _mock_session()
        resp = _mock_aiohttp_response({
            "data": {
                "url": "https://scan.example.com/qr?key=abc",
                "qrcode_key": "abc123",
            }
        })
        session.get.return_value = resp

        with patch("shared.http.get_session", AsyncMock(return_value=session)):
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_url == "https://scan.example.com/qr?key=abc"
        assert result.qr_key == "abc123"
        assert result.expires_in == 180
        session.get.assert_called_once()


# ── BilibiliAuthenticator.poll_qr_status ──────────────────────


def _poll_test_helper(code: int, expected_status: QRStatus, expected_success: bool):
    """Helper: mock a poll response and check the returned AuthStatus."""

    async def run():
        auth = BilibiliAuthenticator()
        session = _mock_session()

        body = {"data": {"code": code, "message": "test"}}
        if code == 0:
            body["data"]["url"] = "https://redirect/url"
            body["data"]["refresh_token"] = "rt_abc"

        resp = _mock_aiohttp_response(body, set_cookies=["SESSDATA=v; Path=/"])
        session.get.return_value = resp

        with patch("shared.http.get_session", AsyncMock(return_value=session)):
            status = await auth.poll_qr_status("k")

        assert status.status == expected_status
        assert status.success == expected_success
        return auth

    return run()


class TestPollQrStatus:
    @pytest.mark.asyncio
    async def test_waiting(self):
        await _poll_test_helper(86101, QRStatus.WAITING, False)

    @pytest.mark.asyncio
    async def test_scanned(self):
        await _poll_test_helper(86090, QRStatus.SCANNED, False)

    @pytest.mark.asyncio
    async def test_success(self):
        auth = await _poll_test_helper(0, QRStatus.SUCCESS, True)
        assert auth._refresh_token == "rt_abc"
        assert auth._saved_cookies.get("SESSDATA") == "v"

    @pytest.mark.asyncio
    async def test_expired(self):
        await _poll_test_helper(86038, QRStatus.EXPIRED, False)


# ── BilibiliAuthenticator.get_tokens ──────────────────────────


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens_and_stores_ac_time(self):
        auth = BilibiliAuthenticator()
        # 模拟 poll 阶段填入的 _saved_cookies
        auth._saved_cookies = {
            "SESSDATA": "sd_val",
            "bili_jct": "bj_val",
            "DedeUserID": "duid_val",
            "sid": "sid_val",
        }
        auth._refresh_token = "rt_abc"

        tokens = await auth.get_tokens("k")

        assert tokens.platform == "bilibili"
        assert tokens.cookies["sessdata"] == "sd_val"
        assert tokens.cookies["bili_jct"] == "bj_val"
        assert tokens.cookies["dedeuserid"] == "duid_val"
        assert auth._last_ac_time_value == "rt_abc"

    @pytest.mark.asyncio
    async def test_empty_cookies(self):
        auth = BilibiliAuthenticator()
        tokens = await auth.get_tokens("k")
        assert tokens.cookies == {}


# ── BilibiliAuthenticator.refresh_tokens ──────────────────────


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_refresh_success(self):
        auth = BilibiliAuthenticator(config_path="/tmp/nonexistent.toml")
        tokens = _sample_tokens()

        mock_cfg = _make_config(ac_time_value="ac_old")
        mock_cred = MagicMock()
        mock_cred.check_refresh = AsyncMock(return_value=True)
        mock_cred.refresh = AsyncMock()
        mock_cred.sessdata = "new_sess"
        mock_cred.bili_jct = "new_jct"
        mock_cred.dedeuserid = "new_duid"
        mock_cred.buvid3 = "new_bv3"
        mock_cred.ac_time_value = "ac_new"

        with (
            patch("shared.config.load_config", return_value=mock_cfg),
            patch("bilibili_api.Credential", return_value=mock_cred),
        ):
            result = await auth.refresh_tokens(tokens)

        assert result.cookies["sessdata"] == "new_sess"
        assert result.cookies["bili_jct"] == "new_jct"
        assert result.cookies["dedeuserid"] == "new_duid"
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
            patch("bilibili_api.Credential", return_value=mock_cred),
        ):
            result = await auth.refresh_tokens(tokens)

        assert result is tokens

    @pytest.mark.asyncio
    async def test_graceful_on_missing_ac_time(self):
        """没有 ac_time_value 时不再抛出异常，返回原始 tokens。"""
        auth = BilibiliAuthenticator(config_path="/tmp/nonexistent.toml")
        tokens = _sample_tokens()
        mock_cfg = _make_config()  # no ac_time_value

        with patch("shared.config.load_config", return_value=mock_cfg):
            result = await auth.refresh_tokens(tokens)

        assert result is tokens

    @pytest.mark.asyncio
    async def test_graceful_on_cookies_refresh_exception(self):
        auth = BilibiliAuthenticator(config_path="/tmp/nonexistent.toml")
        tokens = _sample_tokens()

        mock_cfg = _make_config(ac_time_value="ac_old")
        mock_cred = MagicMock()
        mock_cred.check_refresh = AsyncMock(return_value=True)
        mock_cred.refresh = AsyncMock(side_effect=Exception("correspondPath expired"))

        with (
            patch("shared.config.load_config", return_value=mock_cfg),
            patch("bilibili_api.Credential", return_value=mock_cred),
        ):
            result = await auth.refresh_tokens(tokens)

        assert result is tokens  # 优雅降级，返回原 tokens


# ── BilibiliAuthenticator.validate_tokens ─────────────────────


class TestValidateTokens:
    @pytest.mark.asyncio
    async def test_expired_tokens_return_false(self):
        auth = BilibiliAuthenticator()
        tokens = PlatformTokens(
            platform="bilibili",
            cookies={},
            obtained_at=time.time() - 200 * 86400,
            expires_at=time.time() - 10,
        )
        assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_valid_tokens_return_true(self):
        auth = BilibiliAuthenticator()
        tokens = _sample_tokens()

        mock_cred = MagicMock()
        mock_cred.check_valid = AsyncMock(return_value=True)

        with patch("bilibili_api.Credential", return_value=mock_cred):
            assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_invalid_tokens_return_false(self):
        auth = BilibiliAuthenticator()
        tokens = _sample_tokens()

        mock_cred = MagicMock()
        mock_cred.check_valid = AsyncMock(side_effect=Exception("fail"))

        with patch("bilibili_api.Credential", return_value=mock_cred):
            assert await auth.validate_tokens(tokens) is False


# ── BilibiliAuthenticator.supports_refresh ────────────────────


class TestSupportsRefresh:
    def test_returns_true(self):
        auth = BilibiliAuthenticator()
        assert auth.supports_refresh() is True
