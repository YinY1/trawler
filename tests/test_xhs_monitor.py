"""Tests for monitor — _fetch_notes_via_api 切 AsyncXhsClient + dict 解包。

See docs/superpowers/plans/2026-06-26-xhs-unify.md Task 6.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.monitor import _fetch_notes_via_api, fetch_user_notes
from shared.config import Config
from shared.exceptions import DataError


class TestFetchNotesViaApi:
    """_fetch_notes_via_api: AsyncXhsClient.get_user_notes 返回 dict,解包 notes。"""

    async def test_unpacks_notes_from_dict(self) -> None:
        """wrapper 返回 {notes, cursor, has_more},解包出 notes list。"""
        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(
            return_value={
                "notes": [{"note_id": "n1"}, {"note_id": "n2"}],
                "cursor": "next",
                "has_more": True,
            }
        )
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client
        ):
            result = await _fetch_notes_via_api("u1", "cookie")

        assert result == [{"note_id": "n1"}, {"note_id": "n2"}]
        mock_client.get_user_notes.assert_awaited_once_with("u1", cursor="")

    async def test_empty_notes_when_missing_key(self) -> None:
        """wrapper 返回 {} (无 notes 键) → 解包为空 list,不抛。"""
        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(return_value={})
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client
        ):
            result = await _fetch_notes_via_api("u1", "cookie")

        assert result == []

    async def test_returns_empty_list_on_exception(self) -> None:
        """wrapper 抛异常 → 返回 [],不抛(主流程降级)。"""
        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(side_effect=RuntimeError("net"))
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client
        ):
            result = await _fetch_notes_via_api("u1", "cookie")

        assert result == []

    async def test_closes_client_in_finally(self) -> None:
        """无论成功失败,close() 都被调(资源不泄漏)。"""
        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(return_value={"notes": []})
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client
        ):
            await _fetch_notes_via_api("u1", "cookie")

        mock_client.close.assert_awaited_once()


class TestFetchNotesViaApiSessionExpired:
    """_fetch_notes_via_api: XHS -100 (session expired) 应重新抛出 DataError。"""

    async def test_reraises_data_error_for_code_minus_100(self) -> None:
        """DataError 含 -100 → 重新抛出，不降级为空 list。"""
        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(
            side_effect=DataError("XHS data fetch error: {'code': -100, 'msg': '登录已过期'}")
        )
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client
        ):
            with pytest.raises(DataError, match="-100"):
                await _fetch_notes_via_api("u1", "cookie")

    async def test_non_session_data_error_returns_empty(self) -> None:
        """DataError 不含 -100 → 降级返回空 list（其他 data error 不触发写回）。"""
        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(
            side_effect=DataError("XHS data fetch error: {'code': -2, 'msg': '其它错误'}")
        )
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client
        ):
            result = await _fetch_notes_via_api("u1", "cookie")

        assert result == []


class TestFetchUserNotesWriteback:
    """fetch_user_notes: XHS -100 触发时写回 expires_at=0。"""

    async def test_marks_expires_at_zero_on_session_expired(self) -> None:
        """DataError(-100) 时 config.xiaohongshu.auth.expires_at 被置 0。"""
        config = Config()
        config.xiaohongshu.auth.cookie = "fake_cookie=1"
        config.xiaohongshu.auth.expires_at = 9999999999.0

        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(
            side_effect=DataError("XHS data fetch error: {'code': -100, 'msg': '登录已过期'}")
        )
        mock_client.close = AsyncMock()

        with (
            patch("platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await fetch_user_notes("u1", "测试用户", config)

        assert result == []
        assert config.xiaohongshu.auth.expires_at == 0.0
        mock_update.assert_awaited_once_with(
            "xhs",
            {"expires_at": 0.0},
            config_path="config/config.toml",
        )

    async def test_no_writeback_on_generic_error(self) -> None:
        """非 -100 错误（如 RuntimeError）不触发 expires_at 写回。"""
        config = Config()
        config.xiaohongshu.auth.cookie = "fake_cookie=1"
        config.xiaohongshu.auth.expires_at = 9999999999.0

        with (
            patch(
                "platforms.xiaohongshu.monitor._fetch_notes_via_api",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network boom"),
            ),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            result = await fetch_user_notes("u1", "测试用户", config)

        assert result == []
        assert config.xiaohongshu.auth.expires_at == 9999999999.0
        mock_update.assert_not_awaited()

    async def test_writeback_failure_does_not_block(self) -> None:
        """磁盘写回失败时 fetch_user_notes 仍返回空 list，不抛异常。"""
        config = Config()
        config.xiaohongshu.auth.cookie = "fake_cookie=1"
        config.xiaohongshu.auth.expires_at = 9999999999.0

        mock_client = MagicMock()
        mock_client.get_user_notes = AsyncMock(
            side_effect=DataError("XHS data fetch error: {'code': -100, 'msg': '登录已过期'}")
        )
        mock_client.close = AsyncMock()

        with (
            patch("platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client),
            patch(
                "shared.auth.update_auth_section",
                new_callable=AsyncMock,
                side_effect=PermissionError("read-only fs"),
            ),
        ):
            result = await fetch_user_notes("u1", "测试用户", config)

        assert result == []
        assert config.xiaohongshu.auth.expires_at == 0.0
