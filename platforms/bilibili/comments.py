"""B站评论区亮点抓取 - 获取视频热门评论"""

from __future__ import annotations

import logging

from rich.console import Console

from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS
from shared.protocols import CommentHighlight

logger = logging.getLogger(__name__)
console = Console()


async def fetch_comment_highlights(
    bvid: str,
    config: Config,
    *,
    max_count: int = MAX_COMMENT_HIGHLIGHTS,
) -> list[CommentHighlight]:
    """抓取视频热门评论（按点赞排序）。

    过滤 UP 主本人评论和置顶评论，最多返回 max_count 条。
    需要有效的 Cookie 才能访问评论 API。

    Args:
        bvid: 视频 BV 号
        config: 全局配置
        max_count: 最大返回数量，默认 5

    Returns:
        评论亮点列表
    """
    from bilibili_api import comment, video

    from platforms.bilibili.auth import get_credential

    credential = get_credential(config)

    # 先获取视频的 aid（某些 API 需要 oid）
    v = video.Video(bvid=bvid, credential=credential)
    try:
        info = await v.get_info()
        aid = info.get("aid", 0)
    except Exception as e:
        logger.error(f"获取视频 {bvid} 信息失败: {e}")
        return []

    if aid == 0:
        logger.error(f"视频 {bvid} 的 aid 为 0，无法获取评论")
        return []

    # 获取视频 UP 主的 UID，用于过滤
    up_uid = info.get("owner", {}).get("mid", 0)

    # 抓取评论（按点赞排序）
    highlights: list[CommentHighlight] = []
    page = 1

    while len(highlights) < max_count:
        try:
            resp = await comment.get_comments(
                oid=aid,
                type_=comment.ResourceType.VIDEO,
                order=comment.OrderType.LIKE,
                page_index=page,
                credential=credential,
            )
        except Exception as e:
            logger.error(f"获取视频 {bvid} 评论失败 (page={page}): {e}")
            break

        replies = resp.get("replies")
        if not replies:
            break

        for reply in replies:
            if len(highlights) >= max_count:
                break

            # 跳过置顶评论
            if reply.get("up_action", {}).get("pin", False):
                continue

            member = reply.get("member", {})
            uid = member.get("mid", 0)

            # 过滤 UP 主本人评论
            is_up = uid == up_uid

            content = reply.get("content", {}).get("message", "")
            user_name = member.get("uname", "")
            like_count = reply.get("like", 0)

            if not content:
                continue

            highlights.append(
                CommentHighlight(
                    content=content,
                    user_name=user_name,
                    is_up_owner=is_up,
                    like_count=like_count,
                )
            )

        # 检查是否还有更多页
        page_info = resp.get("page", {})
        total = page_info.get("count", 0)
        current_count = page_info.get("num", 20) * page
        if current_count >= total:
            break
        page += 1

    logger.info(f"视频 {bvid} 获取到 {len(highlights)} 条评论亮点")
    return highlights[:max_count]
