"""小红书用户搜索模块 — 通过昵称搜索用户。

使用方式同 monitor.py：aiohttp + get_xhs_sign 签名。
数据格式参考 vendor/spider_xhs/apis/xhs_pc_apis.py 的 search_user 方法。
"""

from __future__ import annotations

# pyright: basic
import json
import logging
import math
import random
import time
from typing import Any

import aiohttp

from platforms.xiaohongshu.auth import DEFAULT_USER_AGENT, XHS_API_BASE, XHS_HOME_URL
from platforms.xiaohongshu.signer import get_xhs_sign
from shared.constants import XHS_REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# ── 搜索 ID 生成（与 vendor 保持一致）─────────────────────────


def _generate_search_id() -> str:
    """生成搜索会话 ID（base36 编码的 (timestamp_ms << 64) + random）。"""
    timestamp_ms = int(time.time() * 1000)
    random_part = math.ceil(0x7FFFFFFE * random.random())
    return _int_to_base36((timestamp_ms << 64) + random_part)


def _generate_search_request_id() -> str:
    """生成单次请求 ID（random-timestamp_ms）。"""
    timestamp_ms = int(time.time() * 1000)
    random_part = math.ceil(0x7FFFFFFE * random.random())
    return f"{random_part}-{timestamp_ms}"


def _int_to_base36(value: int) -> str:
    """Convert a (potentially very large) integer to base36."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    result = ""
    while value > 0:
        value, rem = divmod(value, 36)
        result = chars[rem] + result
    return result


def _extract_a1(cookie: str) -> str:
    """从 Cookie 中提取 a1 值。"""
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k.strip() == "a1":
                return v.strip()
    return ""


# ── 搜索 API ──────────────────────────────────────────────────


SEARCH_USER_API = "/api/sns/web/v1/search/usersearch"


async def search_xhs_user_by_name(
    cookie: str,
    query: str,
    page: int = 1,
) -> list[dict[str, Any]]:
    """通过昵称搜索小红书用户。

    Args:
        cookie: Cookie 字符串（需含 a1）
        query: 搜索关键词（用户昵称）
        page: 页码

    Returns:
        用户列表，每项含 user_id / nickname / avatar 等字段
    """
    a1 = _extract_a1(cookie)
    if not a1:
        logger.warning("小红书搜索缺少 a1 cookie")
        return []

    data = {
        "search_user_request": {
            "keyword": query,
            "search_id": _generate_search_id(),
            "page": page,
            "page_size": 15,
            "biz_type": "web_search_user",
            "request_id": _generate_search_request_id(),
        }
    }
    data_json = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    sign = get_xhs_sign(SEARCH_USER_API, data_json, a1=a1, method="POST")
    headers: dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Origin": XHS_HOME_URL,
        "Referer": f"{XHS_HOME_URL}/",
        "Content-Type": "application/json;charset=UTF-8",
        "x-s": sign["xs"],
        "x-t": sign["xt"],
        "x-s-common": sign["xs_common"],
        "Cookie": cookie,
    }

    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            async with session.post(
                XHS_API_BASE + SEARCH_USER_API,
                headers=headers,
                data=data_json,
                timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"小红书搜索 API 返回状态码: {resp.status}")
                    return []
                result = await resp.json(content_type=None)
        except Exception:
            logger.exception("小红书搜索请求异常")
            return []

    if not result.get("success", False):
        msg = result.get("msg", "unknown")
        logger.warning(f"小红书搜索 API 失败: {msg}")
        return []

    users = result.get("data", {}).get("users", [])
    return users if isinstance(users, list) else []
