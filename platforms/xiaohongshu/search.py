"""小红书用户搜索模块 — 通过昵称搜索用户 (via XhsClient)."""

from __future__ import annotations

# pyright: basic
import logging
from typing import Any

from platforms.xiaohongshu.client import XhsClient
from shared.cookie_utils import extract_cookie_value

logger = logging.getLogger(__name__)


async def search_xhs_user_by_name(
    cookie: str,
    query: str,
    page: int = 1,
) -> list[dict[str, Any]]:
    """通过昵称搜索小红书用户 (via XhsClient).

    Args:
        cookie: Cookie 字符串（需含 a1）
        query: 搜索关键词（用户昵称）
        page: 页码

    Returns:
        用户列表，每项含 user_id / nickname / avatar 等字段
    """
    a1 = extract_cookie_value(cookie, "a1")
    if not a1:
        logger.warning("小红书搜索缺少 a1 cookie")
        return []

    client = XhsClient(cookie=cookie)
    try:
        return await client.search_users(query, page=page)
    except Exception:
        logger.exception("小红书搜索请求异常")
        return []
    finally:
        await client.close()
