"""Tests for manual content check — run_specific_messages + skip_push behavior.

Plan: docs/superpowers/plans/2026-06-28-manual-content-check.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.engine import PipelineEngine
from shared.protocols import ContentType, MessageRecord, Phase, PhaseContext


# autouse: 防止 run_specific_messages 触发的 handler 模块导入污染全局注册表
# 和 sys.modules 缓存（oracle review Issue 6）。test_engine.py 的某些测试
# 依赖 import 时装饰器重新触发，所以这里同时清掉 sys.modules 缓存。
@pytest.fixture(autouse=True)
def clean_engine_state() -> None:
    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    for mod in list(sys.modules):
        if mod.startswith("platforms.") and mod.endswith(".handlers"):
            sys.modules.pop(mod, None)


# ── 任务 3: PhaseContext.skip_push 字段 ────────────────────────────


def test_phase_context_has_skip_push_default_false() -> None:
    """PhaseContext 必须有 skip_push 字段，默认 False。"""
    msg = MessageRecord(
        msg_id="x", platform="bili", content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED, pubdate=0, title="t", author="a",
    )
    ctx = PhaseContext(msg=msg, config=None)  # type: ignore[arg-type]
    assert ctx.skip_push is False


def test_phase_context_skip_push_can_be_set_true() -> None:
    """PhaseContext.skip_push 可显式传 True。"""
    msg = MessageRecord(
        msg_id="x", platform="bili", content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED, pubdate=0, title="t", author="a",
    )
    ctx = PhaseContext(msg=msg, config=None, skip_push=True)  # type: ignore[arg-type]
    assert ctx.skip_push is True


# ── 任务 4: engine.run_specific_messages + process_message ctx 透传 ────

import time  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from shared.message_store import MessageStore  # noqa: E402


@pytest.fixture
def mock_store(tmp_path: Path) -> MessageStore:
    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.mark_phase("bili:BV2", Phase.PUSHED)
    return store


async def test_run_specific_messages_resets_and_processes(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """run_specific_messages 应 reset 目标消息并逐条 process。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    # 用真实 process_message 的 ctx 创建逻辑，捕获 ctx 验证 skip_push 透传
    captured_ctxs: list[PhaseContext] = []

    async def fake_process(msg: Any, cfg: Any, store: Any) -> None:
        # 复刻 process_message 的 ctx 创建逻辑，验证 _skip_push 被读取
        ctx = PhaseContext(msg=msg, config=cfg, skip_push=getattr(msg, "_skip_push", False))
        captured_ctxs.append(ctx)

    with patch.object(PipelineEngine, "process_message", new=fake_process):
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
        )
    # 验证 reset 生效
    assert mock_store.get_message("bili:BV1").phase == Phase.SUMMARIZED
    # 验证 skip_push 通过 ctx 透传（oracle Issue 8 必须有 assert）
    assert len(captured_ctxs) == 1
    assert captured_ctxs[0].skip_push is True


async def test_run_specific_messages_skip_push_false(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """skip_push=False 时 ctx.skip_push 也应为 False。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    captured_ctxs: list[PhaseContext] = []

    async def fake_process(msg: Any, cfg: Any, store: Any) -> None:
        ctx = PhaseContext(msg=msg, config=cfg, skip_push=getattr(msg, "_skip_push", False))
        captured_ctxs.append(ctx)

    with patch.object(PipelineEngine, "process_message", new=fake_process):
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=False,
            config=config,
            store=mock_store,
        )
    assert len(captured_ctxs) == 1
    assert captured_ctxs[0].skip_push is False


async def test_run_specific_messages_empty_list_noop(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """空 ID 列表应该安全 no-op。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_specific_messages(
            msg_ids=[], from_phase=Phase.SUMMARIZED, skip_push=True,
            config=config, store=mock_store,
        )
        assert not mock_proc.called


async def test_run_specific_messages_skips_cleanup(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """手动模式不能调 cleanup，避免误删超 24h 的历史消息（D6）。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    with patch.object(mock_store, "cleanup") as mock_cleanup, \
         patch.object(PipelineEngine, "process_message", new=AsyncMock()):
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"], from_phase=Phase.SUMMARIZED, skip_push=True,
            config=config, store=mock_store,
        )
        assert not mock_cleanup.called


async def test_run_specific_messages_skips_incompatible_content_type(
    tmp_path: Path,
) -> None:
    """oracle Issue 3: TEXT 消息 reset 到 transcribed 应被跳过（PHASE_FLOW 不含）。"""
    store = MessageStore(tmp_path)
    store.add_new("weibo:W1", "weibo", ContentType.TEXT, int(time.time()), "T1", "A")
    store.mark_phase("weibo:W1", Phase.PUSHED)

    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    # TEXT 的 PHASE_FLOW = [DISCOVERED, DOWNLOADED, PUSHED]，没有 TRANSCRIBED
    # 但 reset_specific 仍会把 phase 设成 transcribed（reset_specific 不校验 content_type）
    # 这里要校验的是 run_specific_messages 进入 process 前的预检
    with patch.object(PipelineEngine, "process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_specific_messages(
            msg_ids=["weibo:W1"], from_phase=Phase.TRANSCRIBED, skip_push=True,
            config=config, store=store,
        )
        # 应被跳过（process_message 未被调用）
        assert not mock_proc.called


# ── 任务 5: 三平台 push handler skip_push 检查 ─────────────────────


async def test_bili_push_skips_when_skip_push_true(tmp_path: Path) -> None:
    """ctx.skip_push=True 时 bili_push 应跳过 send_to_subscription。"""
    from platforms.bilibili.handlers import bili_push

    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A", subscription_ref="42")
    msg = store.get_message("bili:BV1")
    assert msg is not None

    config = MagicMock()
    config.bilibili.subscriptions = [MagicMock(uid=42, notify_endpoints=["gotify"])]

    ctx = PhaseContext(msg=msg, config=config, skip_push=True)

    with patch("platforms.bilibili.handlers.send_to_subscription", new=AsyncMock()) as mock_send:
        result = await bili_push(ctx)
        assert result is True
        assert not mock_send.called


async def test_xhs_push_skips_when_skip_push_true(tmp_path: Path) -> None:
    """ctx.skip_push=True 时 xhs_push 应跳过 send_to_subscription。"""
    from platforms.xiaohongshu.handlers import xhs_push

    store = MessageStore(tmp_path)
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "T1", "A", subscription_ref="u1")
    msg = store.get_message("xhs:N1")
    assert msg is not None

    config = MagicMock()
    config.xiaohongshu.subscriptions = [MagicMock(user_id="u1", notify_endpoints=["gotify"])]

    ctx = PhaseContext(msg=msg, config=config, skip_push=True)

    with patch("platforms.xiaohongshu.handlers.send_to_subscription", new=AsyncMock()) as mock_send:
        result = await xhs_push(ctx)
        assert result is True
        assert not mock_send.called


async def test_weibo_push_skips_when_skip_push_true(tmp_path: Path) -> None:
    """ctx.skip_push=True 时 weibo_push 应跳过 send_to_subscription。"""
    from platforms.weibo.handlers import weibo_push

    store = MessageStore(tmp_path)
    store.add_new("weibo:W1", "weibo", ContentType.TEXT, int(time.time()), "T1", "A", subscription_ref="u1")
    msg = store.get_message("weibo:W1")
    assert msg is not None

    config = MagicMock()
    config.weibo.subscriptions = [MagicMock(user_id="u1", notify_endpoints=["gotify"])]

    ctx = PhaseContext(msg=msg, config=config, skip_push=True)

    with patch("platforms.weibo.handlers.send_to_subscription", new=AsyncMock()) as mock_send:
        result = await weibo_push(ctx)
        assert result is True
        assert not mock_send.called


# ── run_specific_messages log_callback 参数 (issue #71) ──────────


async def test_run_specific_messages_invokes_log_callback(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """run_specific_messages 应在 reset 前后通过 log_callback 发日志事件。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    events: list[tuple[str, str]] = []

    def cb(event_type: str, message: str) -> None:
        events.append((event_type, message))

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()):
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
            log_callback=cb,
        )
    # 至少触发了 log 事件（reset 开始 / 每条消息 / 完成）
    assert len(events) > 0
    # 所有事件类型应为 "log" 或 "done"
    assert all(et in ("log", "done") for et, _ in events)
    # 完成事件应包含 done 类型
    assert any(et == "done" for et, _ in events)


async def test_run_specific_messages_log_callback_none_default(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """log_callback=None（默认）应不报错（向后兼容现有 CLI 调用）。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()):
        # 不传 log_callback，应正常完成（默认 None）
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
        )


async def test_run_specific_messages_empty_list_with_callback(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """空 msg_ids 且有 callback：reset_specific 返回 0 时早退，仍发 done 事件。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    events: list[tuple[str, str]] = []
    cb = lambda et, m: events.append((et, m))  # noqa: E731

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_specific_messages(
            msg_ids=[],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
            log_callback=cb,
        )
        assert not mock_proc.called
    # 空列表也应发 done（让前端 SSE 能收到结束信号）
    assert any(et == "done" for et, _ in events)

