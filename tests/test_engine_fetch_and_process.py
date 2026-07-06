# tests/test_engine_fetch_and_process.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.exceptions import PermanentFetchError
from shared.protocols import ContentType, FetchedMessage


@pytest.fixture(autouse=True)
def clean_fetchers() -> None:
    """每个测试前重置 ``PipelineEngine._fetchers``，避免污染（issue #101）。

    ``tests/test_engine.py::clean_engine_state`` 仅清 ``_handlers`` / ``_detectors``
    （在 T7 之前已存在），未覆盖 ``_fetchers`` —— 本测试文件直接注入 mock
    fetcher，必须自行清理。
    """
    from core.engine import PipelineEngine

    PipelineEngine._fetchers = {}


@pytest.mark.asyncio
async def test_platform_from_msg_id():
    """前缀路由正确。"""
    from core.engine import PipelineEngine

    assert PipelineEngine._platform_from_msg_id("bili:BV1xx") == "bili"
    assert PipelineEngine._platform_from_msg_id("xhs:note1") == "xhs"
    assert PipelineEngine._platform_from_msg_id("weibo:123") == "weibo"
    assert PipelineEngine._platform_from_msg_id("unknown:xx") is None
    assert PipelineEngine._platform_from_msg_id("no_prefix") is None


@pytest.mark.asyncio
async def test_run_fetch_and_process_existing_message_skips_fetch(tmp_path):
    """store 已存在的消息 → 不调 fetcher，直接走 process。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    store.add_new(
        msg_id="bili:BV_existing",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=1700000000,
        title="已存在",
        author="UP",
        force=True,
    )

    config = Config()
    # mock fetcher 确保不被调用
    PipelineEngine._fetchers["bili"] = AsyncMock(side_effect=AssertionError("不该调 fetcher"))
    # mock _safe_process_message 避免真跑流水线
    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["bili:BV_existing"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 1


@pytest.mark.asyncio
async def test_run_fetch_and_process_new_message_calls_fetcher(tmp_path):
    """store 不存在的消息 → 调 fetcher → add_new(force=True) → process。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    fake_fm = FetchedMessage(
        msg_id="bili:BV_new",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=1700000000,
        title="新视频",
        author="UP",
    )
    PipelineEngine._fetchers["bili"] = AsyncMock(return_value=fake_fm)

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        fetched = await PipelineEngine.run_fetch_and_process(
            msg_ids=["bili:BV_new"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 1
        # 入库成功
        rec = store.get_message("bili:BV_new")
        assert rec is not None
        assert rec.title == "新视频"
        # 返回值是实际抓取入库数（P0-2 修复：run_fetch_and_process 返回 int）
        assert fetched == 1


@pytest.mark.asyncio
async def test_run_fetch_and_process_permanent_error_skips_no_record(tmp_path):
    """fetcher 抛 PermanentFetchError → log + skip，不创建 record。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    PipelineEngine._fetchers["xhs"] = AsyncMock(
        side_effect=PermanentFetchError("xhs: xsec_token 缺失"),
    )

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["xhs:note_fail"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 0  # 未进入处理
        assert store.get_message("xhs:note_fail") is None  # 未入库


@pytest.mark.asyncio
async def test_run_fetch_and_process_fetcher_returns_none_skips(tmp_path):
    """fetcher 返回 None → log + skip，不创建 record。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    PipelineEngine._fetchers["bili"] = AsyncMock(return_value=None)

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["bili:BV_none"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 0
        assert store.get_message("bili:BV_none") is None


@pytest.mark.asyncio
async def test_run_fetch_and_process_unknown_prefix_skips(tmp_path):
    """未知前缀 → log warning + skip。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["unknown:xx"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 0
