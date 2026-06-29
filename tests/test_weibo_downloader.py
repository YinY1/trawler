"""Tests for platforms/weibo/downloader.py — Weibo media download."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.downloader import _download_file, download_weibo_media
from shared.protocols import WeiboPost

# ── _download_file ─────────────────────────────────────────


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_downloads_successfully(self, tmp_path):
        url = "https://example.com/image.jpg"
        dest = tmp_path / "image.jpg"

        mock_resp = MagicMock()
        mock_resp.status = 200

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
