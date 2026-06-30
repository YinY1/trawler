"""微博 HTTP API 封装层

提供微博移动端和 PC 端 API 的请求和响应解析。
移动端: m.weibo.cn (不需要签名，请求简单)
PC 端: weibo.com/ajax (需要完整 Cookie，数据更丰富)
"""

from __future__ import annotations

# pyright: basic
import html as _html_module
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from shared.constants import WEIBO_REQUEST_TIMEOUT
from shared.protocols import WeiboPost

logger = logging.getLogger("trawler.weibo.api")

# 移动端 API
MOBILE_USER_POSTS_API = "https://m.weibo.cn/api/container/getIndex?type=uid&value={user_id}&containerid=107603{user_id}"

# PC 端 API
PC_USER_POSTS_API = "https://weibo.com/ajax/statuses/mymblog?uid={user_id}&page=1&feature=0"

# 微博图片 CDN 模板
SINAIMG_URL_TEMPLATE = "https://wx1.sinaimg.cn/large/{pic_id}.jpg"

# 长文 API
LONGTEXT_API = "https://weibo.com/ajax/statuses/longtext?id={post_id}"

# 单条微博详情 API（含视频 page_info，issue #46 PR-2 反查 video_urls）
POST_DETAIL_API = "https://weibo.com/ajax/statuses/show?id={post_id}"

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


# ── ──


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


async def _fetch_long_text(cookie: str, post_id: str) -> str:
    """获取微博长文完整内容。

    Args:
        cookie: Cookie 字符串
        post_id: 帖子 ID

    Returns:
        完整长文纯文本，失败时返回空字符串
    """
    if not cookie or not post_id:
        return ""

    url = LONGTEXT_API.format(post_id=post_id)
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Referer": "https://weibo.com/",
        "Cookie": cookie,
    }

    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                lt = data.get("data", {})
                return lt.get("longTextContent_raw", "") or lt.get("longTextContent", "")
        except Exception:
            logger.debug("获取长文失败: %s", post_id)
            return ""


async def fetch_post_detail(cookie: str, post_id: str) -> dict[str, Any]:
    """获取单条微博详情(含视频 page_info)。

    issue #46 PR-2: download handler 阶段需要 video_urls,但 detector 写入
    MessageRecord 时未持久化 video_urls。download 阶段通过本函数反查详情拿到。

    Args:
        cookie: Cookie 字符串
        post_id: 帖子 ID

    Returns:
        data 字段(dict);失败返回空 dict
    """
    if not cookie or not post_id:
        return {}

    url = POST_DETAIL_API.format(post_id=post_id)
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Referer": "https://weibo.com/",
        "Cookie": cookie,
    }

    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return data.get("data", {}) or {}
        except Exception:
            logger.debug("获取微博详情失败: %s", post_id)
            return {}


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
    except ValueError:
        pass
    except OSError:
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


def _extract_video_urls(page_info: Any) -> list[str]:
    """从 page_info 提取视频直链(spec §3 / issue #46 PR-2)。

    优先级:
    1. page_info.urls (dict[str, str]) — 多分辨率 mp4 直链,取所有 .mp4 值
    2. page_info.media_info.stream_url_hd — 高清 mp4
    3. page_info.media_info.stream_url — 最低码率 mp4(兜底)

    Args:
        page_info: 原始 page_info 字段(dict),可能为 None / 非 dict

    Returns:
        视频 URL 列表;page_info.type != "video" 或无可用 URL 时返回空 list
    """
    if not isinstance(page_info, dict):
        return []
    if page_info.get("type") != "video":
        return []

    urls: list[str] = []

    # 1. page_info.urls (多分辨率 dict)
    pi_urls = page_info.get("urls")
    if isinstance(pi_urls, dict):
        for v in pi_urls.values():
            if isinstance(v, str) and v:
                urls.append(v)

    # 2/3. media_info 兜底
    if not urls:
        media_info = page_info.get("media_info")
        if isinstance(media_info, dict):
            hd = media_info.get("stream_url_hd")
            if isinstance(hd, str) and hd:
                urls.append(hd)
            else:
                low = media_info.get("stream_url")
                if isinstance(low, str) and low:
                    urls.append(low)

    return urls


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

        # 视频直链(spec §3 / issue #46 PR-2)
        video_urls = _extract_video_urls(raw.get("page_info"))

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
            video_urls=video_urls,
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
        clean_text = _clean_html(raw.get("text_raw", "") or text)
        is_long_text = bool(raw.get("isLongText", False))
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
                info = pic_infos.get(pid) if isinstance(pic_infos, dict) else None
                if info is not None:
                    if isinstance(info, dict):
                        url = info.get("original", {}).get("url", "") or info.get("large", {}).get("url", "")
                        if url:
                            image_urls.append(url)
                            continue
                # 降级：使用模板 URL
                image_urls.append(SINAIMG_URL_TEMPLATE.format(pic_id=pid))

        # 视频直链(spec §3 / issue #46 PR-2)
        video_urls = _extract_video_urls(raw.get("page_info"))

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
            is_long_text=is_long_text,
            video_urls=video_urls,
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

    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.warning("移动端 API 返回状态码: %s", resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            logger.warning("移动端 API 请求失败: %s", e)
            return []

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

    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.warning("PC 端 API 返回状态码: %s", resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            logger.warning("PC 端 API 请求失败: %s", e)
            return []

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
    prefer_pc: bool = True,
) -> list[WeiboPost]:
    """获取用户微博列表，自动选择 API 并解析为 WeiboPost。

    默认优先使用 PC 端 API（标准 SUB cookie 即可），移动端 API 需要额外
    weibo.cn 域的 cookie 才可用。

    Args:
        cookie: Cookie 字符串
        user_id: 用户 ID
        max_posts: 最大获取数量
        prefer_pc: 是否优先使用 PC 端 API（默认 True）

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

    # 长文补充
    if cookie:
        for post in results:
            if post.is_long_text:
                full_text = await _fetch_long_text(cookie, post.post_id)
                if full_text:
                    post.long_text = full_text
                    post.clean_text = full_text

    return results


# ── 用户搜索 ────────────────────────────────────────────────────

# PC 网页搜索（s.weibo.com），返回 HTML。
# 用法: PC_USER_SEARCH_API.format(query=url_encoded_name)
# 2026-06-28: m.weibo.cn/api/container/getIndex?type=suggestion 已下线（404）；
# weibo.com/ajax/search/all 等也已失效（404 message:地址不存在）。
# s.weibo.com/user 是当前唯一仍可用的搜索入口，HTML 内嵌 <a class="name">。
# 风控兜底: s.weibo.com 在被风控时仍返回 200，但 HTML 是验证/跳转页，
# 既不含 s.weibo.com 自身标记也不含 $CONFIG。函数内会先校验页面有效性，
# 再用下面的块正则解析（见 search_user_by_name 实现）。
PC_USER_SEARCH_API = "https://s.weibo.com/user?q={query}&Refer=SUer_box"

# 搜索结果解析：以单个 <a ...>...</a> 为块（DOTALL 跨行），块内分别匹配 uid 与 name。
# 不假设 href/class 顺序，不假设属性在同一行（微博模板排版不稳定）。
# 单条样本形如:
#   <a href="//weibo.com/u/2803301701" class="name" ...>人民日报</a>
_SEARCH_RESULT_BLOCK_RE = re.compile(
    r"<a\b(?P<attrs>[^>]*)>(?P<name>.*?)</a>",
    re.DOTALL,
)
_SEARCH_UID_RE = re.compile(r'href\s*=\s*"?//weibo\.com/u/(?P<uid>\d+)"?')
# 严格匹配 class 含 name token：要求 name 前后是空白或引号边界，
# 避免 class="my-name-card" / class="nickname" / class="username" 等假阳性。
# 实测 s.weibo.com 搜索结果页面只用 class="name"，无歧义用例。
_SEARCH_NAME_CLASS_RE = re.compile(r'class\s*=\s*"(?:[^"]*\s)?name(?:\s[^"]*)?"')


async def search_user_by_name(
    cookie: str,
    nickname: str,
    user_agent: str = _DEFAULT_UA,
) -> list[dict[str, Any]]:
    """通过昵称搜索微博用户（PC 网页 s.weibo.com/user）。

    失败原因（状态码 != 200 / 风控页 / 网络异常）只通过 ``logger.warning`` /
    ``logger.exception`` 写到日志，**不抛异常、不在返回值中体现**。
    因此调用方（``core/subscription_cli.py:_search_weibo``）只能看到空列表 +
    通用提示「未找到名为「...」的用户」，无法区分「API 下线/风控」与「真的没结果」。
    这是有意取舍：见 plan §10「日志与可见性」对权衡的讨论。

    Args:
        cookie: 微博 Cookie 字符串（需含 weibo.com 域 SUB）
        nickname: 搜索的昵称
        user_agent: 自定义 UA

    Returns:
        用户列表，每项含 id / screen_name 字段（保持与旧 API 字段名兼容）
    """
    url = PC_USER_SEARCH_API.format(query=quote(nickname))
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
    }

    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.warning("微博用户搜索返回状态码: %s", resp.status)
                    return []
                html = await resp.text()
        except Exception:
            logger.exception("微博用户搜索请求异常")
            return []

    # 风控兜底：s.weibo.com 在被风控时仍返回 200，但页面是
    # passport.weibo.com 跳转/验证页，HTML 中既无 $CONFIG 也无 s.weibo.com 自身标记。
    # 与「正常但 0 结果」必须区分（否则用户/UI 无法判断是 API 失效还是真的没结果）。
    #
    # 阈值决策（NEW-1, 2026-06-28 真机验证）：
    #   真实页面总长 127923，'s.weibo.com' 首次偏移 3162，'$CONFIG' 首次偏移 2116。
    #   plan 原值 html[:2000] 检查 's.weibo.com' 会误判（真实偏移 3162 > 2000）。
    #   因此改用整文搜索（不截断），避免依赖 head 长度的不稳定假设。
    if "s.weibo.com" not in html and "$CONFIG" not in html:
        logger.warning("微博搜索返回疑似验证/风控页面（无 s.weibo.com / $CONFIG 标记），可能触发了风控")
        return []

    # 去重（同 uid 可能多次出现）
    seen: set[str] = set()
    users: list[dict[str, Any]] = []
    for block_m in _SEARCH_RESULT_BLOCK_RE.finditer(html):
        attrs = block_m.group("attrs")
        raw_name = block_m.group("name")
        uid_m = _SEARCH_UID_RE.search(attrs)
        if not uid_m:
            continue
        if not _SEARCH_NAME_CLASS_RE.search(attrs):
            continue
        uid = uid_m.group("uid")
        # html.unescape: 用户名里可能含 &amp; &lt; &quot; &#xxx; 等实体
        name = _html_module.unescape(raw_name).strip()
        if uid in seen or not name:
            continue
        seen.add(uid)
        # 保持旧字段名: id (int) / screen_name (str)，
        # 因为 core/subscription_cli.py:_search_weibo 用这两个键。
        users.append({"id": int(uid), "screen_name": name})
    return users
