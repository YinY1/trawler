"""小红书评论亮点抓取模块 - 获取热门评论"""

from __future__ import annotations

# pyright: basic
import logging
from typing import Any

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
from platforms.xiaohongshu.auth import get_xhs_cookie
from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS
from shared.protocols import CommentHighlight

logger = logging.getLogger("trawler.xiaohongshu.comments")

# 最大返回评论数
MAX_HIGHLIGHT_COMMENTS = MAX_COMMENT_HIGHLIGHTS


def _parse_comment(comment_data: dict[str, Any], author_user_id: str = "") -> CommentHighlight | None:
    """解析单条评论数据。

    Args:
        comment_data: API 返回的评论数据
        author_user_id: 笔记作者 ID（用于判断 is_author）

    Returns:
        CommentHighlight 或 None
    """
    try:
        content = comment_data.get("content", "")
        if not content:
            return None

        user_info = comment_data.get("user_info", {})
        user_name = user_info.get("nickname", "") or user_info.get("username", "")
        user_id = user_info.get("user_id", "")

        like_count = 0
        like_str = comment_data.get("like_count", "0")
        try:
            like_count = int(like_str) if like_str else 0
        except ValueError, TypeError:
            like_count = 0

        is_author = bool(author_user_id and user_id == author_user_id)

        return CommentHighlight(
            content=content,
            user_name=user_name,
            is_author=is_author,
            like_count=like_count,
        )
    except Exception as e:
        logger.debug(f"解析评论数据失败: {e}")
        return None


async def fetch_xhs_comment_highlights(
    note_id: str,
    config: Config,
    *,
    author_user_id: str = "",
    max_count: int = MAX_HIGHLIGHT_COMMENTS,
    xsec_token: str = "",
) -> list[CommentHighlight]:
    """获取小红书笔记的评论亮点（热门评论）。

    按点赞数降序排列，过滤笔记作者本人的评论，最多返回 max_count 条。
    失败时返回空列表，不影响主流程。

    Args:
        note_id: 笔记 ID
        config: 全局配置
        author_user_id: 笔记作者 ID（用于过滤作者评论）
        max_count: 最大返回数量

    Returns:
        评论亮点列表
    """
    cookie = get_xhs_cookie(config)
    if not cookie:
        logger.debug(f"[评论] 缺少 Cookie，跳过评论抓取: {note_id}")
        return []

    client = AsyncXhsClient(cookie=cookie)
    try:
        data = await client.get_note_comments(note_id, xsec_token=xsec_token)
    except Exception as e:
        logger.debug(f"[评论] 请求失败: {e}, note_id: {note_id}")
        return []
    finally:
        await client.close()

    comments_raw = data.get("comments", [])
    if not isinstance(comments_raw, list):
        return []

    all_comments: list[CommentHighlight] = []
    for raw in comments_raw:
        comment = _parse_comment(raw, author_user_id)
        if comment is None or comment.is_author:
            continue
        all_comments.append(comment)

    # 尝试获取第二页（如果需要）
    has_more = data.get("has_more", False)
    cursor = data.get("cursor", "")
    if has_more and cursor and len(all_comments) < max_count:
        try:
            data2 = await client.get_note_comments(note_id, cursor=cursor, xsec_token=xsec_token)
            for raw in data2.get("comments", []):
                comment = _parse_comment(raw, author_user_id)
                if comment is None or comment.is_author:
                    continue
                all_comments.append(comment)
        except Exception:
            pass  # 第二页获取失败不影响结果

    # 按点赞数降序排列
    all_comments.sort(key=lambda c: c.like_count, reverse=True)

    # 限制数量
    result = all_comments[:max_count]

    logger.info(f"[评论] 获取到 {len(result)} 条热门评论, note_id: {note_id}")
    return result
