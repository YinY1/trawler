"""Tests for shared/constants.py version/sha/date fallback chain (issue #73).

注意：``importlib.reload`` 会重跑整个模块顶层代码，把所有模块级名字（包括
``_run_git``/``_dist_version``）重新绑定到原始函数，导致 ``patch.object``
的 mock 失效。所以这里的场景测试直接调用 ``_get_version`` / ``_get_git_sha`` /
``_get_build_date`` helper，它们内部用 ``_run_git`` / ``_dist_version`` /
``_read_pyproject_version`` 计算结果，与模块级常量等价。
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch as _patch_fn


def _force_package_not_found():
    """让 ``constants._dist_version("trawler")`` 抛 PackageNotFoundError.

    必须 ``patch.object(constants, "_dist_version", ...)`` 让 mock 直接落在
    模块级引用上；patching ``importlib.metadata.version`` 全局属性不会影响
    ``import as`` 已经 binding 的名字.
    """
    from importlib.metadata import PackageNotFoundError

    from shared import constants as constants_mod

    return _patch_fn.object(
        constants_mod,
        "_dist_version",
        side_effect=PackageNotFoundError("trawler"),
    )


# ═══════════════════════════════════════════════════════════
# _run_git helper
# ═══════════════════════════════════════════════════════════


def test_run_git_returns_none_when_git_missing():
    """无 git 可执行文件时 ``_run_git`` 返回 ``None``。"""
    from shared import constants as constants_mod

    def fake_run(*_args, **_kw):
        raise FileNotFoundError("git not found")

    with _patch_fn("subprocess.run", side_effect=fake_run):
        assert constants_mod._run_git("rev-parse", "--short", "HEAD") is None


def test_run_git_returns_none_on_nonzero_exit():
    """git 返回非零退出码时 ``_run_git`` 返回 ``None``。"""
    from shared import constants as constants_mod

    class FakeResult:
        returncode = 128
        stdout = ""

    with _patch_fn("subprocess.run", return_value=FakeResult()):
        assert constants_mod._run_git("rev-parse", "--short", "HEAD") is None


def test_run_git_returns_none_on_timeout():
    """git 超时（2s）时 ``_run_git`` 返回 ``None``。"""
    from shared import constants as constants_mod

    def fake_run(*_args, **_kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    with _patch_fn("subprocess.run", side_effect=fake_run):
        assert constants_mod._run_git("rev-parse", "--short", "HEAD") is None


def test_run_git_strips_stdout():
    """git 成功时返回 strip 过的 stdout。"""
    from shared import constants as constants_mod

    class FakeResult:
        returncode = 0
        stdout = "  abc1234\n"

    with _patch_fn("subprocess.run", return_value=FakeResult()):
        assert constants_mod._run_git("rev-parse", "--short", "HEAD") == "abc1234"


# ═══════════════════════════════════════════════════════════
# _read_pyproject_version
# ═══════════════════════════════════════════════════════════


def test_read_pyproject_version_returns_real_version():
    """项目根 pyproject.toml 存在且合法,返回真实版本号。

    断言语义（PEP 440 版本号格式）而非硬编码值，避免每次 bump 版本号都破坏测试。
    """
    import re

    from shared import constants as constants_mod

    ver = constants_mod._read_pyproject_version()
    # PEP 440 简化格式：X.Y.Z（可能带 .devN / .aN / +build 等后缀，这里只要求主版本号部分）
    assert re.match(r"^\d+\.\d+\.\d+", ver), f"unexpected version format: {ver}"


def test_read_pyproject_version_returns_zero_when_missing(monkeypatch, tmp_path):
    """pyproject.toml 不存在时返回 ``0.0.0``。"""
    from shared import constants as constants_mod

    monkeypatch.setattr(constants_mod, "_PROJECT_ROOT", tmp_path)
    assert constants_mod._read_pyproject_version() == "0.0.0"


def test_read_pyproject_version_returns_zero_when_corrupt(monkeypatch, tmp_path):
    """pyproject.toml 非法（无法 TOML 解析）时返回 ``0.0.0``。"""
    from shared import constants as constants_mod

    (tmp_path / "pyproject.toml").write_text("this is not valid toml ::: @@@\n")
    monkeypatch.setattr(constants_mod, "_PROJECT_ROOT", tmp_path)
    assert constants_mod._read_pyproject_version() == "0.0.0"


# ═══════════════════════════════════════════════════════════
# 场景 1：无 env vars + 无 git repo → fallback 到 0.0.0+dev / unknown
# ═══════════════════════════════════════════════════════════


def test_no_env_no_git_falls_back_to_zero_and_unknown(monkeypatch, tmp_path):
    """无 env vars 且 git 不可用 → VERSION 含 ``0.0.0``, BUILD_DATE=``unknown``。

    对应场景：``pyproject.toml`` 不存在（非项目根/未安装）且 git 没装/超时。
    通过 mock ``_run_git`` 直接返回 ``None``。
    """
    from shared import constants as constants_mod

    monkeypatch.delenv("TRAWLER_VERSION", raising=False)
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    monkeypatch.setattr(constants_mod, "_PROJECT_ROOT", tmp_path)
    # short-sha cache 是模块级，import 时已 resolve，必须 reset 才能让 mock 生效
    monkeypatch.setattr(constants_mod, "_short_sha_resolved", False)
    monkeypatch.setattr(constants_mod, "_short_sha_cache", None)

    with (
        _patch_fn.object(constants_mod, "_run_git", return_value=None) as mock_git,
        _force_package_not_found(),
    ):
        ver = constants_mod._get_version()
        sha = constants_mod._get_git_sha()
        date = constants_mod._get_build_date()
        assert "0.0.0" in ver
        assert ver.endswith("+dev")
        assert sha == "dev"
        assert date == "unknown"
        # _run_git 调用 2 次：_get_version/_get_git_sha 共用 _get_short_sha（cache 后 1 次）
        # + _get_build_date 的 git log（1 次）
        assert mock_git.call_count == 2


# ═══════════════════════════════════════════════════════════
# 场景 2：本地 git repo → VERSION 含 pyproject 版本 + dev.短 SHA
# ═══════════════════════════════════════════════════════════


def test_local_git_repo_version_includes_pyproject_and_sha(monkeypatch):
    """本地未安装但 git 可用 → VERSION = ``<pyproject>+dev.<short_sha>``.

    断言拼接格式（动态读 pyproject 版本），避免每次 bump 版本号都破坏测试。
    """
    from shared import constants as constants_mod

    monkeypatch.delenv("TRAWLER_VERSION", raising=False)
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    # short-sha cache 是模块级，import 时已 resolve，必须 reset 才能让 mock 生效
    monkeypatch.setattr(constants_mod, "_short_sha_resolved", False)
    monkeypatch.setattr(constants_mod, "_short_sha_cache", None)
    with (
        _patch_fn.object(constants_mod, "_run_git", return_value="abc1234"),
        _force_package_not_found(),
    ):
        expected = f"{constants_mod._read_pyproject_version()}+dev.abc1234"
        assert constants_mod._get_version() == expected
        assert constants_mod._get_git_sha() == "abc1234"


def test_local_git_repo_version_with_real_dist(monkeypatch):
    """已安装 trawler dist + git 可用 → VERSION 等于 dist 版本（git 不参与）。"""
    import importlib.metadata

    from shared import constants as constants_mod

    monkeypatch.delenv("TRAWLER_VERSION", raising=False)
    monkeypatch.delenv("TRAWLER_GIT_SHA", raising=False)
    monkeypatch.delenv("TRAWLER_BUILD_DATE", raising=False)
    with _patch_fn.object(constants_mod, "_run_git", return_value="abc1234"):
        assert constants_mod._get_version() == importlib.metadata.version("trawler")


# ═══════════════════════════════════════════════════════════
# 场景 3：有 env vars 时优先用 env（优先级最高）
# ═══════════════════════════════════════════════════════════


def test_env_vars_take_precedence_over_everything(monkeypatch):
    """env vars 注入优先级最高 → 完全跳过 importlib/git 分支。"""
    from shared import constants as constants_mod

    monkeypatch.setenv("TRAWLER_VERSION", "9.9.9-release")
    monkeypatch.setenv("TRAWLER_GIT_SHA", "env-sha-xyz")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2099-01-01T00:00:00Z")

    fail = AssertionError("_run_git 不应被调用当 env 注入完整")
    with _patch_fn.object(constants_mod, "_run_git", side_effect=fail) as mock_git:
        assert constants_mod._get_version() == "9.9.9-release"
        assert constants_mod._get_git_sha() == "env-sha-xyz"
        assert constants_mod._get_build_date() == "2099-01-01T00:00:00Z"
        mock_git.assert_not_called()


def test_env_empty_string_falls_back(monkeypatch):
    """env 传空字符串时按 fallback 链继续尝试（不短路为 ``dev``/``unknown``）。"""
    from shared import constants as constants_mod

    monkeypatch.setenv("TRAWLER_GIT_SHA", "")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "")
    monkeypatch.setenv("TRAWLER_VERSION", "")
    # short-sha cache 是模块级，import 时已 resolve，必须 reset 才能让 mock 生效
    monkeypatch.setattr(constants_mod, "_short_sha_resolved", False)
    monkeypatch.setattr(constants_mod, "_short_sha_cache", None)
    with _patch_fn.object(constants_mod, "_run_git", return_value="fakesha"):
        assert constants_mod._get_git_sha() == "fakesha"


# ═══════════════════════════════════════════════════════════
# VERSION_DISPLAY
# ═══════════════════════════════════════════════════════════


def test_version_display_format_contains_all_parts(monkeypatch):
    """VERSION_DISPLAY 形如 ``<version>+<git_sha> (<build_date>)``。"""
    monkeypatch.setenv("TRAWLER_VERSION", "0.1.0")
    monkeypatch.setenv("TRAWLER_GIT_SHA", "abc7def")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-06-30T14:29:00Z")
    import importlib

    mod = importlib.reload(__import__("shared.constants", fromlist=[""]))
    vd = mod.VERSION_DISPLAY
    assert "abc7def" in vd
    assert "2026-06-30T14:29:00Z" in vd
    assert "+" in vd
    assert "(" in vd and ")" in vd
    # 守卫：畸形串（双 + 号 / sha 重复）必须被捕获
    assert vd.count("+") == 1, f"VERSION_DISPLAY 含多个 +：{vd!r}"
    assert "abc7defabc7def" not in vd, f"sha 重复：{vd!r}"


def test_version_display_no_duplicate_sha_in_dev(monkeypatch):
    """dev 分支 VERSION 已含 ``+dev.<sha>`` 时，VERSION_DISPLAY 不再拼 GIT_SHA。

    回归 PR #77 review：原 ``f"{VERSION}+{GIT_SHA} ..."`` 在 dev 分支会生成
    畸形串 ``0.1.0+dev.abc1234+abc1234 (...)``（双 + 号 + sha 重复）。
    """
    monkeypatch.setenv("TRAWLER_VERSION", "0.1.0+dev.abc1234")
    monkeypatch.setenv("TRAWLER_GIT_SHA", "abc1234")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-07-01T00:00:00Z")
    import importlib

    mod = importlib.reload(__import__("shared.constants", fromlist=[""]))
    vd = mod.VERSION_DISPLAY
    # 只能有一个 + 号（VERSION 自带的那个）
    assert vd.count("+") <= 1, f"VERSION_DISPLAY 含多个 +：{vd!r}"
    # sha 不得重复
    assert "abc1234abc1234" not in vd, f"sha 重复：{vd!r}"
    # 期望格式：VERSION (BUILD_DATE)
    assert vd == "0.1.0+dev.abc1234 (2026-07-01T00:00:00Z)"


def test_version_display_pure_version_includes_sha(monkeypatch):
    """正式构建 VERSION 是纯 PEP 440（无 ``+``）时，VERSION_DISPLAY 用 GIT_SHA 注入 local segment。"""
    monkeypatch.setenv("TRAWLER_VERSION", "0.1.0")
    monkeypatch.setenv("TRAWLER_GIT_SHA", "abc1234")
    monkeypatch.setenv("TRAWLER_BUILD_DATE", "2026-07-01T00:00:00Z")
    import importlib

    mod = importlib.reload(__import__("shared.constants", fromlist=[""]))
    vd = mod.VERSION_DISPLAY
    assert vd == "0.1.0+abc1234 (2026-07-01T00:00:00Z)"
    assert vd.count("+") == 1


def test_version_display_dev_renderable(monkeypatch):
    """dev 环境 fallback 下 VERSION_DISPLAY 仍可渲染。"""
    import importlib

    mod = importlib.reload(__import__("shared.constants", fromlist=[""]))
    assert mod.VERSION
    assert mod.GIT_SHA
    assert mod.BUILD_DATE
    assert "(" in mod.VERSION_DISPLAY and ")" in mod.VERSION_DISPLAY
