"""Tests for Xiaohongshu user search — platforms/xiaohongshu/search.py.

Rewrite (2026-06-26): search 切 AsyncXhsClient.get_user_by_keyword,
返回 dict 解包 users。删 _generate_search_id 测试(client.py 将被删)。

See docs/superpowers/plans/2026-06-26-xhs-unify.md Task 8.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.search import search_xhs_user_by_name


class TestSearchXhsUserByName:
    """search 切 AsyncXhsClient.get_user_by_keyword;返回 dict 解包 users。"""

    @pytest.mark.asyncio
    async def test_returns_matching_users(self):
        mock_client = MagicMock()
        mock_client.get_user_by_keyword = AsyncMock(
            return_value={
                "users": [
                    {
                        "id": "5a7d3ed311be106d0306e7d6",
                        "name": "Angelababy",
                        "image": "https://avatar.com/1.jpg",
                    }
                ]
            }
        )
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.search.AsyncXhsClient", return_value=mock_client
        ):
            users = await search_xhs_user_by_name("a1=xxx; web_session=yyy", "Angelababy")

        assert len(users) == 1
        assert users[0]["user_id"] == "5a7d3ed311be106d0306e7d6"
        assert users[0]["nickname"] == "Angelababy"
        assert users[0]["avatar"] == "https://avatar.com/1.jpg"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        mock_client = MagicMock()
        mock_client.get_user_by_keyword = AsyncMock(return_value={"users": []})
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.search.AsyncXhsClient", return_value=mock_client
        ):
            users = await search_xhs_user_by_name("a1=xxx", "未知用户")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        """wrapper 抛异常 → 返回 [],不抛。"""
        mock_client = MagicMock()
        mock_client.get_user_by_keyword = AsyncMock(side_effect=RuntimeError("API error"))
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.search.AsyncXhsClient", return_value=mock_client
        ):
            users = await search_xhs_user_by_name("a1=xxx", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_a1_missing(self):
        """No a1 cookie → early return without calling wrapper."""
        mock_client = MagicMock()
        mock_client.get_user_by_keyword = AsyncMock()
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.search.AsyncXhsClient", return_value=mock_client
        ) as mock_cls:
            users = await search_xhs_user_by_name("web_session=yyy", "test")

        assert users == []
        mock_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_users_when_missing_key(self):
        """wrapper 返回 {} (无 users 键) → 解包为空 list,不抛。"""
        mock_client = MagicMock()
        mock_client.get_user_by_keyword = AsyncMock(return_value={})
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.search.AsyncXhsClient", return_value=mock_client
        ):
            users = await search_xhs_user_by_name("a1=xxx", "test")

        assert users == []
