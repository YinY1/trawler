"""Tests for platforms/weibo/monitor.py — Weibo post monitoring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.monitor import WeiboSubscriptionStore, check_new_weibo_posts
from shared.config import Config
from shared.protocols import WeiboPost


class TestWeiboSubscriptionStore:
    def test_init_defaults(self, tmp_path):
        store = WeiboSubscriptionStore(str(tmp_path))
        assert store.known_count == 0

    def test_mark_known_post(self, tmp_path):
        store = WeiboSubscriptionStore(str(tmp_path))
        post = WeiboPost(
            post_id="post123",
            text="test",
            clean_text="test",
            author="u",
            user_id="1",
            pubdate=1000,
        )
        store.mark_known_weibo_post(post)
        assert store.is_known("post123")


class TestCheckNewWeiboPosts:
    @pytest.mark.asyncio
    async def test_returns_new_posts(self):
        cfg = Config()
        cfg.weibo.auth.cookie = "SUB=fake"
        store = MagicMock()
        store.is_known.return_value = False

        mock_post = WeiboPost(
            post_id="new123",
            text="新微博",
            clean_text="新微博",
            author="用户A",
            user_id="12345",
            pubdate=2000,
        )

        with patch("platforms.weibo.monitor.fetch_user_posts", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = [mock_post]
            results = await check_new_weibo_posts(
                user_id="12345",
                name="用户A",
                config=cfg,
                store=store,
            )

        assert len(results) == 1
        assert results[0].post_id == "new123"

    @pytest.mark.asyncio
    async def test_filters_known_posts(self):
        cfg = Config()
        cfg.weibo.auth.cookie = "SUB=fake"
        store = MagicMock()
        store.is_known.side_effect = lambda pid: pid == "old123"

        old_post = WeiboPost(
            post_id="old123",
            text="旧微博",
            clean_text="旧微博",
            author="u",
            user_id="1",
            pubdate=1000,
        )
        new_post = WeiboPost(
            post_id="new123",
            text="新微博",
            clean_text="新微博",
            author="u",
            user_id="1",
            pubdate=2000,
        )

        with patch("platforms.weibo.monitor.fetch_user_posts", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = [old_post, new_post]
            results = await check_new_weibo_posts(
                user_id="12345",
                name="用户A",
                config=cfg,
                store=store,
            )

        assert len(results) == 1
        assert results[0].post_id == "new123"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_cookie(self):
        cfg = Config()
        cfg.weibo.auth.cookie = ""
        store = MagicMock()

        results = await check_new_weibo_posts(
            user_id="12345",
            name="用户A",
            config=cfg,
            store=store,
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_failure(self):
        cfg = Config()
        cfg.weibo.auth.cookie = "SUB=fake"
        store = MagicMock()

        with patch("platforms.weibo.monitor.fetch_user_posts", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = []
            results = await check_new_weibo_posts(
                user_id="12345",
                name="用户A",
                config=cfg,
                store=store,
            )

        assert results == []
