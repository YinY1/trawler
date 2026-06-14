"""B站评论区抓取 — UP主活动优先：置顶 + 本人评论 + 本人回复（含对话链路）"""

from __future__ import annotations

import logging

from rich.console import Console

from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS
from shared.protocols import CommentHighlight

logger = logging.getLogger(__name__)
console = Console()


def _extract_reply_info(reply: dict) -> dict:
    """从 API 返回的单条评论/回复中提取通用字段。"""
    member = reply.get("member", {})
    content = reply.get("content", {}) or {}
    return {
        "rpid": reply.get("rpid", 0),
        "mid": reply.get("mid", 0) or member.get("mid", 0),
        "user_name": member.get("uname", "匿名"),
        "content": content.get("message", ""),
        "like": reply.get("like", 0),
    }


def _build_highlight(
    *,
    content: str,
    user_name: str,
    is_author: bool,
    like_count: int,
    is_pinned: bool = False,
    reply_to: str = "",
    parent_content: str = "",
) -> CommentHighlight | None:
    if not content:
        return None
    return CommentHighlight(
        content=content,
        user_name=user_name,
        is_author=is_author,
        like_count=like_count,
        is_pinned=is_pinned,
        reply_to=reply_to,
        parent_content=parent_content,
    )


def _find_parent_in_replies(
    replies: list[dict],
    target_rpid: int,
) -> dict | None:
    """在 replies 列表中按 rpid 查找父回复。"""
    for r in replies:
        if r.get("rpid") == target_rpid:
            return r
    return None


async def fetch_comment_highlights(
    bvid: str,
    config: Config,
    *,
    max_count: int = MAX_COMMENT_HIGHLIGHTS,
) -> list[CommentHighlight]:
    """抓取视频评论区中与 UP 主相关的评论。

    优先级：
    1. UP 主置顶的评论
    2. UP 主本人发表的评论
    3. UP 主回复他人的评论（含被回复原文，展示对话链路）
    4. 如果不足 max_count，补充高赞评论

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

    # ── 获取视频信息 ──
    v = video.Video(bvid=bvid, credential=credential)
    try:
        info = await v.get_info()
        aid = info.get("aid", 0)
        up_uid = info.get("owner", {}).get("mid", 0)
    except Exception as e:
        logger.error(f"获取视频 {bvid} 信息失败: {e}")
        return []

    if not aid:
        logger.error(f"视频 {bvid} 的 aid 为 0，无法获取评论")
        return []

    # ── 分页抓取评论 ──
    highlights: list[CommentHighlight] = []
    seen_rpids: set[int] = set()
    page = 1
    max_pages = 5  # 最多扫 5 页

    while len(highlights) < max_count and page <= max_pages:
        try:
            resp = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
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

            rpid = reply.get("rpid", 0)
            if rpid in seen_rpids:
                continue
            seen_rpids.add(rpid)

            # ── 解析顶层回复 ──────────────────────────────
            info_top = _extract_reply_info(reply)
            uid = info_top["mid"]
            is_up = uid == up_uid
            is_pinned = reply.get("up_action", {}).get("pin", False)

            # 检查子回复中是否有 UP 主的互动
            child_replies = reply.get("replies") or []

            # 情况 A：UP主置顶 → 总包含
            if is_pinned:
                hl = _build_highlight(
                    content=info_top["content"],
                    user_name=info_top["user_name"],
                    is_author=is_up,
                    like_count=info_top["like"],
                    is_pinned=True,
                )
                if hl:
                    highlights.append(hl)

            # 情况 B：UP 主本人发的顶层评论
            elif is_up:
                hl = _build_highlight(
                    content=info_top["content"],
                    user_name=info_top["user_name"],
                    is_author=True,
                    like_count=info_top["like"],
                )
                if hl:
                    highlights.append(hl)

            # 情况 C：UP 主在子回复中出现了 → 对话链路
            else:
                found_up_reply = False
                for child in child_replies:
                    child_info = _extract_reply_info(child)
                    if child_info["mid"] == up_uid:
                        # UP 主回复了 person_B，找到被回复的父回复
                        parent_rpid = child.get("parent", 0)
                        parent_text = ""
                        parent_user = ""

                        if parent_rpid == rpid:
                            # 直接回复顶层评论
                            parent_text = info_top["content"]
                            parent_user = info_top["user_name"]
                        else:
                            # 回复了子回复中的某人
                            parent_reply = _find_parent_in_replies(
                                child_replies,
                                parent_rpid,
                            )
                            if parent_reply:
                                p = _extract_reply_info(parent_reply)
                                parent_text = p["content"]
                                parent_user = p["user_name"]

                        hl = _build_highlight(
                            content=child_info["content"],
                            user_name=child_info["user_name"],
                            is_author=True,
                            like_count=child_info["like"],
                            reply_to=parent_user,
                            parent_content=parent_text,
                        )
                        if hl:
                            highlights.append(hl)
                            found_up_reply = True

                # 情况 D：如果都没命中，作为高赞候补
                if not found_up_reply and not is_pinned:
                    # 暂存在 seen 集里但跳过插入
                    pass

        # ── 翻页 ──────────────────────────────────────────
        page_info = resp.get("page", {})
        total = page_info.get("count", 0)
        current_count = page_info.get("num", 20) * page
        if current_count >= total:
            break
        page += 1

    # ── 如果 UP 主相关评论不足，补高赞 ────────────────────
    if len(highlights) < max_count:
        existing_keys: set[tuple[str, str]] = {(h.content, h.user_name) for h in highlights}
        page = 1
        while len(highlights) < max_count and page <= max_pages:
            try:
                resp = await comment.get_comments(
                    oid=aid,
                    type_=comment.CommentResourceType.VIDEO,
                    order=comment.OrderType.LIKE,
                    page_index=page,
                    credential=credential,
                )
            except Exception:
                break
            replies = resp.get("replies") or []
            for reply in replies:
                if len(highlights) >= max_count:
                    break
                info_fill = _extract_reply_info(reply)
                key = (info_fill["content"], info_fill["user_name"])
                if key in existing_keys:
                    continue
                existing_keys.add(key)
                # 只取高赞 (>5) 避免灌水
                if info_fill["like"] <= 5:
                    continue
                hl = _build_highlight(
                    content=info_fill["content"],
                    user_name=info_fill["user_name"],
                    is_author=False,
                    like_count=info_fill["like"],
                )
                if hl:
                    highlights.append(hl)
            page += 1

    logger.info(f"视频 {bvid} 获取到 {len(highlights)} 条评论亮点")
    return highlights[:max_count]
