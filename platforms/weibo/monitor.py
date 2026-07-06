"""微博监控模块 — 按需抓取单条 post（issue #101）。

weibo 的 detector 逻辑在 ``handlers.py::weibo_detector`` 内联（基于
``fetch_user_posts``），本模块仅承载按 ID 单条抓取入口 ``fetch_post_by_id``，
对称 bili/xhs 的 ``fetch_video_by_id`` / ``fetch_note_by_id``。
"""

from __future__ import annotations

# pyright: basic
import logging

from platforms.weibo.api import (
    _clean_html,
    _extract_video_urls,
    _parse_weibo_time,
    fetch_post_detail,
)
from shared.config import Config
from shared.protocols import FetchedMessage

logger = logging.getLogger("trawler.weibo.monitor")


async def fetch_post_by_id(
    post_id: str,
    config: Config,
) -> FetchedMessage | None:
    """按 post_id 抓取单条微博元数据（issue #101）。

    包装 ``api.fetch_post_detail``（download handler 已用同一 API 反查 video_urls）。

    Args:
        post_id: 微博 post ID（不带 "weibo:" 前缀）
        config: 全局配置（取 ``config.weibo.auth.cookie``）

    Returns:
        ``FetchedMessage``；``content_type`` 按 ``page_info.type == "video"``
        或 ``_extract_video_urls(page_info)`` 非空判断。
        ``fetch_post_detail`` 返回空 dict → None（可能 post 不存在或网络问题）。

    Raises:
        无 —— 失败信号通过 None 表达，调用方可重试。
    """
    from shared.protocols import ContentType

    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.warning("weibo cookie 缺失，无法 fetch (post_id=%s)", post_id)
        return None

    detail = await fetch_post_detail(cookie, post_id)
    if not detail:
        logger.info("weibo fetch_post_detail 返回空 (post_id=%s)", post_id)
        return None

    # 文本与作者
    text_raw = detail.get("text_raw", "") or detail.get("text", "") or ""
    clean_text = _clean_html(text_raw)
    user_info = detail.get("user", {}) if isinstance(detail.get("user"), dict) else {}
    author = user_info.get("screen_name", "") or ""

    # content_type 判断（与 handlers.py::weibo_detector 一致）
    page_info = detail.get("page_info", {}) if isinstance(detail.get("page_info"), dict) else {}
    video_urls = _extract_video_urls(page_info)
    content_type = ContentType.VIDEO if video_urls else ContentType.TEXT

    # pubdate
    pubdate = _parse_weibo_time(detail.get("created_at", ""))

    # title 截断到 50 字符预览；body 留空 —— 长文由 download 阶段统一拉
    # （handlers.py:89-98 已实现，fetch 阶段再拉会浪费配额，P2-1 修复）
    title = clean_text[:50] if clean_text else post_id

    return FetchedMessage(
        msg_id=f"weibo:{post_id}",
        platform="weibo",
        content_type=content_type,
        pubdate=pubdate,
        title=title,
        author=author,
        body="",
    )
