# tests/test_bilibili_fetch_by_id.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.protocols import ContentType, FetchedMessage


@pytest.mark.asyncio
async def test_fetch_video_by_id_success(tmp_path):
    """mock ``bilibili_api.video.Video.get_info`` → 正确解析为 FetchedMessage。"""
    from platforms.bilibili.monitor import fetch_video_by_id

    fake_info = {
        "bvid": "BV1xxTest",
        "title": "测试视频标题",
        "pubdate": 1700000000,
        "desc": "视频简介",
        "pic": "//example.com/cover.jpg",
        "owner": {"name": "UP主名字", "mid": 12345},
    }

    # bili get_credential(config) 不抛异常（无凭证时返回未登录 Credential），
    # 但为了测试稳定（不依赖网络/库版本），mock 它返回 None（fetcher 不使用返回值）。
    # 同时显式设 cookie，统一三平台测试的"cookie 来源明确"约定。
    with patch("platforms.bilibili.monitor.bilibili_api") as mock_api:
        mock_video_cls = mock_api.video.Video
        mock_instance = mock_video_cls.return_value
        mock_instance.get_info = AsyncMock(return_value=fake_info)
        with patch("platforms.bilibili.auth.get_credential", return_value=None):
            from shared.config import Config as Cfg
            config = Cfg()
            # 显式 cookie 来源（P1-1：三平台测试统一约定，避免默认空 cookie 误触失败）
            config.bilibili.auth.sessdata = "fake_sessdata"
            config.bilibili.auth.bili_jct = "fake_bili_jct"

            fm = await fetch_video_by_id("BV1xxTest", config)

    assert fm is not None
    assert isinstance(fm, FetchedMessage)
    assert fm.msg_id == "bili:BV1xxTest"
    assert fm.platform == "bili"
    assert fm.content_type is ContentType.VIDEO  # bili 一律 VIDEO
    assert fm.title == "测试视频标题"
    assert fm.author == "UP主名字"
    assert fm.pubdate == 1700000000
    assert fm.body == "视频简介"


@pytest.mark.asyncio
async def test_fetch_video_by_id_api_failure_returns_none(tmp_path):
    """API 抛异常 → 返回 None（调用方可重试）。"""
    from platforms.bilibili.monitor import fetch_video_by_id

    with patch("platforms.bilibili.monitor.bilibili_api") as mock_api:
        mock_video_cls = mock_api.video.Video
        mock_instance = mock_video_cls.return_value
        mock_instance.get_info = AsyncMock(side_effect=Exception("network error"))
        with patch("platforms.bilibili.auth.get_credential", return_value=None):
            from shared.config import Config as Cfg
            config = Cfg()
            config.bilibili.auth.sessdata = "fake_sessdata"
            config.bilibili.auth.bili_jct = "fake_bili_jct"

            fm = await fetch_video_by_id("BV1xxMissing", config)

    assert fm is None
