"""Tests for platforms/weibo/downloader.py — Weibo media download."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.downloader import (
    _download_file,
    download_weibo_media,
    download_weibo_video,
)
from shared.protocols import WeiboPost

# ── _download_file ─────────────────────────────────────────


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_downloads_successfully(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content_length = None

        async def read_side() -> bytes:
            return b"fake_image_data"

        mock_resp.read = AsyncMock(side_effect=read_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await _download_file(url, dest)

        assert result is True
        assert dest.read_bytes() == b"fake_image_data"

    @pytest.mark.asyncio
    async def test_fails_on_bad_status(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await _download_file(url, dest)

        assert result is False
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_fails_on_exception(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=Exception("network error"))

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await _download_file(url, dest)

        assert result is False

    @pytest.mark.asyncio
    async def test_download_file_content_length_mismatch_returns_false(
        self, tmp_path
    ):
        """content_length=1000 但 read() 只返回 500 字节 → 完整性校验失败,不写文件。"""
        url = "https://example.com/image.jpg"
        dest = tmp_path / "test.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content_length = 1000
        mock_resp.read = AsyncMock(return_value=b"x" * 500)
        mock_resp.close = MagicMock()
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await _download_file(url, dest)

        assert result is False
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_download_file_content_length_match_writes_file(self, tmp_path):
        """content_length=10 且 read() 返回 10 字节 → 正常写盘,返回 True。"""
        url = "https://example.com/image.jpg"
        dest = tmp_path / "test.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content_length = 10
        mock_resp.read = AsyncMock(return_value=b"x" * 10)
        mock_resp.close = MagicMock()
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await _download_file(url, dest)

        assert result is True
        assert dest.exists()
        assert dest.read_bytes() == b"x" * 10


# ── download_weibo_media ───────────────────────────────────


def _make_post(image_urls: list[str] | None = None) -> WeiboPost:
    return WeiboPost(
        post_id="post123",
        text="测试微博内容",
        clean_text="测试微博内容",
        author="用户A",
        user_id="12345",
        pubdate=1000,
        image_urls=image_urls or [],
    )


class TestDownloadWeiboMedia:
    @pytest.mark.asyncio
    async def test_returns_success_with_no_images(self):
        cfg = MagicMock()
        cfg.download.dir = "/tmp/downloads"

        post = _make_post(image_urls=[])
        result = await download_weibo_media(post, cfg)

        assert result.success is True
        assert result.source_id == "post123"
        assert result.image_paths == []

    @pytest.mark.asyncio
    async def test_downloads_all_images(self, tmp_path):
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_post(
            image_urls=[
                "https://example.com/img1.jpg",
                "https://example.com/img2.png",
            ]
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content_length = None
        mock_resp.read = AsyncMock(return_value=b"data")
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await download_weibo_media(post, cfg)

        assert result.success is True
        assert len(result.image_paths) == 2
        # Files should exist on disk
        for path in result.image_paths:
            assert path.exists()

    @pytest.mark.asyncio
    async def test_reports_failure_when_all_downloads_fail(self, tmp_path):
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_post(image_urls=["https://example.com/img1.jpg"])

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await download_weibo_media(post, cfg)

        assert result.success is False
        assert result.error is not None


class TestWeiboDownloadResultFilepathField:
    def test_filepath_defaults_to_none(self):
        """WeiboDownloadResult 必须有 filepath 字段,默认 None。

        download_weibo_video 把视频路径写入此字段,download handler 读它设到 ctx.downloaded_filepath。
        """
        from shared.protocols import WeiboDownloadResult

        result = WeiboDownloadResult(
            success=True,
            source_id="post1",
            title="t",
        )
        assert result.filepath is None

    def test_filepath_accepts_path(self):
        from pathlib import Path

        from shared.protocols import WeiboDownloadResult

        result = WeiboDownloadResult(
            success=True,
            source_id="post1",
            title="t",
            filepath=Path("/tmp/weibo/post1/post1.mp4"),
        )
        assert result.filepath == Path("/tmp/weibo/post1/post1.mp4")


# ── download_weibo_video ────────────────────────────────────


def _make_video_post(video_urls: list[str] | None = None) -> WeiboPost:
    return WeiboPost(
        post_id="videopost1",
        text="视频微博",
        clean_text="视频微博",
        author="视频博主",
        user_id="88888",
        pubdate=2000,
        video_urls=video_urls or [],
    )


class TestDownloadWeiboVideo:
    @pytest.mark.asyncio
    async def test_downloads_video_successfully(self, tmp_path):
        """有 video_urls 时,下载第一个 URL 到 {post_id}.mp4,filepath 字段填入路径。"""
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_video_post(video_urls=["https://example.com/video.mp4"])

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.content_length = None
        mock_resp.read = AsyncMock(return_value=b"fake_mp4_bytes")
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await download_weibo_video(post, cfg)

        assert result.success is True
        assert result.source_id == "videopost1"
        assert result.filepath is not None
        assert result.filepath.exists()
        assert result.filepath.read_bytes() == b"fake_mp4_bytes"
        assert result.filepath.name == "videopost1.mp4"

    @pytest.mark.asyncio
    async def test_fails_when_no_video_urls(self, tmp_path):
        """video_urls 为空时返回 success=False,filepath=None。"""
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_video_post(video_urls=[])

        result = await download_weibo_video(post, cfg)

        assert result.success is False
        assert result.filepath is None
        assert result.error is not None
        assert "无视频" in result.error or "no video" in result.error.lower()

    @pytest.mark.asyncio
    async def test_fails_on_download_error(self, tmp_path):
        """HTTP 错误时返回 success=False,filepath=None。"""
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_video_post(video_urls=["https://example.com/missing.mp4"])

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await download_weibo_video(post, cfg)

        assert result.success is False
        assert result.filepath is None
        assert result.error is not None
