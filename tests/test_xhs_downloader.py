"""Tests for downloader — 第一层切 wrapper + 协调逻辑修复 + 字段提取。

See docs/superpowers/plans/2026-06-26-xhs-unify.md Task 9/11/12.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.downloader import _try_xhs_downloader_lib
from shared.protocols import NoteInfo


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
