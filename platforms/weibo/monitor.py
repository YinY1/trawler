"""微博内容监控模块 - 检测用户新微博"""

from __future__ import annotations

import logging

from rich.console import Console

from platforms.weibo.api import fetch_user_posts
from shared.config import Config
from shared.protocols import JsonSetStore, WeiboPost

logger = logging.getLogger(__name__)
console = Console()

# 默认每次检查最大微博数
DEFAULT_MAX_POSTS_PER_CHECK = 10


class WeiboSubscriptionStore(JsonSetStore):
    """微博已知帖子存储，用于去重。

    继承 JsonSetStore，管理 data/known_weibo_posts.json 文件。
    """

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "known_weibo_posts.json")

    def mark_known_weibo_post(self, post: WeiboPost) -> None:
        """将帖子标记为已知（便利方法）。"""
        self.mark_known(post.post_id)


async def check_new_weibo_posts(
    user_id: str,
    name: str,
    config: Config,
    store: WeiboSubscriptionStore,
    max_posts: int = DEFAULT_MAX_POSTS_PER_CHECK,
) -> list[WeiboPost]:
    """检查指定用户的新微博。

    获取用户微博列表，过滤已知微博，返回新增微博列表。

    Args:
        user_id: 微博用户 ID
        name: 用户名称（用于日志）
        config: 全局配置
        store: 已知帖子存储
        max_posts: 单次检查最大返回帖子数

    Returns:
        新增的 WeiboPost 列表（按发布时间降序）
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.error("[%s] 缺少 Cookie，无法检查微博", name)
        return []

    logger.info("检查用户 %s (%s) 的新微博", name, user_id)

    try:
        posts = await fetch_user_posts(cookie, user_id, max_posts)
    except Exception as e:
        logger.error("获取用户 %s 微博失败: %s", user_id, e)
        return []

    if not posts:
        logger.info("[%s] 未获取到任何微博", name)
        return []

    new_posts: list[WeiboPost] = []
    for post in posts:
        if store.is_known(post.post_id):
            continue
        new_posts.append(post)

    # 按发布时间降序
    new_posts.sort(key=lambda p: p.pubdate, reverse=True)

    # 限制数量
    if len(new_posts) > max_posts:
        new_posts = new_posts[:max_posts]

    logger.info("[%s] 发现 %d 条新微博", name, len(new_posts))
    return new_posts
