"""微博内容监控模块 — 已迁移至 handlers.py + PipelineEngine

此模块保留为兼容性 re-export。
"""

from __future__ import annotations

from platforms.weibo.api import fetch_user_posts  # noqa: F401 - re-export