from __future__ import annotations

import logging
from typing import Any, Callable

from shared.config import Config
from shared.protocols import CommentHighlight

# ── 跨平台评论链统一抽象 ──
# 各平台通过 @register("platform") 装饰器注册获取器，handler 只需一行调用:
#
#     from core.comments import fetch_comment_highlights
#     highlights = await fetch_comment_highlights(ctx.msg.platform, content_id, ctx.config)
#     ctx.comment_highlights = format_comment_highlights(highlights)
#
# 注册的函数签名为 fetch_highlights(content_id, config, **kwargs) -> list[CommentHighlight]，
# **kwargs 透传平台特定参数（如 author_user_id、max_count）。

logger = logging.getLogger(__name__)

# 评论获取器注册表: {platform: fetch_func}
_FETCHERS: dict[str, Callable[..., Any]] = {}

# 平台 → 评论模块路径映射（延迟导入）
_COMMENT_MODULES: dict[str, str] = {
    "bili": "platforms.bilibili.comments",
    "xhs": "platforms.xiaohongshu.comments",
    "weibo": "platforms.weibo.comments",
}


def register(platform: str) -> Callable:
    """装饰器：注册某平台的评论获取器。

    被装饰的函数必须接受 ``(content_id: str, config: Config, **kwargs)`` 并返回
    ``list[CommentHighlight]``。

    Usage::

        @register("xhs")
        async def fetch_highlights(content_id: str, config: Config, **kwargs) -> list[CommentHighlight]:
            ...
    """

    def decorator(func: Callable) -> Callable:
        _FETCHERS[platform] = func
        return func

    return decorator


async def fetch_comment_highlights(
    platform: str,
    content_id: str,
    config: Config,
    **kwargs: Any,
) -> list[CommentHighlight]:
    """统一入口：获取评论亮点。

    自动延迟导入平台模块，按需注册。无需手动注册。

    Args:
        platform: 平台标识 ("bili" | "xhs" | "weibo")
        content_id: 内容 ID (bvid / note_id / post_id)
        config: 全局配置
        **kwargs: 平台特定参数（如 author_user_id, max_count）

    Returns:
        评论亮点列表
    """
    if platform not in _FETCHERS:
        _load_platform(platform)
    fetcher = _FETCHERS.get(platform)
    if fetcher is None:
        logger.warning("No comment fetcher registered for platform: %s", platform)
        return []
    return await fetcher(content_id, config, **kwargs)


def _load_platform(platform: str) -> None:
    """延迟导入平台评论模块（触发 @register 装饰器）。"""
    module_path = _COMMENT_MODULES.get(platform)
    if module_path is None:
        return
    import importlib

    try:
        importlib.import_module(module_path)
    except Exception as e:
        logger.warning("Failed to import comment module for %s: %s", platform, e)
