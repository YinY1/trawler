"""End-to-end integration test for PipelineEngine with mocked platform.

Tests the full pipeline flow: detect -> process_message -> phase transitions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.engine import PipelineEngine
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase


@pytest.fixture(autouse=True)
def clean_engine() -> None:
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}


@pytest.fixture
def config() -> Config:
    return Config()


# -- Happy path: detector -> all handlers succeed -----------------


@pytest.mark.asyncio
async def test_full_pipeline_happy_path(config: Config, tmp_path: Path) -> None:
    """Detector discovers a message -> all phase handlers called in order -> PUSHED."""
    call_order: list[str] = []

    @PipelineEngine.register_detector("test")
    async def test_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new(
            msg_id="test:001",
            platform="test",
            content_type=ContentType.TEXT,
            pubdate=2000000000,
            title="E2E Test",
            author="Tester",
        )

    @PipelineEngine.register("test", Phase.DOWNLOADED)
    async def test_download(ctx) -> bool:  # type: ignore[type-arg]
        call_order.append("downloaded")
        return True

    @PipelineEngine.register("test", Phase.PUSHED)
    async def test_push(ctx) -> bool:  # type: ignore[type-arg]
        call_order.append("pushed")
        return True

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test")

    assert call_order == ["downloaded", "pushed"]

    store2 = MessageStore(tmp_path)
    msg = store2.get_message("test:001")
    assert msg is not None
    assert msg.phase == Phase.PUSHED


# -- Error path: handler fails -> phase stops --------------------


@pytest.mark.asyncio
async def test_full_pipeline_handler_failure(config: Config, tmp_path: Path) -> None:
    """If a handler returns False, pipeline stops and error is recorded."""

    @PipelineEngine.register_detector("test")
    async def test_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new(
            msg_id="test:002",
            platform="test",
            content_type=ContentType.TEXT,
            pubdate=2000000000,
            title="Fail Test",
            author="Tester",
        )

    @PipelineEngine.register("test", Phase.DOWNLOADED)
    async def test_download(ctx) -> bool:  # type: ignore[type-arg]
        ctx.error = "download failed"
        return False

    @PipelineEngine.register("test", Phase.PUSHED)
    async def test_push(ctx) -> bool:  # type: ignore[type-arg]
        pytest.fail("should not be called after failure")

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test")

    store2 = MessageStore(tmp_path)
    msg = store2.get_message("test:002")
    assert msg is not None
    assert msg.phase == Phase.DISCOVERED  # unchanged
    assert msg.error == "download failed"


# -- Resume from mid-phase --------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_resume_from_phase(config: Config, tmp_path: Path) -> None:
    """Message already at DOWNLOADED should resume from there, not repeat."""
    call_order: list[str] = []

    @PipelineEngine.register_detector("test")
    async def test_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new(
            msg_id="test:003",
            platform="test",
            content_type=ContentType.TEXT,
            pubdate=2000000000,
            title="Resume Test",
            author="Tester",
        )
        st.mark_phase("test:003", Phase.DOWNLOADED)

    @PipelineEngine.register("test", Phase.DOWNLOADED)
    async def test_download(ctx) -> bool:  # type: ignore[type-arg]
        call_order.append("downloaded")
        return True

    @PipelineEngine.register("test", Phase.PUSHED)
    async def test_push(ctx) -> bool:  # type: ignore[type-arg]
        call_order.append("pushed")
        return True

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test")

    # DOWNLOADED handler should NOT be called (already at that phase)
    assert call_order == ["pushed"]