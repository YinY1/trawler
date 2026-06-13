"""Tests for PipelineEngine — decorator-based pipeline engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.engine import PipelineEngine
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext


@pytest.fixture(autouse=True)
def clean_engine_state() -> None:
    """每个测试前重置 PipelineEngine 注册表，避免污染。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path)


# ── Registration ────────────────────────────────────────────────


def test_register_handler() -> None:
    """@PipelineEngine.register should store handler in _handlers."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def mock_handler(ctx: PhaseContext) -> bool:
        return True

    assert ("bili", Phase.DOWNLOADED) in PipelineEngine._handlers


def test_register_detector() -> None:
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register_detector("bili")
    async def mock_detector(config: Config, store: MessageStore) -> None:
        pass

    assert "bili" in PipelineEngine._detectors


# ── process_message ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_message_full_flow(config: Config, store: MessageStore) -> None:
    """VIDEO message should go through all phases."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        calls.append("transcribed")
        return True

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        calls.append("summarized")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "Test", "Author")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    assert calls == ["downloaded", "transcribed", "summarized", "pushed"]
    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED


@pytest.mark.asyncio
async def test_process_message_text_skips_transcribe_summarize(config: Config, store: MessageStore) -> None:
    """TEXT message should only go through DOWNLOADED -> PUSHED."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("weibo", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("weibo", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new("weibo:123", "weibo", ContentType.TEXT, 2000000000, "Post", "Author")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    assert calls == ["downloaded", "pushed"]


@pytest.mark.asyncio
async def test_process_message_handler_failure_stops_flow(config: Config, store: MessageStore) -> None:
    """If a handler returns False, flow should stop and error should be recorded."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.error = "download failed"
        return False

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        pytest.fail("should not be called")

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "Test", "Author")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.DISCOVERED  # unchanged
    assert updated.error == "download failed"


@pytest.mark.asyncio
async def test_process_message_resume_from_mid_phase(config: Config, store: MessageStore) -> None:
    """Should resume from current phase, not repeat completed phases."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        calls.append("transcribed")
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "Test", "Author")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED

    await PipelineEngine.process_message(msg, config, store)
    assert calls == ["transcribed"]  # only transcribed, not downloaded


# ── run_platform ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_platform_detect_and_process(config: Config, tmp_path: Path) -> None:
    """run_platform should run detector then process pending messages."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    detected = False

    @PipelineEngine.register_detector("bili")
    async def bili_detector(cfg: Config, st: MessageStore) -> None:
        nonlocal detected  # noqa: F824
        detected = True
        st.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "bili")

    assert detected
    store2 = MessageStore(tmp_path)
    assert store2.is_known("bili:BV1")
    msg = store2.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.PUSHED
