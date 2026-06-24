"""小红书 HTTP 客户端 — 统一所有 XHS API 调用

设计：
- ``XhsClient`` 是小红书平台唯一的 HTTP 入口。所有 API 调用——内容获取、
  认证、签名验证——都通过这一个类进行。
- 内部使用 ``signer.get_xhs_sign()`` 获取完整 header set（7 个签名头：
  x-s, x-t, x-s-common, x-b3-traceid, x-mns, x-xray-traceid, xy-direction）。
- ``__init__`` 接受可选的 ``aiohttp.ClientSession`` 注入，方便测试。
"""

from __future__ import annotations

# pyright: basic
import logging
import math
import random
import time
from typing import Any, Self
from urllib.parse import urlencode

import aiohttp

from platforms.xiaohongshu.signer import get_xhs_sign
from shared.cookie_utils import build_cookie_str, extract_cookie_value, parse_cookie_str, parse_set_cookie_headers
from shared.exceptions import (
    CaptchaError,
    DataError,
    IpBlockError,
    RetryableError,
)

logger = logging.getLogger(__name__)

XHS_API_BASE = "https://edith.xiaohongshu.com"
XHS_HOME_URL = "https://www.xiaohongshu.com"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


# ── 搜索 ID 生成 helpers ──────────────────────────────────────


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


class XhsClient:
    """小红书 API 客户端。所有 HTTP 请求通过此类统一发送。

    Args:
        cookie: Cookie 字符串 (``"k1=v1; k2=v2"``) 或字典
        session: 可选的 ``aiohttp.ClientSession`` 注入（测试用）。
            ``None`` 则懒创建自己的 session。
    """

    # XHS 不同的 API 使用不同的 base domain（大部分走 edith，极少走 www）
    _BASE_MAP: dict[str, str] = {
        "/api/sec/v1/shield/webprofile": XHS_HOME_URL,
    }

    @staticmethod
    def _base_for(api: str) -> str:
        """Return the correct base URL for a given API path."""
        return XhsClient._BASE_MAP.get(api, XHS_API_BASE)

    def __init__(
        self,
        cookie: str | dict[str, str],
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if isinstance(cookie, dict):
            self._cookie_str = build_cookie_str(cookie)
            self._cookie_dict = dict(cookie)
        else:
            self._cookie_str = cookie
            self._cookie_dict = {}
        self._a1 = extract_cookie_value(cookie, "a1")
        self._session = session
        self._owns_session = session is None

    @property
    def cookies(self) -> dict[str, str]:
        """当前 cookies 快照（只读）。"""
        if self._cookie_dict:
            return dict(self._cookie_dict)
        return parse_cookie_str(self._cookie_str)

    # ── Internal ───────────────────────────────────────────

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
            self._owns_session = True
        return self._session

    async def _request(
        self,
        method: str,
        api: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        _set_cookie_collect: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """签名 + 发送 + 错误转译的统一入口。

        Args:
            method: HTTP 方法
            api: API 路径 (如 ``/api/sns/web/v1/user_posted``)
            params: URL 查询参数 (GET)
            json: JSON body (POST)
            _set_cookie_collect: 若传入 dict，将在响应后收集 Set-Cookie 头写入其中。

        Returns:
            ``data["data"]`` (API 响应中的 data 字段)

        Raises:
            IpBlockError: code=300012
            CaptchaError: HTTP 461/471
            RetryableError: HTTP 403/429/5xx
            DataError: 其他业务错误 (success=false, 4xx)
        """
        session = await self._ensure_session()

        # ── 构造签名 ──
        headers: dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "Cookie": self._cookie_str,
        }

        # Build the URL with query string if params
        base = self._base_for(api)
        if params:
            # NOTE: must keep commas unencoded (image_formats) or the signature
            # won't match server-side.
            query = urlencode(params, doseq=True, safe=",")
            url = f"{base}{api}?{query}"
        else:
            url = f"{base}{api}"

        # Sign — pass the BASE api path (no query) for signing. xhshow internally
        # deals with params/payload separately.
        data_for_sign: dict[str, Any] = (json or {}) if method == "POST" else (params or {})
        sign_headers = get_xhs_sign(
            api,
            data=data_for_sign,
            a1=self._a1,
            method=method,  # type: ignore[arg-type]
        )
        headers.update(sign_headers)

        # ── 发送 ──
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                params=None,  # already in url
                json=json,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if _set_cookie_collect is not None:
                    raw = resp.headers.getall("Set-Cookie", [])
                    _set_cookie_collect.update(parse_set_cookie_headers(raw))

                if resp.status == 461 or resp.status == 471:
                    raise CaptchaError(f"XHS captcha challenge (HTTP {resp.status})")
                if resp.status == 429:
                    raise RetryableError(f"Rate limited (HTTP {resp.status})")
                if resp.status in (403,):
                    raise RetryableError(f"Access denied (HTTP {resp.status})")
                if resp.status >= 500:
                    raise RetryableError(f"Server error (HTTP {resp.status})")
                if resp.status >= 400:
                    body = await resp.text()
                    raise DataError(f"HTTP {resp.status}: {body[:200]}")

                data = await resp.json(content_type=None)

            # ── 结果翻译 ──
            if not isinstance(data, dict):
                raise DataError(f"Unexpected response type: {type(data).__name__}")

            # Check for IP block first (can appear even on 200)
            code = data.get("code", 0)
            if code == 300012:
                raise IpBlockError("XHS IP blocked (code=300012)")

            if not data.get("success", False):
                msg = data.get("msg", "unknown error")
                raise DataError(f"XHS API error: {msg}")

            return data.get("data", {})
        except (aiohttp.ClientError, OSError) as e:
            raise RetryableError(f"Network error: {e}") from e

    # ── Content APIs ───────────────────────────────────────

    async def get_user_notes(
        self,
        user_id: str,
        cursor: str = "",
        num: int = 20,
    ) -> list[dict[str, Any]]:
        """获取用户笔记列表 (GET /api/sns/web/v1/user_posted)。

        Returns:
            笔记数据列表 (每个 dict 对应一条笔记)
        """
        params: dict[str, str | int] = {
            "num": str(num),
            "cursor": cursor,
            "user_id": user_id,
            "image_formats": "jpg,webp,avif",
            "xsec_token": "",
            "xsec_source": "pc_feed",
        }
        result = await self._request("GET", "/api/sns/web/v1/user_posted", params=params)
        notes = result.get("notes", [])
        return notes if isinstance(notes, list) else []

    async def get_note_detail(self, note_id: str, xsec_token: str) -> dict[str, Any]:
        """获取笔记详情 (POST /api/sns/web/v1/feed)。

        Returns:
            笔记卡片 dict (从 items[0].note_card 解包)
        """
        payload: dict[str, Any] = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": "pc_share",
            "xsec_token": xsec_token,
        }
        result = await self._request("POST", "/api/sns/web/v1/feed", json=payload)
        items = result.get("items", [])
        if items and isinstance(items, list):
            return items[0].get("note_card", items[0])
        return {}

    async def get_comments(self, note_id: str, xsec_token: str = "", cursor: str = "") -> dict[str, Any]:
        """获取笔记评论 (GET /api/sns/web/v2/comment/page)。

        Returns:
            dict 含 ``comments`` 列表和 ``cursor`` 分页标记
        """
        params: dict[str, str] = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
        }
        if xsec_token:
            params["xsec_token"] = xsec_token
        return await self._request("GET", "/api/sns/web/v2/comment/page", params=params)

    async def search_users(self, keyword: str, page: int = 1) -> list[dict[str, Any]]:
        """搜索用户 (POST /api/sns/web/v1/search/usersearch)。

        Args:
            keyword: 搜索关键词（用户昵称）
            page: 页码

        Returns:
            用户列表，每项含 user_id / nickname / avatar 等字段
        """
        data = {
            "search_user_request": {
                "keyword": keyword,
                "search_id": _generate_search_id(),
                "page": page,
                "page_size": 15,
                "biz_type": "web_search_user",
                "request_id": _generate_search_request_id(),
            }
        }
        result = await self._request("POST", "/api/sns/web/v1/search/usersearch", json=data)
        users = result.get("users", [])
        return users if isinstance(users, list) else []

    # ── Auth APIs ──────────────────────────────────────────

    async def get_user_info(self) -> dict[str, Any]:
        """获取当前用户信息 (GET /api/sns/web/v2/user/me)。

        Returns:
            ``{"nickname": "...", "user_id": "...", ...}``
        """
        result = await self._request("GET", "/api/sns/web/v2/user/me")
        return result if isinstance(result, dict) else {}

    async def create_qrcode(self, init_cookies: dict[str, str]) -> dict[str, Any]:
        """创建 QR 登录二维码 (POST /api/sns/web/v1/login/qrcode/create)。

        Args:
            init_cookies: 初始 cookies (a1, web_id, sec_poison_id, gid 等)

        Returns:
            dict 含 ``qr_id``, ``qr_url``, ``code``
        """
        # Temporarily switch to the init cookies for this request
        old_str, old_dict = self._cookie_str, self._cookie_dict
        self._cookie_str = build_cookie_str(init_cookies)
        self._cookie_dict = dict(init_cookies)
        self._a1 = init_cookies.get("a1", "")
        try:
            payload = {"qr_type": 1}
            return await self._request("POST", "/api/sns/web/v1/login/qrcode/create", json=payload)
        finally:
            self._cookie_str, self._cookie_dict = old_str, old_dict
            self._a1 = extract_cookie_value(old_str if not old_dict else old_dict, "a1")

    async def check_qrcode_status(self, qr_id: str, code: str) -> dict[str, Any]:
        """轮询 QR 登录状态 (GET /api/sns/web/v1/login/qrcode/status)。

        Returns:
            dict 含 ``status`` (1=waiting, 2=scanned, 3=success, 4=expired) 和可能的 ``cookies``
        """
        params = {"qr_id": qr_id, "code": code}
        return await self._request("GET", "/api/sns/web/v1/login/qrcode/status", params=params)

    async def fetch_sec_cookies(self, init_cookies: dict[str, str]) -> dict[str, str]:
        """获取安全相关 cookies (sec_poison_id, gid)。

        在 QR 登录前调用。两个 API 调用各为独立请求；任一失败不影响另一个。
        不成功时返回空 dict。

        Args:
            init_cookies: 初始 cookies

        Returns:
            ``{"sec_poison_id": "...", "gid": "..."}`` 或空 dict
        """
        result: dict[str, str] = {}
        old_str, old_dict = self._cookie_str, self._cookie_dict
        old_a1 = self._a1
        self._cookie_str = build_cookie_str(init_cookies)
        self._cookie_dict = dict(init_cookies)
        self._a1 = init_cookies.get("a1", "")

        try:
            # sec/scripting
            try:
                payload = {"callFrom": "web", "callback": "", "type": "ds", "appId": "xhs-pc-web"}
                resp_data = await self._request("POST", "/api/sec/v1/scripting", json=payload)
                sec_id = resp_data.get("secPoisonId")
                if sec_id:
                    result["sec_poison_id"] = sec_id
            except Exception:
                logger.debug("fetch_sec_cookies: sec/scripting failed", exc_info=True)

            # sec/shield/webprofile
            try:
                shield_data = {
                    "platform": "Windows",
                    "sdkVersion": "4.3.5",
                    "svn": "2",
                    "profileData": "",
                }
                collected: dict[str, str] = {}
                await self._request(
                    "POST",
                    "/api/sec/v1/shield/webprofile",
                    json=shield_data,
                    _set_cookie_collect=collected,
                )
                if "gid" in collected:
                    result["gid"] = collected["gid"]
            except Exception:
                logger.debug("fetch_sec_cookies: shield/webprofile failed", exc_info=True)
        finally:
            self._cookie_str, self._cookie_dict = old_str, old_dict
            self._a1 = old_a1

        return result

    async def probe(self) -> bool:
        """验证 cookie 是否仍被服务器接受。

        Returns:
            ``True`` 如果 cookie 有效且有 nickname
        """
        try:
            user_info = await self.get_user_info()
            return bool(user_info.get("nickname"))
        except Exception:
            return False

    async def refresh_cookies(self) -> dict[str, str] | None:
        """通过访问主页捕获 Set-Cookie 来刷新 cookie 值。

        Spike result (2026-06-15): ``GET https://www.xiaohongshu.com/explore``
        确实返回 Set-Cookie 头。如果服务器未返回新 cookie，返回 ``None``。

        Returns:
            ``dict`` 更新的 cookie 值 (可合并到现有 cookies)，或 ``None``
        """
        session = await self._ensure_session()
        headers: dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Cookie": self._cookie_str,
        }
        try:
            async with session.get(
                f"{XHS_HOME_URL}/explore",
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                raw_headers = resp.headers.getall("Set-Cookie", [])
                if not raw_headers:
                    return None
                return parse_set_cookie_headers(raw_headers)
        except Exception:
            logger.debug("refresh_cookies failed", exc_info=True)
            return None

    # ── Lifecycle ──────────────────────────────────────────

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """关闭拥有的 session（不关闭注入的 session）。"""
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
