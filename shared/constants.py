"""Trawler 全局常量"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

# ═══════════════════════════════════════════════════════════
# 版本信息（issue #55）
#
# - VERSION: dist metadata 读取（pyproject.toml [project].version），构建期不变
#   未安装（直接跑源码）时 fallback '0.0.0+unknown'，避免 PackageNotFoundError
# - GIT_SHA / BUILD_DATE: Docker 构建 ARG 注入 ENV，本地 dev fallback 'dev'/'unknown'
#   不调用 git 子进程，避免引入 git 依赖 + 非 git 环境报错
# - VERSION_DISPLAY: 统一展示字符串，形如 `0.1.0+a1b2c3d (2026-06-30T14:29:00Z)`
# ═══════════════════════════════════════════════════════════
try:
    _dist_ver = _dist_version("trawler")
except PackageNotFoundError:
    _dist_ver = "0.0.0+unknown"
VERSION: str = _dist_ver
GIT_SHA: str = os.environ.get("TRAWLER_GIT_SHA", "dev")
BUILD_DATE: str = os.environ.get("TRAWLER_BUILD_DATE", "unknown")
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
