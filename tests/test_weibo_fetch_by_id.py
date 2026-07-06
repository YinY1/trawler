# tests/test_weibo_fetch_by_id.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.protocols import ContentType, FetchedMessage


@pytest.mark.asyncio
async def test_fetch_post_by_id_text_post_success():
    """无视频 → FetchedMessage(TEXT)。"""
    from platforms.weibo.monitor import fetch_post_by_id

    fake_detail = {
        "id": 12345,
        "text": "<a>作者</a>纯文字微博内容",
        "text_raw": "纯文字微博内容",
        "user": {"screen_name": "作者名"},
        "created_at": "Tue Jun 11 10:00:00 +0800 2026",
        "page_info": {"type": "text"},
    }

    with patch("platforms.weibo.monitor.fetch_post_detail", new=AsyncMock(return_value=fake_detail)):
        from shared.config import Config
        config = Config()
        config.weibo.auth.cookie = "fake_cookie"

        fm = await fetch_post_by_id("12345", config)

    assert fm is not None
    assert isinstance(fm, FetchedMessage)
    assert fm.msg_id == "weibo:12345"
    assert fm.platform == "weibo"
    assert fm.content_type is ContentType.TEXT
    assert fm.author == "作者名"


@pytest.mark.asyncio
async def test_fetch_post_by_id_video_post_success():
    """含视频 page_info → FetchedMessage(VIDEO)。"""
    from platforms.weibo.monitor import fetch_post_by_id

    fake_detail = {
        "id": 67890,
        "text": "<a>UP</a>视频微博",
        "text_raw": "视频微博",
        "user": {"screen_name": "UP主"},
        "created_at": "Tue Jun 11 10:00:00 +0800 2026",
        "page_info": {
            "type": "video",
            "media_info": {"stream_url": "http://example.com/v.mp4"},
        },
    }

    with patch("platforms.weibo.monitor.fetch_post_detail", new=AsyncMock(return_value=fake_detail)):
        from shared.config import Config
        config = Config()
        config.weibo.auth.cookie = "fake_cookie"

        fm = await fetch_post_by_id("67890", config)

    assert fm is not None
    assert fm.content_type is ContentType.VIDEO


@pytest.mark.asyncio
async def test_fetch_post_by_id_detail_empty_returns_none():
    """``fetch_post_detail`` 返回 {} → None。"""
    from platforms.weibo.monitor import fetch_post_by_id

    with patch("platforms.weibo.monitor.fetch_post_detail", new=AsyncMock(return_value={})):
        from shared.config import Config
        config = Config()
        config.weibo.auth.cookie = "fake_cookie"
        fm = await fetch_post_by_id("000", config)

    assert fm is None
