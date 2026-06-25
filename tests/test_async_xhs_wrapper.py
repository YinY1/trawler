"""Tests for AsyncXhsClient — verify asyncio.to_thread wrapping of sync xhs library.

Strategy: patch the underlying xhs.core.XhsClient class, then assert the async
wrapper delegates to the right method and returns/raises the same value/error.
Does NOT test the real xhs library HTTP layer.

See docs/superpowers/plans/2026-06-26-xhs-auth-xhs-library-migration.md (Phase 1).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient

# ── ──


class TestGetQrcode:
    async def test_delegates_to_get_qrcode_and_returns_dict(self) -> None:
        """get_qrcode returns {qr_id, code, url, multi_flag} from underlying client."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_qrcode.return_value = {
                "qr_id": "q1",
                "code": "c1",
                "url": "u1",
                "multi_flag": 0,
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_qrcode()

            mock_instance.get_qrcode.assert_called_once_with()
            assert result == {"qr_id": "q1", "code": "c1", "url": "u1", "multi_flag": 0}


class TestCheckQrcode:
    async def test_delegates_with_qr_id_and_code(self) -> None:
        """check_qrcode passes (qr_id, code) positionally; returns raw dict.

        Regression: real field name is snake_case 'code_status' (spec §1.2 根因 #1).
        """
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.check_qrcode.return_value = {"code_status": 2, "code_msg": "ok"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.check_qrcode("qr_abc", "code_123")

            mock_instance.check_qrcode.assert_called_once_with("qr_abc", "code_123")
            assert result["code_status"] == 2


class TestActivate:
    async def test_delegates_to_activate(self) -> None:
        """activate runs with no args, returns web_session info."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.activate.return_value = {"web_session": "ws"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.activate()

            mock_instance.activate.assert_called_once_with()
            assert result == {"web_session": "ws"}


class TestGetSelfInfo:
    async def test_delegates_to_get_self_info_returns_dict(self) -> None:
        """get_self_info returns user info (e.g. nickname, user_id)."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_self_info.return_value = {"nickname": "alice", "user_id": "u1"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_self_info()

            mock_instance.get_self_info.assert_called_once_with()
            assert result["nickname"] == "alice"


class TestCookieProperty:
    async def test_cookie_getter_returns_underlying_str(self) -> None:
        """cookie property is a transparent passthrough of underlying str."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            # The underlying cookie property returns a "k=v;k=v" string
            type(mock_instance).cookie = property(lambda self: "a1=v1; web_session=ws")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            assert client.cookie == "a1=v1; web_session=ws"


class TestClose:
    async def test_close_closes_underlying_session(self) -> None:
        """XhsClient has no close(); wrapper must close its .session instead."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_session = MagicMock()
            mock_instance.session = mock_session
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.close()

            mock_session.close.assert_called_once_with()


class TestExceptionPassthrough:
    async def test_xhs_data_fetch_error_propagates(self) -> None:
        """xhs library exceptions must propagate unchanged (translation is auth.py's job)."""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_self_info.side_effect = DataFetchError("boom")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            with pytest.raises(DataFetchError, match="boom"):
                await client.get_self_info()
