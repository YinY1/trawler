"""Tests for Xiaohongshu user search — platforms/xiaohongshu/search.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from platforms.xiaohongshu.client import _generate_search_id, _generate_search_request_id
from platforms.xiaohongshu.search import search_xhs_user_by_name


class TestGenerateSearchId:
    def test_returns_string(self):
        sid = _generate_search_id()
        assert isinstance(sid, str)
        assert len(sid) > 0

    def test_unique_per_call(self):
        ids = {_generate_search_id() for _ in range(10)}
        assert len(ids) == 10  # all unique


class TestGenerateSearchRequestId:
    def test_returns_string_with_dash(self):
        rid = _generate_search_request_id()
        assert isinstance(rid, str)
        assert "-" in rid

    def test_unique_per_call(self):
        ids = {_generate_search_request_id() for _ in range(10)}
        assert len(ids) == 10


class TestSearchXhsUserByName:
    """search_xhs_user_by_name uses XhsClient.search_users; tests mock that entry."""

    @pytest.mark.asyncio
    async def test_returns_matching_users(self):
        with patch(
            "platforms.xiaohongshu.client.XhsClient.search_users",
            new=AsyncMock(
                return_value=[
                    {
                        "user_id": "5a7d3ed311be106d0306e7d6",
                        "nickname": "Angelababy",
                        "avatar": "https://avatar.com/1.jpg",
                    }
                ],
            ),
        ):
            users = await search_xhs_user_by_name("a1=xxx; web_session=yyy", "Angelababy")

        assert len(users) == 1
        assert users[0]["user_id"] == "5a7d3ed311be106d0306e7d6"
        assert users[0]["nickname"] == "Angelababy"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        with patch(
            "platforms.xiaohongshu.client.XhsClient.search_users",
            new=AsyncMock(return_value=[]),
        ):
            users = await search_xhs_user_by_name("a1=xxx", "未知用户")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        """XhsClient.search_users raises; search must catch and return []."""
        with patch(
            "platforms.xiaohongshu.client.XhsClient.search_users",
            new=AsyncMock(side_effect=RuntimeError("API error")),
        ):
            users = await search_xhs_user_by_name("a1=xxx", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_a1_missing(self):
        """No a1 cookie → early return without calling search_users."""
        with patch(
            "platforms.xiaohongshu.client.XhsClient.search_users",
            new=AsyncMock(),
        ) as mock_req:
            users = await search_xhs_user_by_name("web_session=yyy", "test")

        assert users == []
        mock_req.assert_not_called()
