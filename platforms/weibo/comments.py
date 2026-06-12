"""微博评论亮点抓取模块 - 获取热门评论"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from rich.console import Console

from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS, WEIBO_REQUEST_TIMEOUT
from shared.http import get_session
from shared.protocols import WeiboCommentHighlight

logger = logging.getLogger(__name__)
console = Console()

# 微博评论 API（移动端）
COMMENT_API = "https://m.weibo.cn/comments/hotflow?id={post_id}&mid={post_id}&max_id_type=0"


def _get_default_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )


def _parse_comment(comment_data: dict[str, Any], author_user_id: str = "") -> WeiboCommentHighlight | None:
    """解析单条评论数据。

    Args:
        comment_data: API 返回的评论数据
        author_user_id: 微博作者 ID（用于判断 is_author）

    Returns:
        WeiboCommentHighlight 或 None
    """
    try:
        content = comment_data.get("text", "")
        if not content:
            return None

        # 去除 HTML 标签
        content = re.sub(r"<[^>]+>", "", content)
        content = content.strip()
        if not content:
            return None

        user_info = comment_data.get("user", {})
        user_name = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        like_count = int(comment_data.get("like_count", 0) or 0)
        is_author = bool(author_user_id and user_id == author_user_id)

        return WeiboCommentHighlight(
            content=content,
            user_name=user_name,
            is_author=is_author,
            like_count=like_count,
        )
    except Exception as e:
        logger.debug("解析评论数据失败: %s", e)
        return None


async def fetch_weibo_comment_highlights(
    post_id: str,
    config: Config,
    *,
    author_user_id: str = "",
    max_count: int = MAX_COMMENT_HIGHLIGHTS,
) -> list[WeiboCommentHighlight]:
    """获取微博帖子的评论亮点（热门评论）。

    按点赞数降序排列，最多返回 max_count 条。
    失败时返回空列表，不影响主流程。

    Args:
        post_id: 帖子 ID
        config: 全局配置
        author_user_id: 帖子作者 ID（用于过滤作者评论）
        max_count: 最大返回数量

    Returns:
        评论亮点列表
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.debug("[评论] 缺少 Cookie，跳过评论抓取: %s", post_id)
        return []

    url = COMMENT_API.format(post_id=post_id)
    headers = {
        "User-Agent": _get_default_ua(),
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }

    all_comments: list[WeiboCommentHighlight] = []
    session = await get_session()

    try:
        resp = await session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT))
        try:
            if resp.status != 200:
                logger.debug("[评论] API 返回状态码: %s, post_id: %s", resp.status, post_id)
                return []
            data = await resp.json()
        finally:
            resp.close()

        if not data.get("ok"):
            logger.debug("[评论] API 失败: %s, post_id: %s", data.get("msg", "unknown"), post_id)
            return []

        comments_raw = data.get("data", {}).get("data", [])
        if not isinstance(comments_raw, list):
            return []

        for raw in comments_raw:
            comment = _parse_comment(raw, author_user_id)
            if comment is None:
                continue
            all_comments.append(comment)

    except Exception as e:
        logger.warning("[评论] 抓取评论异常: %s, post_id: %s", e, post_id)
        return []

    # 按点赞数降序
    all_comments.sort(key=lambda c: c.like_count, reverse=True)
    result = all_comments[:max_count]

    logger.info("[评论] 获取到 %d 条热门评论, post_id: %s", len(result), post_id)
    return result
