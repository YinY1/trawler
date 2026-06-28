"""Tests for manual content check — run_specific_messages + skip_push behavior.

Plan: docs/superpowers/plans/2026-06-28-manual-content-check.md
"""

from __future__ import annotations

import pytest

from core.engine import PipelineEngine
from shared.protocols import ContentType, MessageRecord, Phase, PhaseContext


# autouse: 防止 run_specific_messages 触发的 handler 模块导入污染全局注册表
# （oracle review Issue 6）。
@pytest.fixture(autouse=True)
def clean_engine_state() -> None:
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}


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
