"""小红书认证与签名模块 - Cookie 管理 + API 签名参数生成"""

from __future__ import annotations

import binascii
import hashlib
import json
import logging
import os
import random
import time
import uuid
from typing import Any

import aiohttp
from rich.console import Console

from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)
from shared.config import Config
from shared.constants import XHS_REQUEST_TIMEOUT
from shared.http import get_session

logger = logging.getLogger("trawler.xiaohongshu.auth")
console = Console()

XHS_BASE_URL = "https://www.xiaohongshu.com"

# 常用浏览器 User-Agent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

XHS_API_BASE = "https://edith.xiaohongshu.com"
XHS_HOME_URL = "https://www.xiaohongshu.com"
XHS_QR_CREATE_API = "/api/sns/web/v1/login/qrcode/create"
XHS_QR_CHECK_API = "/api/qrcode/userinfo"
XHS_QR_STATUS_API = "/api/sns/web/v1/login/qrcode/status"
XHS_SEC_SCRIPT_API = "/api/sec/v1/scripting"
XHS_SEC_GID_API = "/api/sec/v1/shield/webprofile"
XHS_AS_BASE = "https://as.xiaohongshu.com"
_A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"


def get_xhs_cookie(config: Config) -> str:
    """从配置或环境变量获取小红书 Cookie。

    优先级: config.xiaohongshu.auth.cookie > 环境变量 XHS_COOKIE > 空字符串

    Args:
        config: 全局配置对象

    Returns:
        Cookie 字符串
    """
    cookie = config.xiaohongshu.auth.cookie
    if cookie:
        return cookie.strip()

    cookie = os.environ.get("XHS_COOKIE", "")
    if cookie:
        return cookie.strip()

    logger.warning("未配置小红书 Cookie，API 请求可能失败")
    console.print("[yellow]⚠ 未配置小红书 Cookie，请在 config.toml 或环境变量 XHS_COOKIE 中设置[/yellow]")
    return ""


def _try_vendor_sign(params: dict[str, Any], cookie: str) -> dict[str, str] | None:
    """尝试使用 vendor/spider_xhs 中的签名函数。

    Args:
        params: 请求参数
        cookie: Cookie 字符串

    Returns:
        签名头字典 (x-s, x-t, x-s-common) 或 None
    """
    try:
        # 尝试导入 vendor 目录下的签名模块
        import importlib
        import sys

        vendor_paths = [
            os.path.join(os.getcwd(), "vendor", "spider_xhs"),
            os.path.join(os.getcwd(), "vendor"),
        ]
        for vp in vendor_paths:
            if os.path.isdir(vp) and vp not in sys.path:
                sys.path.insert(0, vp)

        # 尝试多种可能的签名模块名称
        for module_name in ("sign", "xhs_sign", "encrypt", "utils"):
            try:
                mod = importlib.import_module(module_name)
                # 常见签名函数名
                for func_name in ("get_sign", "sign", "get_signed_params", "get_headers"):
                    if hasattr(mod, func_name):
                        sign_func = getattr(mod, func_name)
                        result = sign_func(params, cookie)
                        if isinstance(result, dict):
                            return result
            except (ImportError, ModuleNotFoundError):
                continue

    except Exception as e:
        logger.debug(f"vendor 签名模块不可用: {e}")

    return None


def _local_sign(params: dict[str, Any], cookie: str) -> dict[str, str]:
    """本地简易签名实现（降级方案）。

    生成基本的 x-t 时间戳和基于参数哈希的 x-s 值。
    注意：这不是小红书真正的签名算法，仅作为降级方案使用。

    Args:
        params: 请求参数
        cookie: Cookie 字符串

    Returns:
        包含 x-s, x-t, x-s-common 的头字典
    """
    timestamp = str(int(time.time()))

    # 使用参数 JSON + 时间戳 + cookie 片段生成哈希
    params_str = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    cookie_fragment = cookie[:32] if cookie else ""
    raw = f"{params_str}_{timestamp}_{cookie_fragment}"

    x_s = "XYW_" + hashlib.md5(raw.encode()).hexdigest()

    # x-s-common: base64 编码的常见参数
    common_payload = json.dumps(
        {"s0": 5, "s1": "", "x0": "1", "x1": "3.6.8", "x2": "Windows", "x3": "xhs-pc-web", "x4": "4.33.0"},
        separators=(",", ":"),
    )
    import base64

    x_s_common = base64.b64encode(common_payload.encode()).decode()

    return {
        "x-s": x_s,
        "x-t": timestamp,
        "x-s-common": x_s_common,
    }


def get_signed_params(params: dict[str, Any], cookie: str) -> dict[str, str]:
    """为小红书 API 请求生成签名参数。

    优先使用 vendor/spider_xhs 签名函数，降级为本地简易签名。

    Args:
        params: 请求参数 (body 或 query)
        cookie: Cookie 字符串

    Returns:
        签名头字典，包含 x-s, x-t, x-s-common 等
    """
    # 优先使用 vendor 签名
    signed = _try_vendor_sign(params, cookie)
    if signed:
        logger.debug("使用 vendor 签名")
        return signed

    # 降级：本地简易签名
    logger.debug("使用本地降级签名")
    return _local_sign(params, cookie)


def get_request_headers(cookie: str) -> dict[str, str]:
    """构造小红书 API 请求的完整 Headers。

    Args:
        cookie: Cookie 字符串

    Returns:
        包含 User-Agent, Referer, Cookie 等的 headers 字典
    """
    headers: dict[str, str] = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": f"{XHS_BASE_URL}/",
        "Origin": XHS_BASE_URL,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json;charset=UTF-8",
        "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    if cookie:
        headers["Cookie"] = cookie

    return headers


# ═══════════════════════════════════════════════════════════
# Helper functions for XhsAuthenticator
# ═══════════════════════════════════════════════════════════

def generate_a1() -> str:
    """Generate a random a1 cookie value (same algorithm as Spider_XHS)."""
    ts_hex = hex(int(time.time() * 1000))[2:]
    random_str = "".join(random.choices(_A1_CHARSET, k=30))
    a_part = ts_hex + random_str + "5" + "0" + "000"
    crc = binascii.crc32(a_part.encode()) & 0xFFFFFFFF
    return (a_part + str(crc))[:52]


def generate_web_id(a1: str) -> str:
    """Generate webId from a1 (MD5 hash)."""
    return hashlib.md5(a1.encode()).hexdigest()


async def _fetch_sec_cookies(session: Any, cookies: dict[str, str]) -> dict[str, str]:
    """Fetch sec_poison_id and gid for initial cookies. Called before QR generation."""
    from platforms.xiaohongshu.signer import get_xhs_sign

    result: dict[str, str] = {}
    try:
        api = XHS_SEC_SCRIPT_API
        data = {"callFrom": "web", "callback": "", "type": "ds", "appId": "xhs-pc-web"}
        sign = get_xhs_sign(api, data, cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.post(
            XHS_AS_BASE + api,
            headers=headers,
            cookies=cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            res = await resp.json(content_type=None)
            sec_id = res.get("data", {}).get("secPoisonId")
            if sec_id:
                result["sec_poison_id"] = sec_id
    except Exception:
        pass
    try:
        api = XHS_SEC_GID_API
        data = {"platform": "Windows", "sdkVersion": "4.3.5", "svn": "2", "profileData": ""}
        sign = get_xhs_sign(api, data, cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.post(
            XHS_AS_BASE + api,
            headers=headers,
            cookies=cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            for key, morsel in resp.cookies.items():
                cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            if "gid" in cookies:
                result["gid"] = cookies["gid"]
    except Exception:
        pass
    return result


# ═══════════════════════════════════════════════════════════
# XhsAuthenticator — QR 登录 + Keepalive 续期
# ═══════════════════════════════════════════════════════════

class XhsAuthenticator(BaseAuthenticator):
    """小红书 QR 扫码登录 + Keepalive 保活续期"""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._init_cookies: dict[str, str] = {}
        self._qr_code: str = ""

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = await get_session()
        return self._session

    async def generate_qr_code(self) -> QRCodeResult:
        from platforms.xiaohongshu.signer import get_xhs_sign

        session = await self._ensure_session()
        a1_val = generate_a1()
        self._init_cookies = {
            "abRequestId": str(uuid.uuid4()),
            "ets": str(int(time.time() * 1000)),
            "webBuild": "6.7.4",
            "xsecappid": "xhs-pc-web",
            "loadts": str(int(time.time() * 1000) + random.randint(50, 200)),
            "a1": a1_val,
            "webId": generate_web_id(a1_val),
        }
        sec_cookies = await _fetch_sec_cookies(session, self._init_cookies)
        self._init_cookies.update(sec_cookies)
        api = XHS_QR_CREATE_API
        data = {"qr_type": 1}
        sign = get_xhs_sign(api, data, self._init_cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.post(
            XHS_API_BASE + api,
            headers=headers,
            cookies=self._init_cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            for key, morsel in resp.cookies.items():
                self._init_cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            res = await resp.json(content_type=None)
        if not res.get("success"):
            raise RuntimeError(f"生成二维码失败: {res.get('msg', '未知错误')}")
        qr_data: dict = res.get("data") or {}
        if not all(k in qr_data for k in ("qr_id", "code", "url")):
            raise RuntimeError("生成二维码失败: 响应缺少必要字段")
        self._qr_code = qr_data["code"]
        return QRCodeResult(
            qr_url=qr_data["url"],
            qr_key=qr_data["qr_id"],
            expires_in=180,
        )

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        from platforms.xiaohongshu.signer import get_xhs_sign

        session = await self._ensure_session()
        api = XHS_QR_CHECK_API
        data = {"qrId": qr_key, "code": self._qr_code}
        sign = get_xhs_sign(api, data, self._init_cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.post(
            XHS_API_BASE + api,
            headers=headers,
            cookies=self._init_cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            for key, morsel in resp.cookies.items():
                self._init_cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            res = await resp.json(content_type=None)
        status = (res.get("data") or {}).get("codeStatus")
        status_map: dict[int, QRStatus] = {
            0: QRStatus.WAITING,
            1: QRStatus.SCANNED,
            2: QRStatus.SUCCESS,
            3: QRStatus.EXPIRED,
        }
        qr_status = status_map.get(status, QRStatus.WAITING)
        msg_map: dict[int, str] = {
            0: "等待扫码",
            1: "已扫码，等待确认",
            2: "登录成功",
            3: "二维码已过期",
        }
        if qr_status == QRStatus.SUCCESS:
            await self._fetch_login_info(qr_key, session)
        return AuthStatus(
            success=qr_status == QRStatus.SUCCESS,
            status=qr_status,
            message=msg_map.get(status, f"未知状态: {status}"),
        )

    async def _fetch_login_info(self, qr_key: str, session: aiohttp.ClientSession) -> None:
        from platforms.xiaohongshu.signer import get_xhs_sign

        api = XHS_QR_STATUS_API
        params = {"qr_id": qr_key, "code": self._qr_code}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_api = f"{api}?{query}"
        sign = get_xhs_sign(full_api, a1=self._init_cookies.get("a1", ""), method="GET")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.get(
            XHS_API_BASE + full_api,
            headers=headers,
            cookies=self._init_cookies,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            for key, morsel in resp.cookies.items():
                self._init_cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            res = await resp.json(content_type=None)
        if res.get("success") and "login_info" in res.get("data", {}):
            self._init_cookies["web_session"] = res["data"]["login_info"].get("session", "")

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        if "web_session" not in self._init_cookies:
            raise RefreshFailedError("未获取到 web_session，QR 登录可能未完成")
        now = time.time()
        cookie_keys = ["a1", "web_session", "webId", "gid", "sec_poison_id"]
        cookies = {}
        for k in cookie_keys:
            v = self._init_cookies.get(k)
            if v is not None:
                cookies[k] = v
        return PlatformTokens(
            platform="xhs",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 7 * 86400,
        )

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        session = await self._ensure_session()
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        try:
            async with session.get(
                XHS_HOME_URL,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Cookie": cookie_str},
                timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    now = time.time()
                    return PlatformTokens(
                        platform="xhs",
                        cookies=dict(tokens.cookies),
                        obtained_at=now,
                        expires_at=now + 7 * 86400,
                    )
            return tokens
        except Exception:
            return tokens

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        if tokens.expires_at < time.time():
            return False
        session = await self._ensure_session()
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        try:
            async with session.get(
                XHS_HOME_URL,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Cookie": cookie_str},
                timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.warning("小红书 token 有效性检查失败: %s", e)
            return False

    def supports_refresh(self) -> bool:
        return True


def build_tokens_from_config(config: Config) -> PlatformTokens | None:
    """Build PlatformTokens from config.xiaohongshu.auth. Returns None if not configured."""
    import time as _time
    auth = config.xiaohongshu.auth
    if not auth.cookie or auth.expires_at <= 0:
        return None
    cookie_dict: dict[str, str] = {}
    for part in auth.cookie.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookie_dict[k] = v
    if not cookie_dict:
        return None
    return PlatformTokens(
        platform="xhs",
        cookies=cookie_dict,
        obtained_at=_time.time(),
        expires_at=auth.expires_at,
    )
