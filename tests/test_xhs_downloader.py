"""Tests for downloader — 第一层切 wrapper + 协调逻辑修复 + 字段提取。

See docs/superpowers/plans/2026-06-26-xhs-unify.md Task 9/11/12.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.downloader import _try_xhs_downloader_lib
from shared.protocols import NoteInfo, XhsDownloadResult


def _video_note() -> NoteInfo:
    return NoteInfo(
        note_id="n1",
        title="视频笔记",
        author="a",
        user_id="u1",
        note_type="video",
        pubdate=0,
    )


def _image_note() -> NoteInfo:
    return NoteInfo(
        note_id="n2",
        title="图文笔记",
        author="a",
        user_id="u1",
        note_type="normal",
        pubdate=0,
    )


def _make_config(tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.download.dir = str(tmp_path)
    return config


class TestTryXhsDownloaderLibDelegates:
    """第一层切 AsyncXhsClient.get_note_by_id(删 from xhs import)。"""

    async def test_uses_async_wrapper_not_raw_xhs_lib(self) -> None:
        """验证第一层走 AsyncXhsClient(7头签名),不再 from xhs import XhsClient。"""
        mock_client = MagicMock()
        mock_client.get_note_by_id = AsyncMock(return_value={"desc": "d"})
        mock_client.close = AsyncMock()

        with (
            patch(
                "platforms.xiaohongshu.downloader.AsyncXhsClient",
                return_value=mock_client,
            ) as mock_cls,
            patch("platforms.xiaohongshu.downloader.get_xhs_cookie", return_value="c"),
        ):
            await _try_xhs_downloader_lib(_image_note(), _make_config(Path("/tmp")))

        mock_cls.assert_called_once_with(cookie="c")
        mock_client.get_note_by_id.assert_awaited_once_with("n2")

    async def test_returns_none_when_note_detail_empty(self) -> None:
        """get_note_by_id 返回空 dict → 第一层返回 None(降级信号)。"""
        mock_client = MagicMock()
        mock_client.get_note_by_id = AsyncMock(return_value={})
        mock_client.close = AsyncMock()

        with (
            patch(
                "platforms.xiaohongshu.downloader.AsyncXhsClient",
                return_value=mock_client,
            ),
            patch("platforms.xiaohongshu.downloader.get_xhs_cookie", return_value="c"),
        ):
            result = await _try_xhs_downloader_lib(
                _image_note(), _make_config(Path("/tmp"))
            )

        assert result is None


class TestFetchNoteDetailPcShare:
    """第二层(原第三层)_fetch_note_detail 切 wrapper,显式 pc_share。"""

    async def test_delegates_to_wrapper_with_pc_share(self) -> None:
        """get_note_by_id(note_id, xsec_token=t, xsec_source='pc_share')。"""
        from platforms.xiaohongshu.downloader import _fetch_note_detail

        mock_client = MagicMock()
        mock_client.get_note_by_id = AsyncMock(return_value={"note_id": "n1"})
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.downloader.AsyncXhsClient", return_value=mock_client
        ):
            result = await _fetch_note_detail(_video_note(), "cookie")

        mock_client.get_note_by_id.assert_awaited_once_with(
            "n1", xsec_token="", xsec_source="pc_share"
        )
        assert result == {"note_id": "n1"}

    async def test_returns_none_on_exception(self) -> None:
        from platforms.xiaohongshu.downloader import _fetch_note_detail

        mock_client = MagicMock()
        mock_client.get_note_by_id = AsyncMock(side_effect=RuntimeError("net"))
        mock_client.close = AsyncMock()

        with patch(
            "platforms.xiaohongshu.downloader.AsyncXhsClient", return_value=mock_client
        ):
            result = await _fetch_note_detail(_video_note(), "cookie")

        assert result is None


class TestDownloadNoteCoordination:
    """主入口 download_note 协调逻辑:第一层 success=False 必须降级(修的 bug)。"""

    async def test_first_layer_success_no_fallback(self) -> None:
        """第一层 success=True → 直接返回,不调第二层。"""
        from platforms.xiaohongshu.downloader import download_note

        first_result = XhsDownloadResult(
            success=True, source_id="n1", title="t", content_text="ok"
        )

        with (
            patch(
                "platforms.xiaohongshu.downloader._try_xhs_downloader_lib",
                new=AsyncMock(return_value=first_result),
            ) as mock_first,
            patch(
                "platforms.xiaohongshu.downloader._try_direct_download",
                new=AsyncMock(),
            ) as mock_second,
        ):
            result = await download_note(_image_note(), _make_config(Path("/tmp")))

        assert result.success is True
        mock_first.assert_awaited_once()
        mock_second.assert_not_called()

    async def test_first_layer_failure_falls_back(self) -> None:
        """第一层 success=False → 降级到第二层(修的 bug,spec §3.3)。"""
        from platforms.xiaohongshu.downloader import download_note

        first_result = XhsDownloadResult(
            success=False, source_id="n1", title="t", error="视频 URL 提取失败"
        )
        second_result = XhsDownloadResult(
            success=True, source_id="n1", title="t", content_text="recovered"
        )

        with (
            patch(
                "platforms.xiaohongshu.downloader._try_xhs_downloader_lib",
                new=AsyncMock(return_value=first_result),
            ) as mock_first,
            patch(
                "platforms.xiaohongshu.downloader._try_direct_download",
                new=AsyncMock(return_value=second_result),
            ) as mock_second,
        ):
            result = await download_note(_image_note(), _make_config(Path("/tmp")))

        mock_first.assert_awaited_once()
        mock_second.assert_awaited_once()
        assert result.success is True
        assert result.content_text == "recovered"

    async def test_first_layer_none_falls_back(self) -> None:
        """第一层 None(cookie 缺失等)→ 降级到第二层。"""
        from platforms.xiaohongshu.downloader import download_note

        second_result = XhsDownloadResult(
            success=True, source_id="n1", title="t", content_text="ok"
        )

        with (
            patch(
                "platforms.xiaohongshu.downloader._try_xhs_downloader_lib",
                new=AsyncMock(return_value=None),
            ) as mock_first,
            patch(
                "platforms.xiaohongshu.downloader._try_direct_download",
                new=AsyncMock(return_value=second_result),
            ) as mock_second,
        ):
            result = await download_note(_image_note(), _make_config(Path("/tmp")))

        mock_first.assert_awaited_once()
        mock_second.assert_awaited_once()
        assert result.success is True
