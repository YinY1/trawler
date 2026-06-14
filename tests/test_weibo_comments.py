"""Tests for platforms/weibo/comments.py — Weibo comment highlights."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.comments import (
    _parse_comment,
    fetch_weibo_comment_highlights,
)

# ── _parse_comment ─────────────────────────────────────────


class TestParseComment:
    def test_parses_basic_comment(self):
        raw = {
            "text": "这是一条评论 <a href='/n/user'>@user</a>",
            "user": {"screen_name": "评论用户", "id": 12345},
            "like_count": 42,
        }
        result = _parse_comment(raw, author_user_id="999")
        assert result is not None
        assert "这是一条评论" in result.content
        assert result.user_name == "评论用户"
        assert result.is_author is False
        assert result.like_count == 42

    def test_marks_author_comment(self):
        raw = {
            "text": "作者回复",
            "user": {"screen_name": "博主", "id": 999},
            "like_count": 10,
        }
        result = _parse_comment(raw, author_user_id="999")
        assert result is not None
        assert result.is_author is True

    def test_returns_none_on_empty_text(self):
        raw = {"text_raw": "", "text": "", "user": {"screen_name": "u", "id": 1}, "like_count": 0}
        assert _parse_comment(raw) is None

    def test_returns_none_on_missing_text(self):
        raw = {"user": {"screen_name": "u", "id": 1}}
        assert _parse_comment(raw) is None

    def test_handles_missing_user(self):
        raw = {"text": "hello", "like_count": 5}
        result = _parse_comment(raw)
        assert result is not None
        assert result.user_name == ""
        assert result.is_author is False
        assert result.like_count == 5

    def test_prefers_text_raw_over_text(self):
        raw = {
            "text_raw": "纯净文本",
            "text": "<b>HTML版本</b>",
            "user": {"screen_name": "u", "id": 1},
            "like_count": 3,
        }
        result = _parse_comment(raw)
        assert result is not None
        assert result.content == "纯净文本"


# ── fetch_weibo_comment_highlights ─────────────────────────


class TestFetchWeiboCommentHighlights:
    @pytest.mark.asyncio
    async def test_returns_highlights(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = AsyncMock()
        mock_resp.status = 200

        async def json_side(*, content_type=None) -> dict:
            return {
                "ok": 1,
                "data": [
                    {
                        "text_raw": "好评论",
                        "text": "好评论",
                        "user": {"screen_name": "用户A", "id": 1},
                        "like_count": 100,
                    },
                ],
                "total_number": 1,
                "max_id": 0,
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert len(results) == 1
        assert results[0].content == "好评论"
        assert results[0].like_count == 100

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cookie(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = ""

        results = await fetch_weibo_comment_highlights("post123", cfg)
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert results == []

    @pytest.mark.asyncio
    async def test_sorts_by_like_count_descending(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = AsyncMock()
        mock_resp.status = 200

        async def json_side(*, content_type=None) -> dict:
            return {
                "ok": 1,
                "data": [
                    {"text_raw": "低赞", "text": "低赞", "user": {"screen_name": "u", "id": 1}, "like_count": 1},
                    {"text_raw": "高赞", "text": "高赞", "user": {"screen_name": "u", "id": 2}, "like_count": 99},
                ],
                "total_number": 2,
                "max_id": 0,
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert len(results) == 2
        assert results[0].content == "高赞"
        assert results[1].content == "低赞"
