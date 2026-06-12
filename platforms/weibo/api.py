"""微博 HTTP API 封装层

提供微博移动端和 PC 端 API 的请求和响应解析。
移动端: m.weibo.cn (不需要签名，请求简单)
PC 端: weibo.com/ajax (需要完整 Cookie，数据更丰富)
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

import aiohttp

import shared.http
from shared.constants import WEIBO_REQUEST_TIMEOUT
from shared.protocols import WeiboPost

logger = logging.getLogger(__name__)

# 移动端 API
MOBILE_USER_POSTS_API = "https://m.weibo.cn/api/container/getIndex?type=uid&value={user_id}&containerid=107603{user_id}"

# PC 端 API
PC_USER_POSTS_API = "https://weibo.com/ajax/statuses/mymblog?uid={user_id}&page=1&feature=0"

# 微博图片 CDN 模板
SINAIMG_URL_TEMPLATE = "https://wx1.sinaimg.cn/large/{pic_id}.jpg"

# 默认 User-Agent
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 微博时间格式：Tue Jun 11 10:00:00 +0800 2026
_WEIBO_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"

# HTML 清理正则
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")

# 中文时间格式（PC 端可能返回）：刚刚 / N分钟前 / 今天 HH:MM / 月-日 HH:MM / 年-月-日 HH:MM
_CHINESE_TIME_RE = re.compile(
    r"(?:刚刚)|"
    r"(?:(\d+)分钟前)|"
    r"(?:今天\s+(\d+):(\d+))|"
    r"(?:(\d+)-(\d+)\s+(\d+):(\d+))|"
    r"(?:(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d+):(\d+))"
)


# ── 公共辅助函数 ──────────────────────────────────────────


def _clean_html(text: str) -> str:
    """去除 HTML 标签和实体，保留纯文本。

    Args:
        text: 可能包含 HTML 标记的原始文本

    Returns:
        清理后的纯文本
    """
    if not text:
        return ""
    text = _HTML_TAG_RE.sub("", text)
    text = _HTML_ENTITY_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _parse_weibo_time(time_str: str) -> int:
    """将微博时间字符串解析为 Unix 时间戳。

    支持格式：
    - 标准格式: "Tue Jun 11 10:00:00 +0800 2026"
    - 中文格式: "刚刚", "5分钟前", "今天 10:00", "06-11 10:00", "2026-06-11 10:00"

    Args:
        time_str: 微博时间字符串

    Returns:
        Unix 时间戳
    """
    if not time_str:
        return int(time.time())

    # 尝试标准格式
    try:
        dt = datetime.strptime(time_str, _WEIBO_TIME_FORMAT)
        return int(dt.timestamp())
    except (ValueError, OSError):
        pass

    # 尝试中文格式
    now = datetime.now()
    m = _CHINESE_TIME_RE.match(time_str)
    if m:
        groups = m.groups()
        if time_str == "刚刚":
            return int(time.time())
        if groups[0]:  # N分钟前
            return int(time.time()) - int(groups[0]) * 60
        if groups[1] and groups[2]:  # 今天 HH:MM
            today = now.replace(hour=int(groups[1]), minute=int(groups[2]), second=0, microsecond=0)
            return int(today.timestamp())
        if groups[3] and groups[4]:  # 月-日 HH:MM
            d = now.replace(
                month=int(groups[3]),
                day=int(groups[4]),
                hour=int(groups[5]),
                minute=int(groups[6]),
                second=0,
                microsecond=0,
            )
            return int(d.timestamp())
        if groups[7] and groups[8]:  # 年-月-日 HH:MM
            d = datetime(int(groups[7]), int(groups[8]), int(groups[9]), int(groups[10]), int(groups[11]))
            return int(d.timestamp())

    return int(time.time())


def _parse_mobile_post(raw: dict[str, Any]) -> WeiboPost | None:
    """解析移动端 API 返回的单条微博数据。

    移动端返回结构: {id, text, user, created_at, pics, reposts_count, ...}

    Args:
        raw: 移动端 API 返回的单条微博数据

    Returns:
        WeiboPost 或 None（解析失败时）
    """
    try:
        post_id = str(raw.get("id", ""))
        if not post_id:
            return None

        text = raw.get("text", "")
        clean_text = _clean_html(text)
        user_info = raw.get("user", {})
        author = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        pubdate = _parse_weibo_time(raw.get("created_at", ""))

        # 图片列表
        image_urls: list[str] = []
        pics = raw.get("pics", [])
        if isinstance(pics, list):
            for pic in pics:
                url = ""
                if isinstance(pic, dict):
                    url = pic.get("url", "") or pic.get("large", {}).get("url", "")
                elif isinstance(pic, str):
                    url = pic
                if url:
                    image_urls.append(url)

        # 统计数据
        reposts_count = int(raw.get("reposts_count", 0) or 0)
        comments_count = int(raw.get("comments_count", 0) or 0)
        likes_count = int(raw.get("attitudes_count", 0) or 0)
        is_original = bool(raw.get("is_original", 1))

        # 转发微博（嵌套）
        reposted_post: WeiboPost | None = None
        retweeted = raw.get("retweeted_status")
        if isinstance(retweeted, dict) and retweeted.get("id"):
            reposted_post = _parse_mobile_post(retweeted)

        return WeiboPost(
            post_id=post_id,
            text=text,
            clean_text=clean_text,
            author=author,
            user_id=user_id,
            pubdate=pubdate,
            image_urls=image_urls,
            reposts_count=reposts_count,
            comments_count=comments_count,
            likes_count=likes_count,
            is_original=is_original,
            reposted_post=reposted_post,
        )
    except Exception as e:
        logger.debug("解析移动端微博数据失败: %s", e)
        return None


def _parse_pc_post(raw: dict[str, Any]) -> WeiboPost | None:
    """解析 PC 端 API 返回的单条微博数据。

    PC 端返回结构: {id, idstr, text, user, created_at, pic_ids, pic_infos, ...}

    Args:
        raw: PC 端 API 返回的单条微博数据

    Returns:
        WeiboPost 或 None（解析失败时）
    """
    try:
        post_id = str(raw.get("idstr", "") or raw.get("id", ""))
        if not post_id:
            return None

        text = raw.get("text", "")
        clean_text = _clean_html(text)
        user_info = raw.get("user", {})
        author = user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        user_id = str(user_info.get("id", "")) if isinstance(user_info, dict) else ""

        pubdate = _parse_weibo_time(raw.get("created_at", ""))

        # 图片列表（PC 端使用 pic_ids + pic_infos）
        image_urls: list[str] = []
        pic_ids = raw.get("pic_ids", [])
        pic_infos = raw.get("pic_infos", {})
        if isinstance(pic_ids, list) and pic_ids:
            for pid in pic_ids:
                if isinstance(pic_infos, dict) and pid in pic_infos:
                    info = pic_infos[pid]
                    if isinstance(info, dict):
                        url = info.get("original", {}).get("url", "") or info.get("large", {}).get("url", "")
                        if url:
                            image_urls.append(url)
                            continue
                # 降级：使用模板 URL
                image_urls.append(SINAIMG_URL_TEMPLATE.format(pic_id=pid))

        # 统计数据（PC 端返回字符串）
        reposts_count = int(raw.get("reposts_count", "0") or "0")
        comments_count = int(raw.get("comments_count", "0") or "0")
        likes_count = int(raw.get("attitudes_count", "0") or "0")
        is_original = bool(raw.get("is_original", 1))

        # 转发微博
        reposted_post: WeiboPost | None = None
        retweeted = raw.get("retweeted_status")
        if isinstance(retweeted, dict) and retweeted.get("id"):
            reposted_post = _parse_pc_post(retweeted)

        return WeiboPost(
            post_id=post_id,
            text=text,
            clean_text=clean_text,
            author=author,
            user_id=user_id,
            pubdate=pubdate,
            image_urls=image_urls,
            reposts_count=reposts_count,
            comments_count=comments_count,
            likes_count=likes_count,
            is_original=is_original,
            reposted_post=reposted_post,
        )
    except Exception as e:
        logger.debug("解析 PC 端微博数据失败: %s", e)
        return None


# ── 获取用户微博列表 ──────────────────────────────────────


async def fetch_user_posts_mobile(
    cookie: str,
    user_id: str,
    max_posts: int = 20,
    user_agent: str = _DEFAULT_UA,
) -> list[dict[str, Any]]:
    """通过移动端 API 获取用户微博列表。

    移动端 API 不需要签名，请求简单，是优先使用的数据源。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        user_agent: User-Agent

    Returns:
        原始微博数据字典列表
    """
    url = MOBILE_USER_POSTS_API.format(user_id=user_id)
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }

    session = await shared.http.get_session()
    resp = None
    try:
        resp = await session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        )
        if resp.status != 200:
            logger.warning("移动端 API 返回状态码: %s", resp.status)
            return []
        data = await resp.json()
    except Exception as e:
        logger.warning("移动端 API 请求失败: %s", e)
        return []
    finally:
        if resp is not None:
            resp.close()

    if not data.get("ok"):
        logger.debug("移动端 API 返回失败: %s", data.get("msg", "unknown"))
        return []

    cards = data.get("data", {}).get("cards", [])
    posts: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        mblog = card.get("mblog")
        if isinstance(mblog, dict) and mblog.get("id"):
            posts.append(mblog)
            if len(posts) >= max_posts:
                break

    return posts


async def fetch_user_posts_pc(
    cookie: str,
    user_id: str,
    max_posts: int = 20,
    user_agent: str = _DEFAULT_UA,
) -> list[dict[str, Any]]:
    """通过 PC 端 API 获取用户微博列表。

    PC 端 API 返回数据更丰富（含 pic_infos），但可能需要完整 Cookie。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        user_agent: User-Agent

    Returns:
        原始微博数据字典列表
    """
    url = PC_USER_POSTS_API.format(user_id=user_id)
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": cookie,
    }

    session = await shared.http.get_session()
    resp = None
    try:
        resp = await session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        )
        if resp.status != 200:
            logger.warning("PC 端 API 返回状态码: %s", resp.status)
            return []
        data = await resp.json()
    except Exception as e:
        logger.warning("PC 端 API 请求失败: %s", e)
        return []
    finally:
        if resp is not None:
            resp.close()

    if not data.get("ok"):
        logger.debug("PC 端 API 返回失败: %s", data.get("msg", "unknown"))
        return []

    post_list = data.get("data", {}).get("list", [])
    if not isinstance(post_list, list):
        return []

    return post_list[:max_posts]


async def fetch_user_posts(
    cookie: str,
    user_id: str,
    max_posts: int = 20,
    prefer_pc: bool = False,
) -> list[WeiboPost]:
    """获取用户微博列表，自动选择 API 并解析为 WeiboPost。

    优先使用移动端 API（无需签名），降级使用 PC 端 API。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        prefer_pc: 是否优先使用 PC 端 API

    Returns:
        解析后的 WeiboPost 列表
    """
    if prefer_pc:
        raw_posts = await fetch_user_posts_pc(cookie, user_id, max_posts)
        parse_func = _parse_pc_post
        if not raw_posts:
            raw_posts = await fetch_user_posts_mobile(cookie, user_id, max_posts)
            parse_func = _parse_mobile_post
    else:
        raw_posts = await fetch_user_posts_mobile(cookie, user_id, max_posts)
        parse_func = _parse_mobile_post
        if not raw_posts:
            raw_posts = await fetch_user_posts_pc(cookie, user_id, max_posts)
            parse_func = _parse_pc_post

    results: list[WeiboPost] = []
    for raw in raw_posts:
        post = parse_func(raw)
        if post is not None:
            results.append(post)

    return results
