"""微博内容监控模块 — 已迁移至 handlers.py + PipelineEngine

此模块保留为兼容性 re-export（旧接口保留，标记为 deprecated）。
"""

from __future__ import annotations

import logging

from platforms.weibo.api import fetch_user_posts  # noqa: F401 - re-export
from shared.protocols import JsonSetStore

logger = logging.getLogger(__name__)


class WeiboSubscriptionStore(JsonSetStore):
    """[deprecated] 微博已知帖子存储，用于去重。

    已弃用，请使用 ``MessageStore``。保留仅为兼容旧调用方。
    """

    def __init__(self, data_dir: str = "data") -> None:
        import warnings

        warnings.warn(
            "WeiboSubscriptionStore is deprecated, use MessageStore instead",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(data_dir, "known_weibo_posts.json")


async def check_new_weibo_posts(  # noqa: PLR0913
    user_id: str,
    name: str,
    config: object,
    store: object,
    max_posts: int = 10,
) -> list:
    """[deprecated] 检查指定用户的新微博。

    已弃用，请使用 ``fetch_user_posts`` + ``MessageStore``。保留仅为兼容旧调用方。
    """
    import warnings

    warnings.warn(
        "check_new_weibo_posts is deprecated, use weibo_detector + MessageStore instead",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        posts = await fetch_user_posts(
            cookie="",
            user_id=user_id,
            max_posts=max_posts,
        )
    except Exception:
        logger.warning("Deprecated check_new_weibo_posts called for %s", user_id)
        return []
    return posts