"""Tests for shared/constants.py version constants (issue #55)."""

from __future__ import annotations

import importlib.metadata
from unittest.mock import patch


def test_version_uses_package_metadata():
    """VERSION 等于 importlib.metadata 读到的 dist 版本。"""
    from shared.constants import VERSION

    expected = importlib.metadata.version("trawler")
    assert VERSION == expected


def test_version_fallback_when_not_installed(monkeypatch):
    """未安装 trawler dist 时 VERSION fallback '0.0.0+unknown'.

    直接跑源码（未 ``uv pip install -e .``）的场景，importlib.metadata.version
    会抛 PackageNotFoundError，constants.py 必须 catch 后 fallback.
    """
    import importlib
    from importlib.metadata import PackageNotFoundError

    with patch(
        "importlib.metadata.version",
        side_effect=PackageNotFoundError("trawler"),
    ):
        from shared import constants as constants_mod

        importlib.reload(constants_mod)
        assert constants_mod.VERSION == "0.0.0+unknown"


def test_git_sha_defaults_to_dev_when_env_missing(monkeypatch):
    """未注入 ENV 时 GIT_SHA == 'dev'."""
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    # 重新 import 模块以触发模块级求值
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.GIT_SHA == "dev"


def test_git_sha_reads_env_when_present(monkeypatch):
    """注入 ENV 后 GIT_SHA 反映 ENV 值。"""
    monkeypatch.setenv("TRAWLER_GIT_SHA", "a1b2c3d")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.GIT_SHA == "a1b2c3d"


def test_git_sha_falls_back_when_env_empty_string(monkeypatch):
    """ENV 存在但值为空字符串时 fallback 'dev'.

    Dockerfile ``ARG GIT_SHA=""`` 未传 build-arg 时 ENV 存在但为空,
    直接 ``os.environ.get(..., 'dev')`` 会返回 '' 而非 'dev',
    导致 VERSION_DISPLAY 出现孤零零的 '+' 号。需用 ``or`` 短路兜底。
    """
    monkeypatch.setenv("TRAWLER_GIT_SHA", "")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.GIT_SHA == "dev"


def test_build_date_defaults_to_unknown_when_env_missing(monkeypatch):
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.BUILD_DATE == "unknown"


def test_build_date_reads_env_when_present(monkeypatch):
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-06-30T14:29:00Z")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.BUILD_DATE == "2026-06-30T14:29:00Z"


def test_build_date_falls_back_when_env_empty_string(monkeypatch):
    """ENV 存在但值为空字符串时 fallback 'unknown' (同 GIT_SHA 空串边界)."""
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    assert constants_mod.BUILD_DATE == "unknown"


def test_version_display_format_contains_all_parts(monkeypatch):
    """VERSION_DISPLAY 形如 ``<version>+<git_sha> (<build_date>)``."""
    monkeypatch.setenv("TRAWLER_GIT_SHA", "a1b2c3d")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-06-30T14:29:00Z")
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    vd = constants_mod.VERSION_DISPLAY
    # 包含 dist version、git sha、build date 三段
    assert "a1b2c3d" in vd
    assert "2026-06-30T14:29:00Z" in vd
    assert "+" in vd
    assert "(" in vd and ")" in vd


def test_version_display_dev_fallback(monkeypatch):
    """未注入 ENV 时 VERSION_DISPLAY 仍可读，含 'dev' 和 'unknown'."""
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    import importlib

    from shared import constants as constants_mod

    importlib.reload(constants_mod)
    vd = constants_mod.VERSION_DISPLAY
    assert "dev" in vd
    assert "unknown" in vd
