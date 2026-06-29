"""Tests for platforms/weibo/handlers.py — detector + download handler.

PR-2 (issue #46): 视频检测 + download handler 按 content_type 分支 + 移除内联 AI 摘要。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.protocols import ContentType, PhaseContext, WeiboPost


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


def _get_weibo_download():
    """与 ``_get_weibo_detector`` 同理:每次测试内部重新 import 拿当前版本,
    避免 test_engine.py 的 ``sys.modules.pop`` 让顶部旧引用失效。"""
    import platforms.weibo.handlers as wb

    return wb.weibo_download


class TestWeiboDownloadVideoBranch:
    @pytest.mark.asyncio
    async def test_video_type_calls_download_weibo_video_and_sets_filepath(self):
        """VIDEO 类型 download handler 必须调 download_weibo_video 并设 ctx.downloaded_filepath。"""
        from pathlib import Path

        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:v1"
        msg.title = "视频微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.VIDEO

        ctx = PhaseContext(msg=msg, config=cfg)

        mock_video_result = MagicMock()
        mock_video_result.success = True
        mock_video_result.filepath = Path("/tmp/weibo/v1/v1.mp4")
        mock_video_result.text = "视频微博正文"
        mock_video_result.image_paths = []

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_video",
                new=AsyncMock(return_value=mock_video_result),
            ) as mock_video_fn,
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
        ):
            result = await _get_weibo_download()(ctx)

        assert result is True
        # 关键:调用了 download_weibo_video(而非 download_weibo_media)
        mock_video_fn.assert_called_once()
        # 关键:filepath 透传到 ctx(下游 transcribe_phase 需要)
        assert ctx.downloaded_filepath == Path("/tmp/weibo/v1/v1.mp4")
        assert ctx.content_text == "视频微博正文"

    @pytest.mark.asyncio
    async def test_video_type_does_not_call_analyze_content(self):
        """VIDEO 类型 download handler 不再调用 analyze_content(移除内联摘要)。

        spec §6: weibo VIDEO 通过 PHASE_FLOW 自然走 SUMMARIZED handler,
        由通用 summarize_phase 调 analyze_content,而非 download 内联。

        实现侧证明:`analyze_content` import 已删除(handlers 模块不含此符号);
        行为侧证明:handler 执行后 ctx.summary_text 应保持空(download 不产摘要)。
        """
        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:v2"
        msg.title = "视频微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.VIDEO

        ctx = PhaseContext(msg=msg, config=cfg)

        mock_video_result = MagicMock()
        mock_video_result.success = True
        mock_video_result.filepath = MagicMock()
        mock_video_result.text = "正文"
        mock_video_result.image_paths = []

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_video",
                new=AsyncMock(return_value=mock_video_result),
            ),
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
        ):
            await _get_weibo_download()(ctx)

        # 关键 1:模块层已不再 import analyze_content(spec §6 内联摘要移除的物证)
        import platforms.weibo.handlers as wb_mod

        assert not hasattr(wb_mod, "analyze_content")
        # 关键 2:download handler 没有生成摘要(走 SUMMARIZED 才会产)
        assert ctx.summary_text == ""


class TestWeiboDownloadTextBranch:
    @pytest.mark.asyncio
    async def test_text_type_calls_download_weibo_media(self):
        """TEXT 类型保持当前行为:调 download_weibo_media 下图片。"""
        from pathlib import Path

        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:t1"
        msg.title = "图文微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.TEXT

        ctx = PhaseContext(msg=msg, config=cfg)
        ctx.config.weibo.auth.cookie = ""  # 跳过长文获取

        mock_media_result = MagicMock()
        mock_media_result.success = True
        mock_media_result.image_paths = [Path("/tmp/img1.jpg")]
        mock_media_result.text = "图文正文"
        mock_media_result.filepath = None

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_media",
                new=AsyncMock(return_value=mock_media_result),
            ) as mock_media_fn,
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
        ):
            result = await _get_weibo_download()(ctx)

        assert result is True
        mock_media_fn.assert_called_once()
        assert ctx.image_paths == [Path("/tmp/img1.jpg")]
        assert ctx.content_text == "图文正文"

    @pytest.mark.asyncio
    async def test_text_type_does_not_call_analyze_content(self):
        """TEXT 类型不再调 analyze_content(移除内联摘要,推全文)。

        实现侧:`analyze_content` import 已删除;行为侧:ctx.summary_text 应保持空。
        """
        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:t2"
        msg.title = "图文微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.TEXT

        ctx = PhaseContext(msg=msg, config=cfg)
        ctx.config.weibo.auth.cookie = ""

        mock_media_result = MagicMock()
        mock_media_result.success = True
        mock_media_result.image_paths = []
        mock_media_result.text = "正文"
        mock_media_result.filepath = None

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_media",
                new=AsyncMock(return_value=mock_media_result),
            ),
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
            patch(
                "platforms.weibo.handlers.fetch_weibo_comment_highlights",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await _get_weibo_download()(ctx)

        # 关键 1:模块层已不再 import analyze_content
        import platforms.weibo.handlers as wb_mod

        assert not hasattr(wb_mod, "analyze_content")
        # 关键 2:TEXT 类型不再产摘要(推全文,无 AI 摘要)
        assert ctx.summary_text == ""
