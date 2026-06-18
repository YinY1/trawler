"""微博评论亮点抓取模块 - 使用 PC 端 API"""

from __future__ import annotations

# pyright: basic
import logging
import re
from typing import Any

import aiohttp

from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS, WEIBO_REQUEST_TIMEOUT
from shared.protocols import CommentHighlight

logger = logging.getLogger("trawler.weibo.comments")

# PC 端评论 API
COMMENT_API = "https://weibo.com/ajax/statuses/buildComments?flow=default&id={post_id}&is_show_bulletin=2&key="


def _get_default_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )


def _clean_html(text: str) -> str:
    """去除 HTML 标签和 &entity;。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", "", text)
    return text.strip()


def _parse_comment(
    comment_data: dict[str, Any],
    author_user_id: str = "",
) -> CommentHighlight | None:
    """解析 PC 端 API 返回的单条评论。

    Args:
        comment_data: API 返回的评论数据
        author_user_id: 微博作者 ID（用于判断 is_author）

    Returns:
        CommentHighlight 或 None
    """
    try:
        text = comment_data.get("text_raw", "") or comment_data.get("text", "")
        if not text:
            return None
        content = _clean_html(text)
        if not content:
            return None

        user_info = comment_data.get("user", {})
        user_name = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        like_count = int(comment_data.get("like_count", 0) or 0)
        is_author = bool(author_user_id and user_id == author_user_id)

        return CommentHighlight(
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
) -> list[CommentHighlight]:
    """获取微博帖子的评论亮点（PC 端 API）。

    按点赞数降序排列，最多返回 max_count 条。
    翻页获取更多评论，失败时返回空列表。

    Args:
        post_id: 帖子 ID
        config: 全局配置
        author_user_id: 帖子作者 ID（用于标记作者评论）
        max_count: 最大返回数量

    Returns:
        评论亮点列表
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.debug("[评论] 缺少 Cookie，跳过评论抓取: %s", post_id)
        return []

    headers = {
        "User-Agent": _get_default_ua(),
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
    }

    all_comments: list[CommentHighlight] = []
    async with aiohttp.ClientSession(trust_env=False) as session:
        max_id = 0
        page = 0
        max_pages = 5

        while len(all_comments) < max_count * 2 and page < max_pages:
            page += 1
            try:
                url = COMMENT_API.format(post_id=post_id)
                if max_id:
                    url += f"&max_id={max_id}"

                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        logger.debug(
                            "[评论] API 返回状态码: %s, post_id: %s",
                            resp.status,
                            post_id,
                        )
                        break

                    data = await resp.json()

                if not data.get("ok"):
                    break

                comments_raw = data.get("data", [])
                if not isinstance(comments_raw, list) or not comments_raw:
                    break

                for raw in comments_raw:
                    comment = _parse_comment(raw, author_user_id)
                    if comment is not None:
                        all_comments.append(comment)

                max_id = data.get("max_id", 0) or 0
                if not max_id:
                    break  # 无更多页

            except Exception as e:
                logger.warning("[评论] 抓取评论异常: %s, post_id: %s", e, post_id)
                break

    if not all_comments:
        return []

    all_comments.sort(key=lambda c: c.like_count, reverse=True)
    result = all_comments[:max_count]

    logger.info("[评论] 获取到 %d 条热门评论, post_id: %s", len(result), post_id)
    return result
