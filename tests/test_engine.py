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
    """Should resume from current phase, not repeat completed phases.

    Uses DYNAMIC content: DYNAMIC phase flow excludes DOWNLOADED/TRANSCRIBED,
    so the Bug-3 VIDEO-only rewind gate never fires here and this test keeps
    verifying the pure resume semantics."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        calls.append("summarized")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "Test", "Author")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.SUMMARIZED

    await PipelineEngine.process_message(msg, config, store)
    assert calls == ["pushed"]  # only pushed, summarized is not repeated


# ── run_platform ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_platform_detect_and_process(config: Config, tmp_path: Path) -> None:
    """run_platform should run detector then process pending messages."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    detected = False

    @PipelineEngine.register_detector("test_platform")
    async def bili_detector(cfg: Config, st: MessageStore) -> None:
        nonlocal detected  # noqa: F824
        detected = True
        st.add_new("test:001", "test_platform", ContentType.TEXT, 2000000000, "T", "A")

    @PipelineEngine.register("test_platform", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        return True

    @PipelineEngine.register("test_platform", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test_platform")

    assert detected
    store2 = MessageStore(tmp_path)
    assert store2.is_known("test:001")
    msg = store2.get_message("test:001")
    assert msg is not None
    assert msg.phase == Phase.PUSHED


@pytest.mark.asyncio
async def test_process_message_video_missing_filepath_rewinds_to_discovered(
    config: Config, store: MessageStore
) -> None:
    """Bug 3 fix: a VIDEO message stuck at DOWNLOADED with no filepath
    (cross-process state loss) should auto-rewind to DISCOVERED and re-run
    the full download phase."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        ctx.downloaded_filepath = Path("/tmp/fake_video.mp4")  # simulate real download
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        calls.append("transcribed")
        # Filepath is now set by re-download
        assert ctx.downloaded_filepath is not None
        return True

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        calls.append("summarized")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    # Seed a message stuck at DOWNLOADED (phase persisted, filepath lost)
    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None

    # New PhaseContext starts with downloaded_filepath=None (the bug scenario)
    await PipelineEngine.process_message(msg, config, store)

    # Auto-rewind should have re-run DOWNLOADED then proceeded
    assert "downloaded" in calls
    assert calls == ["downloaded", "transcribed", "summarized", "pushed"]
    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED


@pytest.mark.asyncio
async def test_transcribe_phase_missing_filepath_returns_false_with_error(
    config: Config, store: MessageStore, tmp_path: Path
) -> None:
    """Bug 3 fix: transcribe_phase with filepath=None must set ctx.error and
    return False (no silent success → no empty push).

    Note: this test imports ``platforms.bilibili.handlers`` to access the real
    ``transcribe_phase``. Python caches the module after first import, which
    would break ``test_platform_handlers.py::test_bili_module_imports`` (its
    "import → assert registered" logic relies on import side effects firing
    every time). We work around this by removing the module from ``sys.modules``
    in a finally block so the next test that imports it re-triggers the
    decorators. Only the handlers module itself is removed — its many
    third-party dependencies stay cached, so re-import is cheap."""
    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401

        # Find the registered "*" / TRANSCRIBED handler
        handler = PipelineEngine._handlers.get(("*", Phase.TRANSCRIBED))
        assert handler is not None, "transcribe_phase should be registered"

        msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
        assert msg is not None
        ctx = PhaseContext(msg=msg, config=config)
        ctx.downloaded_filepath = None  # the bug scenario

        result = await handler(ctx)

        assert result is False
        assert "downloaded_filepath missing" in ctx.error
    finally:
        # Drop the handlers module from cache so later tests that import it
        # (e.g. test_platform_handlers.py) see the decorators re-fire.
        sys.modules.pop("platforms.bilibili.handlers", None)


# ── body/summary flush (plan 2026-06-25 D5) ─────────────────────


@pytest.mark.asyncio
async def test_process_message_flushes_body_after_download(config: Config, store: MessageStore) -> None:
    """DOWNLOADED 阶段 handler 设置 ctx.content_text 后，engine 必须 flush 到 store.body。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("xhs", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.content_text = "正文内容"
        return True

    @PipelineEngine.register("xhs", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("xhs:note1", "xhs", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("xhs:note1")
    assert updated is not None
    assert updated.body == "正文内容"


@pytest.mark.asyncio
async def test_process_message_flushes_summary_after_summarized(config: Config, store: MessageStore) -> None:
    """SUMMARIZED 阶段 handler 设置 ctx.summary_text 后，engine 必须 flush 到 store.summary。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.downloaded_filepath = Path("/tmp/fake.mp4")
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        return True

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        ctx.summary_text = "AI 摘要内容"
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.summary == "AI 摘要内容"


@pytest.mark.asyncio
async def test_process_message_flushes_inline_summary_after_downloaded(config: Config, store: MessageStore) -> None:
    """覆盖 weibo 内联摘要路径（F6/R2/D5）：DOWNLOADED handler 内直接设置
    ctx.summary_text，流程不经过 SUMMARIZED 阶段（TEXT 类型）。
    engine 集中 flush 必须在 DOWNLOADED 后也捞 summary。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("weibo", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.content_text = "微博正文"
        ctx.summary_text = "内联摘要"  # 模拟 weibo download handler 内联生成摘要
        return True

    @PipelineEngine.register("weibo", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("weibo:post1", "weibo", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("weibo:post1")
    assert updated is not None
    # body 来自 content_text
    assert updated.body == "微博正文"
    # summary 来自内联摘要（关键：DOWNLOADED 阶段也要 flush summary）
    assert updated.summary == "内联摘要"


@pytest.mark.asyncio
async def test_process_message_flush_truncates_long_body(config: Config, store: MessageStore) -> None:
    """body 超过 5000 字必须截断并加省略号（plan D3）。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    long_text = "X" * 6000

    @PipelineEngine.register("xhs", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.content_text = long_text
        return True

    @PipelineEngine.register("xhs", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("xhs:note2", "xhs", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("xhs:note2")
    assert updated is not None
    assert len(updated.body) == 5001  # 5000 字 + "…"
