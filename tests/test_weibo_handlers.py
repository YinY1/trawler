"""Tests for platforms/weibo/handlers.py — detector + download handler.

PR-2 (issue #46): 视频检测 + download handler 按 content_type 分支 + 移除内联 AI 摘要。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.protocols import ContentType, WeiboPost


def _make_post(
    post_id: str = "post1",
    video_urls: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> WeiboPost:
    return WeiboPost(
        post_id=post_id,
        text="t",
        clean_text="clean text",
        author="author",
        user_id="uid",
        pubdate=1000,
        image_urls=image_urls or [],
        video_urls=video_urls or [],
    )


def _make_config():
    cfg = MagicMock()
    cfg.weibo.subscriptions = [MagicMock(user_id="uid1")]
    cfg.weibo.auth.cookie = ""
    return cfg


def _get_weibo_detector():
    """从当前 sys.modules 拿 weibo detector,避免跨测试 reload 后引用过期。

    test_engine.py 的部分测试会 ``sys.modules.pop("platforms.weibo.handlers")``,
    导致顶部 ``from platforms.weibo.handlers import weibo_detector`` 拿到的旧函数引用
    在 patch 新模块属性时不生效(旧函数内部还是用旧模块的 fetch_user_posts 引用)。
    每次测试内部重新 import 拿当前版本,patch 才能稳定生效。
    """
    import platforms.weibo.handlers as wb

    return wb.weibo_detector


class TestWeiboDetectorVideo:
    @pytest.mark.asyncio
    async def test_registers_as_video_when_video_urls_present(self):
        """post 含 video_urls 时 detector 注册为 ContentType.VIDEO(spec §3)。"""
        cfg = _make_config()
        store = MagicMock()

        posts_with_video = [_make_post(post_id="v1", video_urls=["https://x.com/v.mp4"])]

        with (
            patch(
                "platforms.weibo.handlers.fetch_user_posts",
                new=AsyncMock(return_value=posts_with_video),
            ),
            patch.object(store, "add_new") as mock_add,
        ):
            await _get_weibo_detector()(cfg, store)

        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args.kwargs
        assert call_kwargs["msg_id"] == "weibo:v1"
        assert call_kwargs["content_type"] == ContentType.VIDEO

    @pytest.mark.asyncio
    async def test_registers_as_text_when_no_video_urls(self):
        """post 无 video_urls 时 detector 注册为 ContentType.TEXT(当前行为)。"""
        cfg = _make_config()
        store = MagicMock()

        posts_text_only = [_make_post(post_id="t1", image_urls=["https://x.com/p.jpg"])]

        with (
            patch(
                "platforms.weibo.handlers.fetch_user_posts",
                new=AsyncMock(return_value=posts_text_only),
            ),
            patch.object(store, "add_new") as mock_add,
        ):
            await _get_weibo_detector()(cfg, store)

        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args.kwargs
        assert call_kwargs["msg_id"] == "weibo:t1"
        assert call_kwargs["content_type"] == ContentType.TEXT
