"""Tests that all platform handler modules can be imported without errors.

These tests verify that the @PipelineEngine.register decorators fire correctly
at import time, registering all phase handlers and detectors.
"""

from __future__ import annotations

import pytest

from core.engine import PipelineEngine
from shared.protocols import Phase


@pytest.fixture(autouse=True)
def _clean_engine_state() -> None:
    """每个测试前重置 PipelineEngine 注册表，避免污染。

    同时清理 sys.modules 中缓存的平台 handler 模块：本测试依赖 import 时
    装饰器重新触发，若模块已被其他测试（如 test_manual_check 的
    run_specific_messages）导入并缓存，装饰器不会再次执行。
    """
    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    for mod in list(sys.modules):
        if mod.startswith("platforms.") and mod.endswith(".handlers"):
            sys.modules.pop(mod, None)


def _check_registrations(platform: str, expected_phases: list[Phase]) -> None:
    """Verify all expected phases are registered for a platform."""
    for phase in expected_phases:
        assert (platform, phase) in PipelineEngine._handlers, f"Missing handler for {platform} / {phase}"


def _check_detector(platform: str) -> None:
    """Verify a detector is registered for the platform."""
    assert platform in PipelineEngine._detectors, f"Missing detector for {platform}"


# -- B站 handlers ------------------------------------------------


def test_bili_module_imports() -> None:
    """Importing bili handlers should register all decorators."""
    import platforms.bilibili.handlers  # noqa: F401

    _check_detector("bili")
    _check_registrations(
        "bili",
        [
            Phase.DOWNLOADED,
            Phase.PUSHED,
        ],
    )


# -- XHS handlers -------------------------------------------------


def test_xhs_module_imports() -> None:
    """Importing xhs handlers should register all decorators."""
    import platforms.xiaohongshu.handlers  # noqa: F401

    _check_detector("xhs")
    _check_registrations(
        "xhs",
        [
            Phase.DOWNLOADED,
            Phase.PUSHED,
        ],
    )


# -- Weibo handlers -----------------------------------------------


def test_weibo_module_imports() -> None:
    """Importing weibo handlers should register all decorators."""
    import platforms.weibo.handlers  # noqa: F401

    _check_detector("weibo")
    _check_registrations(
        "weibo",
        [
            Phase.DOWNLOADED,
            Phase.PUSHED,
        ],
    )
