# tests/test_xhs_fetch_by_id.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.exceptions import DataError, PermanentFetchError
from shared.protocols import ContentType, FetchedMessage


@pytest.mark.asyncio
async def test_fetch_note_by_id_text_note_success():
    """normal 笔记（有 desc）→ FetchedMessage(TEXT)。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    fake_note_card = {
        "note_id": "note_abc",
        "type": "normal",
        "display_title": "测试笔记标题",
        "desc": "笔记正文内容",
        "user": {"nickname": "作者昵称", "userid": "u_1"},
        "xsec_token": "",
        "last_update_time": 1700000000,
    }

    # mock 风格与现有 tests/test_xhs_monitor.py:33 一致：
    # AsyncXhsClient 实际库无 __aenter__/__aexit__（仅 async def close，见
    # async_xhs_wrapper.py:345），实现是裸 client + try/finally: await client.close()
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(return_value=fake_note_card)
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            fm = await fetch_note_by_id("note_abc", config)

    assert fm is not None
    assert isinstance(fm, FetchedMessage)
    assert fm.msg_id == "xhs:note_abc"
    assert fm.platform == "xhs"
    assert fm.content_type is ContentType.TEXT  # type=normal
    assert fm.title == "测试笔记标题"
    assert fm.body == "笔记正文内容"
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_note_by_id_video_note_success():
    """video 笔记 → FetchedMessage(VIDEO)。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    fake_note_card = {
        "note_id": "note_v",
        "type": "video",
        "display_title": "视频笔记",
        "desc": "视频简介",
        "video": {"media": {"stream": {"h264": [{"master_url": "http://x"}]}}},
        "user": {"nickname": "UP", "userid": "u_2"},
    }

    # 与 test_fetch_note_by_id_text_note_success 同款 mock（裸 client）
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(return_value=fake_note_card)
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            fm = await fetch_note_by_id("note_v", config)

    assert fm is not None
    assert fm.content_type is ContentType.VIDEO


@pytest.mark.asyncio
async def test_fetch_note_by_id_data_error_raises_permanent():
    """``DataError``（server 拒绝/-100，token 缺失等）→ PermanentFetchError。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    # 裸 client mock（get_note_by_id 抛 DataError）
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(side_effect=DataError("server rejected"))
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            with pytest.raises(PermanentFetchError):
                await fetch_note_by_id("note_missing", config)


@pytest.mark.asyncio
async def test_fetch_note_by_id_empty_body_raises_permanent():
    """拿到 note_card 但 desc/image_list/video 全空 → PermanentFetchError。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    fake_note_card = {
        "note_id": "note_empty",
        "type": "normal",
        "display_title": "",
        "desc": "",
        "image_list": [],
        "user": {"nickname": "X", "userid": "u_3"},
    }

    # 裸 client mock（拿到 note_card 但正文全空）
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(return_value=fake_note_card)
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            with pytest.raises(PermanentFetchError):
                await fetch_note_by_id("note_empty", config)
