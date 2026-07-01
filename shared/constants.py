"""Trawler 全局常量"""

from __future__ import annotations

import os
import subprocess
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# 版本信息（issue #55 + #73 dev fallback 链）
#
# Fallback 优先级：
#   1. Env vars（Docker 构建 ARG 注入：TRAWLER_GIT_SHA / TRAWLER_BUILD_DATE）
#   2. importlib.metadata（已 ``uv pip install -e .``）
#   3. pyproject.toml + git 子进程 推断 dev 版本串
#      （形如 ``0.1.0+dev.abc1234``）
#   4. ``"0.0.0+dev"`` / ``"dev"`` / ``"unknown"`` 最终兜底
#
# VERSION_DISPLAY: 统一展示字符串，形如 `0.1.0+dev.abc1234 (2026-06-30 14:29:00 +0800)`
# ═══════════════════════════════════════════════════════════

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_git(*args: str) -> str | None:
    """跑 ``git`` 子进程，失败/超时/不存在返回 ``None``。

    timeout=2s 避免 git 卡死拖慢模块导入；捕获 ``FileNotFoundError``
    让无 git 环境（如 Docker 镜像仅含 python）也能正常降级。
    """
    try:
        proc = subprocess.run(
            ("git", *args),
            cwd=_PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _read_pyproject_version() -> str:
    """从 ``pyproject.toml`` 直读 ``project.version``，缺失/非法返回 ``"0.0.0"``。"""
    pyproject = _PROJECT_ROOT / "pyproject.toml"
    if not pyproject.exists():
        return "0.0.0"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return "0.0.0"
    ver = data.get("project", {}).get("version")
    return ver if isinstance(ver, str) and ver else "0.0.0"


def _get_version() -> str:
    """按优先级返回 VERSION：env → dist metadata → pyproject+git → dev 兜底。"""
    if env_v := os.environ.get("TRAWLER_VERSION"):
        return env_v
    try:
        return _dist_version("trawler")
    except PackageNotFoundError:
        pass
    pkg_ver = _read_pyproject_version()
    short_sha = _run_git("rev-parse", "--short", "HEAD")
    if short_sha:
        return f"{pkg_ver}+dev.{short_sha}"
    return f"{pkg_ver}+dev"


def _get_git_sha() -> str:
    """按优先级返回 GIT_SHA：env → git short sha → 'dev' 兜底。"""
    if sha := os.environ.get("TRAWLER_GIT_SHA"):
        return sha
    return _run_git("rev-parse", "--short", "HEAD") or "dev"


def _get_build_date() -> str:
    """按优先级返回 BUILD_DATE：env → 最近一次 commit 时间 → 'unknown' 兜底。"""
    if d := os.environ.get("TRAWLER_BUILD_DATE"):
        return d
    return _run_git("log", "-1", "--format=%ci") or "unknown"


VERSION: str = _get_version()
GIT_SHA: str = _get_git_sha()
BUILD_DATE: str = _get_build_date()
VERSION_DISPLAY: str = f"{VERSION}+{GIT_SHA} ({BUILD_DATE})"

# 超时（秒）
DOWNLOAD_TIMEOUT = 600  # yt-dlp 下载超时
LLM_API_TIMEOUT = 60  # OpenAI 兼容 API 超时
GOTIFY_TIMEOUT = 10  # Gotify 推送超时
RSS_REQUEST_TIMEOUT = 15  # RSS 请求超时
XHS_REQUEST_TIMEOUT = 15  # 小红书 API 请求超时
XHS_DOWNLOAD_TIMEOUT = 120  # 小红书文件下载超时

WEIBO_REQUEST_TIMEOUT = 15  # 微博 API 请求超时
WEIBO_DOWNLOAD_TIMEOUT = 120  # 微博文件下载超时
WEIBO_POLL_TIMEOUT = 240  # 二维码轮询超时（秒）

# 重试
GOTIFY_MAX_RETRIES = 3  # Gotify 最大重试次数

# 评论
MAX_COMMENT_HIGHLIGHTS = 5  # 最大评论亮点数量

# AI 摘要重试上限（连续失败 N 次后 mark_error 让 cron 永久跳过）
MAX_SUMMARY_RETRIES = 5
