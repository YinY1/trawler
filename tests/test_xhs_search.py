"""Tests for Xiaohongshu user search — platforms/xiaohongshu/search.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.search import (
    _generate_search_id,
    _generate_search_request_id,
    search_xhs_user_by_name,
)


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
    def _mock_response(self, status: int, json_data: dict) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=json_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    def _setup_mocks(self, mock_resp: MagicMock) -> MagicMock:
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_resp)
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__.return_value = mock_session
        return mock_cls

    @pytest.mark.asyncio
    async def test_returns_matching_users(self):
        mock_resp = self._mock_response(
            200,
            {
                "success": True,
                "data": {
                    "users": [
                        {
                            "user_id": "5a7d3ed311be106d0306e7d6",
                            "nickname": "Angelababy",
                            "avatar": "https://avatar.com/1.jpg",
                        }
                    ],
                    "has_more": False,
                },
            },
        )
        mock_cls = self._setup_mocks(mock_resp)

        with (
            patch("platforms.xiaohongshu.search.aiohttp.ClientSession", mock_cls),
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "t", "xs_common": "c"}),
        ):
            users = await search_xhs_user_by_name("a1=xxx; web_session=yyy", "Angelababy")

        assert len(users) == 1
        assert users[0]["user_id"] == "5a7d3ed311be106d0306e7d6"
        assert users[0]["nickname"] == "Angelababy"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        mock_resp = self._mock_response(
            200,
            {
                "success": True,
                "data": {"users": [], "has_more": False},
            },
        )
        mock_cls = self._setup_mocks(mock_resp)

        with (
            patch("platforms.xiaohongshu.search.aiohttp.ClientSession", mock_cls),
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "t", "xs_common": "c"}),
        ):
            users = await search_xhs_user_by_name("cookie", "未知用户")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        mock_resp = self._mock_response(500, {})
        mock_cls = self._setup_mocks(mock_resp)

        with (
            patch("platforms.xiaohongshu.search.aiohttp.ClientSession", mock_cls),
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "t", "xs_common": "c"}),
        ):
            users = await search_xhs_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_success_false(self):
        mock_resp = self._mock_response(
            200,
            {
                "success": False,
                "msg": "rate limited",
            },
        )
        mock_cls = self._setup_mocks(mock_resp)

        with (
            patch("platforms.xiaohongshu.search.aiohttp.ClientSession", mock_cls),
            patch("platforms.xiaohongshu.signer.get_xhs_sign", return_value={"xs": "x", "xt": "t", "xs_common": "c"}),
        ):
            users = await search_xhs_user_by_name("cookie", "test")

        assert users == []
