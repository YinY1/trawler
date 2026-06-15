"""Tests for platforms/weibo/api.py — Weibo HTTP API wrapper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.api import (
    _parse_mobile_post,
    _parse_pc_post,
    fetch_user_posts_mobile,
    fetch_user_posts_pc,
    search_user_by_name,
)

# ── Cookie helper ─────────────────────────────────────────


def _make_cookie_str() -> str:
    return "SUB=fake_sub; SUBP=fake_subp; WBPSESS=fake_wbpsess; SSOLoginState=123"


# ── fetch_user_posts_mobile ───────────────────────────────


class TestFetchUserPostsMobile:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "cards": [
                        {
                            "card_type": 9,
                            "mblog": {
                                "id": "post123",
                                "text": "测试微博内容",
                                "user": {"screen_name": "测试用户", "id": 12345},
                                "created_at": "Tue Jun 11 10:00:00 +0800 2026",
                                "pics": [{"url": "https://wx1.sinaimg.cn/large/001.jpg"}],
                                "reposts_count": 10,
                                "comments_count": 5,
                                "attitudes_count": 100,
                                "is_original": 0,
                            },
                        }
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert len(posts) == 1
        assert posts[0]["id"] == "post123"

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"ok": 0}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert posts == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert posts == []


# ── fetch_user_posts_pc ───────────────────────────────────


class TestFetchUserPostsPc:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "list": [
                        {
                            "id": 456789,
                            "idstr": "456789",
                            "text": "PC端微博内容",
                            "user": {"screen_name": "PC用户", "id": 67890},
                            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
                            "pic_ids": ["001", "002"],
                            "reposts_count": "20",
                            "comments_count": "15",
                            "attitudes_count": "200",
                            "is_original": 0,
                        }
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_pc(cookie, user_id="67890")

        assert len(posts) == 1
        assert posts[0]["id"] == 456789

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"ok": 0}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_pc(cookie, user_id="67890")

        assert posts == []


# ── _parse_mobile_post ────────────────────────────────────


class TestParseMobilePost:
    def test_parses_basic_post(self):
        raw = {
            "id": "post123",
            "text": "测试微博 <a href='/n/test'>@test</a> 内容",
            "user": {"screen_name": "测试用户", "id": 12345},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pics": [{"url": "https://wx1.sinaimg.cn/large/001.jpg"}],
            "reposts_count": 10,
            "comments_count": 5,
            "attitudes_count": 100,
            "is_original": 1,
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.post_id == "post123"
        assert "测试微博" in result.text
        assert "测试微博" in result.clean_text  # HTML tags stripped
        assert result.author == "测试用户"
        assert result.user_id == "12345"
        assert result.pubdate > 0
        assert len(result.image_urls) == 1
        assert result.reposts_count == 10
        assert result.comments_count == 5
        assert result.likes_count == 100
        assert result.is_original is True

    def test_handles_reposted_post(self):
        raw = {
            "id": "post456",
            "text": "转发微博",
            "user": {"screen_name": "转发者", "id": 999},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "reposts_count": 0,
            "comments_count": 0,
            "attitudes_count": 0,
            "is_original": 0,
            "retweeted_status": {
                "id": "original123",
                "text": "原始微博内容",
                "user": {"screen_name": "原作者", "id": 111},
                "created_at": "Mon Jun 10 08:00:00 +0800 2026",
                "reposts_count": 50,
                "comments_count": 20,
                "attitudes_count": 300,
                "is_original": 1,
            },
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.is_original is False
        assert result.reposted_post is not None
        assert result.reposted_post.post_id == "original123"
        assert result.reposted_post.author == "原作者"

    def test_returns_none_on_missing_id(self):
        raw = {"text": "no id post"}
        result = _parse_mobile_post(raw)
        assert result is None


# ── _parse_pc_post ────────────────────────────────────────


class TestParsePcPost:
    def test_parses_basic_post(self):
        raw = {
            "id": 456789,
            "idstr": "456789",
            "text": "PC端微博 <a href='/n/test'>@test</a>",
            "user": {"screen_name": "PC用户", "id": 67890},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pic_ids": ["001", "002"],
            "pic_infos": {
                "001": {"original": {"url": "https://wx1.sinaimg.cn/large/001.jpg"}},
                "002": {"original": {"url": "https://wx1.sinaimg.cn/large/002.jpg"}},
            },
            "reposts_count": "20",
            "comments_count": "15",
            "attitudes_count": "200",
            "is_original": 1,
        }
        result = _parse_pc_post(raw)
        assert result is not None
        assert result.post_id == "456789"
        assert "PC端微博" in result.text
        assert result.author == "PC用户"
        assert result.user_id == "67890"
        assert len(result.image_urls) == 2
        assert result.reposts_count == 20
        assert result.comments_count == 15
        assert result.likes_count == 200
        assert result.is_original is True

    def test_returns_none_on_missing_id(self):
        raw = {"text": "no id"}
        result = _parse_pc_post(raw)
        assert result is None


# ── search_user_by_name ─────────────────────────────────────────


class TestSearchUserByName:
    def _mock_response(self, status: int, json_data: dict) -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.json = AsyncMock(return_value=json_data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    def _setup_mocks(self, mock_resp: MagicMock) -> tuple[MagicMock, MagicMock]:
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__.return_value = mock_session
        return mock_cls, mock_session

    @pytest.mark.asyncio
    async def test_returns_matching_users(self):
        mock_resp = self._mock_response(
            200,
            {
                "ok": 1,
                "data": {
                    "cards": [
                        {
                            "card_group": [
                                {
                                    "user": {
                                        "id": 2803301701,
                                        "screen_name": "人民日报",
                                        "description": "人民日报官方微博",
                                    }
                                }
                            ]
                        }
                    ]
                },
            },
        )
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "人民日报")

        assert len(users) == 1
        assert users[0]["id"] == 2803301701
        assert users[0]["screen_name"] == "人民日报"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        mock_resp = self._mock_response(200, {"ok": 1, "data": {"cards": []}})
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "未知用户")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        mock_resp = self._mock_response(500, {})
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_not_ok(self):
        mock_resp = self._mock_response(200, {"ok": 0})
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []
