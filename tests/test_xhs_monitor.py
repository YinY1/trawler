"""Tests for monitor — _fetch_notes_via_api 切 AsyncXhsClient + dict 解包。

See docs/superpowers/plans/2026-06-26-xhs-unify.md Task 6.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from platforms.xiaohongshu.monitor import _fetch_notes_via_api


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
