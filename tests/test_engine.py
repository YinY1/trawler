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
    """每个测试前重置 PipelineEngine 注册表，避免污染。

    同时清理 sys.modules 中缓存的平台 handler 模块：部分测试（如
    test_transcribe_phase_missing_filepath）依赖 import 时装饰器重新触发，
    若模块已被其他测试导入并缓存，装饰器不会再次执行。
    """
    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    for mod in list(sys.modules):
        if mod.startswith("platforms.") and mod.endswith(".handlers"):
            sys.modules.pop(mod, None)


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
    """If a handler returns False, flow should stop and error should be recorded.

    Engine 改造后失败走 retry_count 机制（< MAX 不写 error）。本测试预置
    retry_count = MAX-1，让单次失败就触发 mark_error，保持「失败 stops flow」的原
    断言语义（updated.error == "download failed"）。"""
    from shared.constants import MAX_SUMMARY_RETRIES

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
    # 预置 retry_count = MAX - 1，下一次失败即触发 mark_error
    for _ in range(MAX_SUMMARY_RETRIES - 1):
        store.mark_retry_failure("bili:BV1", "prev fail")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.DISCOVERED  # unchanged
    assert updated.error == "download failed"


@pytest.mark.asyncio
async def test_process_message_resume_from_mid_phase(config: Config, store: MessageStore) -> None:
    """Should resume from current phase, not repeat completed phases.

    Uses TEXT content: TEXT phase flow excludes TRANSCRIBED/SUMMARIZED,
    so the Bug-3 VIDEO-only rewind gate never fires here and this test keeps
    verifying the pure resume semantics."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "Test", "Author")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED

    await PipelineEngine.process_message(msg, config, store)
    assert calls == ["pushed"]  # only pushed, downloaded is not repeated


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


# ── summarize_phase failure semantics (plan 2026-06-28) ─────────


@pytest.mark.asyncio
async def test_summarize_phase_returns_false_on_analysis_failed(
    config: Config, store: MessageStore
) -> None:
    """AI 摘要 fallback 全失败时 summarize_phase 必须 return False。"""
    import sys
    from unittest.mock import AsyncMock, patch

    from core.summarizer import AnalysisResult

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401

        # mock analyze_content 返回 failed=True
        with patch(
            "platforms.bilibili.handlers.analyze_content",
            new=AsyncMock(return_value=AnalysisResult(source="none", failed=True)),
        ):
            handler = PipelineEngine._handlers.get(("*", Phase.SUMMARIZED))
            assert handler is not None

            msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
            assert msg is not None
            ctx = PhaseContext(msg=msg, config=config)
            ctx.transcript_text = "transcript 内容"  # 提供正文让 analyze_content 被调

            result = await handler(ctx)

        assert result is False
        assert "AI 摘要失败" in ctx.error or "摘要" in ctx.error
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)


@pytest.mark.asyncio
async def test_summarize_phase_returns_true_when_analysis_succeeds(
    config: Config, store: MessageStore
) -> None:
    """analyze_content 成功（failed=False）时 summarize_phase 返回 True。

    覆盖：LLM 配置 disabled → analyze_content 返回 source='none' failed=False。
    """
    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401

        config.analysis.enabled = False  # analyze_content 返回 source='none', failed=False

        handler = PipelineEngine._handlers.get(("*", Phase.SUMMARIZED))
        assert handler is not None

        msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
        assert msg is not None
        ctx = PhaseContext(msg=msg, config=config)

        result = await handler(ctx)

        assert result is True  # 关键：分析成功（非 failed）就推进
        assert ctx.error == ""
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)


# ── engine retry_count handling (plan 2026-06-28) ───────────────


@pytest.mark.asyncio
async def test_handler_failure_increments_retry_count(
    config: Config, store: MessageStore
) -> None:
    """handler 返回 False 且 retry_count < MAX 时：retry_count += 1，不写 error，cron 仍重试。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.error = "download 失败"
        return False

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        pytest.fail("PUSHED 不应被调用")

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    # TEXT flow=[DISCOVERED, DOWNLOADED, PUSHED]：handler 失败时 next_phase=DOWNLOADED 未推进
    assert updated.phase == Phase.DISCOVERED
    assert updated.retry_count == 1
    assert updated.last_error == "download 失败"
    assert updated.error == ""  # 关键：未达上限，不写 error


@pytest.mark.asyncio
async def test_handler_failure_after_max_retries_marks_error(
    config: Config, store: MessageStore
) -> None:
    """retry_count 达到 MAX_SUMMARY_RETRIES 后：写 error，cron 永久跳过。"""
    from shared.constants import MAX_SUMMARY_RETRIES

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.error = "download 失败"
        return False

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    # 预置 retry_count = MAX - 1，下一次失败应触发 mark_error
    for _ in range(MAX_SUMMARY_RETRIES - 1):
        store.mark_retry_failure("bili:BV1", "prev fail")
    pre = store.get_message("bili:BV1")
    assert pre is not None
    assert pre.retry_count == MAX_SUMMARY_RETRIES - 1

    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.DISCOVERED  # TEXT flow 中 DOWNLOADED 未推进
    assert updated.error != ""  # 关键：达到上限，写 error
    assert "download 失败" in updated.error
    # 注：mark_error 不增加 retry_count（mark_error 只写 error 字段）。
    # engine 的「达到上限」检查用 current_count+1 >= MAX，触发后直接 mark_error，
    # 所以 retry_count 仍为预置的 MAX-1（最后一次失败的计数未写入）。
    assert updated.retry_count == MAX_SUMMARY_RETRIES - 1


@pytest.mark.asyncio
async def test_handler_permanent_error_marks_error_immediately(
    config: Config, store: MessageStore
) -> None:
    """Issue 6: handler 标记 ctx.permanent_error=True 时直接 mark_error，跳过 retry。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.downloaded_filepath = Path("/tmp/fake.mp4")
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tc(ctx: PhaseContext) -> bool:
        ctx.error = "transcribe 文件路径缺失"
        ctx.permanent_error = True  # 关键：标记永久失败
        return False

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
    assert msg is not None
    # 即便 retry_count = 0（远未达上限），permanent_error 也立即触发 mark_error
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    # DOWNLOADED 推进成功，TRANSCRIBED handler 失败 → 停在 DOWNLOADED（next=TRANSCRIBED 未推进）
    assert updated.phase == Phase.DOWNLOADED
    assert updated.error != ""  # 关键：直接 mark_error，不等 retry
    assert "transcribe 文件路径缺失" in updated.error
    assert updated.retry_count == 0  # 关键：未走 retry 路径，retry_count 不增


@pytest.mark.asyncio
async def test_handler_success_resets_retry_count(
    config: Config, store: MessageStore
) -> None:
    """handler 成功后 retry_count 必须重置为 0（之前失败过的消息恢复后清状态）。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.content_text = "成功正文"
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    store.mark_retry_failure("bili:BV1", "prev fail")
    store.mark_retry_failure("bili:BV1", "prev fail")
    pre = store.get_message("bili:BV1")
    assert pre is not None
    assert pre.retry_count == 2

    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED
    assert updated.retry_count == 0  # 重置
    assert updated.last_error == ""


# ── bili_download handler permanent passthrough (Issue #47) ─────


@pytest.mark.asyncio
async def test_bili_download_handler_passthrough_permanent_to_engine(
    config: Config, store: MessageStore
) -> None:
    """Issue #47: shared/downloader.py 标记 ``permanent=True`` 的下载失败
    必须由 bili_download handler 透传到 ``ctx.permanent_error``，
    进而被 engine 直接 mark_error（不增 retry_count）。

    与 ``test_handler_permanent_error_marks_error_immediately`` 的区别：
    后者直接在测试 handler 内手动设 ``ctx.permanent_error=True``，本测试
    透过真实 bili_download handler 验证「result.permanent → ctx.permanent_error」
    的透传链路完整。
    """
    import sys
    from unittest.mock import AsyncMock, patch

    from shared.protocols import DownloadResult

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401

        # mock download_video 返回 permanent=True 失败（如凭证缺失）
        fail_result = DownloadResult(
            success=False,
            source_id="BV1",
            title="T",
            error="B站未配置登录凭证",
            permanent=True,
        )
        with patch(
            "shared.downloader.download_video",
            new=AsyncMock(return_value=fail_result),
        ):
            # bili_download 是模块级装饰器注册的 handler；从注册表取出调用
            handler = PipelineEngine._handlers.get(("bili", Phase.DOWNLOADED))
            assert handler is not None, "bili_download handler should be registered"

            msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
            assert msg is not None
            ctx = PhaseContext(msg=msg, config=config)

            result = await handler(ctx)

        # handler 层断言：透传成功
        assert result is False
        assert ctx.permanent_error is True  # 关键：透传
        assert "凭证" in (ctx.error or "")

        # engine 层断言：permanent_error 触发直接 mark_error（retry_count 不增）
        msg = store.get_message("bili:BV1")
        assert msg is not None
        await PipelineEngine.process_message(msg, config, store)

        updated = store.get_message("bili:BV1")
        assert updated is not None
        assert updated.phase == Phase.DISCOVERED  # DOWNLOADED 未推进
        assert updated.error != ""  # 直接 mark_error
        assert "凭证" in updated.error
        assert updated.retry_count == 0  # 未走 retry 路径
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)


@pytest.mark.asyncio
async def test_bili_download_handles_dynamic_text_prefix(
    config: Config, store: MessageStore
) -> None:
    """bili_dyn: 前缀的 TEXT 消息走到 DOWNLOADED 时,bili_download 应 no-op return True
    并把 msg.body 复制到 ctx.content_text,让 push 阶段能拿到正文 (plan D3)。

    背景:detector 把纯文字动态注册为 bili_dyn:{id} + TEXT,TEXT flow 含 DOWNLOADED。
    若 bili_download 不特判,会用 msg_id.replace('bili:', '') 切出错误 bvid='dyn:xxx',
    调 download_video 必然失败,消息卡死在 DOWNLOADED。
    """
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    # 重新注册真实的 bili_download(不依赖模块导入副作用)
    from platforms.bilibili.handlers import bili_download

    PipelineEngine._handlers[("bili", Phase.DOWNLOADED)] = bili_download

    msg = store.add_new(
        msg_id="bili_dyn:12345",
        platform="bili",
        content_type=ContentType.TEXT,
        pubdate=2000000000,
        title="纯文字动态",
        author="UP1",
    )
    assert msg is not None
    # 模拟 detector 阶段已写入的动态正文
    store.mark_body("bili_dyn:12345", "动态的完整正文内容")

    # 重新读出含 body 的 msg
    msg = store.get_message("bili_dyn:12345")
    assert msg is not None
    ctx = PhaseContext(msg=msg, config=config)

    result = await bili_download(ctx)

    assert result is True
    # 关键:body 被复制到 content_text,push 阶段直接读 ctx.content_text
    assert ctx.content_text == "动态的完整正文内容"


@pytest.mark.asyncio
async def test_text_message_never_reaches_transcribe_phase(
    config: Config, store: MessageStore
) -> None:
    """TEXT flow 不含 TRANSCRIBED: engine 不会调用 transcribe handler。

    验证 PHASE_FLOW[TEXT] 简化后,即使 transcribe_phase 移除 content_type 特判,
    TEXT 消息也走不到 TRANSCRIBED 阶段(由 PHASE_FLOW 保证,而非 handler 特判)。
    """
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        return True

    @PipelineEngine.register("*", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        pytest.fail("TRANSCRIBED handler 不应被 TEXT 消息调用")

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("bili_dyn:t1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili_dyn:t1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED
