"""小红书认证与签名模块 - Cookie 管理 + API 签名参数生成"""

from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import logging
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console

from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRExpiredError,
    QRStatus,
)
from shared.auth.qr_display import display_qr_in_terminal
from shared.config import Config
from shared.constants import XHS_REQUEST_TIMEOUT

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
    console.print("[yellow]⚠ 未配置小红书 Cookie，请在 config/cookies.toml 或环境变量 XHS_COOKIE 中设置[/yellow]")
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
# Vendor XHSLoginApi helper functions
# ═══════════════════════════════════════════════════════════

_VENDOR_DIR = str(Path(__file__).resolve().parent.parent.parent / "vendor" / "spider_xhs")
_VENDOR_LOCK = threading.Lock()


def _vendor_setup() -> None:
    """设置 vendor 模块的导入路径和 Node.js 搜索路径。

    注：NODE_PATH/LOGURU_LEVEL 不在 shared/config.py 中集中配置，因为这是 vendor 模块的
    运行基础设施而非业务配置。VENDOR_DIR 从代码路径计算，不适用预配置模式。
    """
    if _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)
    os.environ["NODE_PATH"] = os.path.join(_VENDOR_DIR, "node_modules")
    os.environ["LOGURU_LEVEL"] = "ERROR"


def _vendor_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在 vendor 目录上下文中执行函数（设置 cwd + path，完成后恢复）。"""
    _vendor_setup()
    old_cwd = os.getcwd()
    with _VENDOR_LOCK:
        os.chdir(_VENDOR_DIR)
        try:
            return func(*args, **kwargs)
        finally:
            os.chdir(old_cwd)


def _vendor_init_qr() -> dict:
    """同步：通过 vendor XHSLoginApi 生成 init cookies + QR code。

    Returns:
        dict 包含 cookies, qr_id, code, qr_url
    """
    from apis.xhs_pc_login_apis import XHSLoginApi

    api = XHSLoginApi()
    cookies = api.generate_init_cookies()
    success, msg, qr_data = api.generate_qrcode(cookies)
    if not success:
        raise RuntimeError(f"生成二维码失败: {msg}")
    return {
        "cookies": qr_data["cookies"],
        "qr_id": qr_data["qr_id"],
        "code": qr_data["code"],
        "qr_url": qr_data["qr_url"],
    }


def _vendor_check_status(poll_data: dict) -> tuple[bool, str, dict]:
    """同步：通过 vendor XHSLoginApi 单次轮询 QR 状态。

    Returns:
        (success, msg, updated_cookies)
    """
    from apis.xhs_pc_login_apis import XHSLoginApi

    api = XHSLoginApi()
    return api.check_qrcode_status(poll_data["qr_id"], poll_data["code"], dict(poll_data["cookies"]))


def _vendor_poll_login(init_data: dict, deadline: float = 0.0) -> dict:
    """同步：通过 vendor XHSLoginApi 完整轮询 + 验证。

    循环轮询直至成功或过期。成功后调用 get_user_info 验证。

    Args:
        init_data: QR 初始化数据（cookies, qr_id, code）
        deadline: 截止时间（time.monotonic()），0 表示无限制

    Returns:
        最终 cookies dict（含 web_session）

    Raises:
        QRExpiredError: 二维码过期或轮询超时
    """
    from apis.xhs_pc_login_apis import XHSLoginApi

    api = XHSLoginApi()
    cookies = dict(init_data["cookies"])
    qr_id = init_data["qr_id"]
    code = init_data["code"]

    while True:
        if deadline > 0 and time.monotonic() > deadline:
            raise QRExpiredError("二维码轮询超时")
        success, msg, cookies = api.check_qrcode_status(qr_id, code, cookies)
        if success:
            break
        if msg == "二维码已过期":
            raise QRExpiredError("二维码已过期")
        time.sleep(2)

    # 验证登录
    api.get_user_info(cookies)
    return {k: v for k, v in cookies.items() if v}


# ═══════════════════════════════════════════════════════════
# XhsAuthenticator — QR 登录 + Keepalive 续期
# ═══════════════════════════════════════════════════════════


class XhsAuthenticator(BaseAuthenticator):
    """小红书 QR 扫码登录 + Keepalive 保活续期"""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._vendor_cookies: dict[str, str] = {}
        self._qr_code: str = ""

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def generate_qr_code(self) -> QRCodeResult:
        init_data = await asyncio.to_thread(_vendor_call, _vendor_init_qr)
        self._vendor_cookies = init_data["cookies"]
        self._qr_code = init_data["code"]
        return QRCodeResult(
            qr_url=init_data["qr_url"],
            qr_key=init_data["qr_id"],
            expires_in=180,
        )

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        poll_data = {
            "cookies": self._vendor_cookies,
            "qr_id": qr_key,
            "code": self._qr_code,
        }
        try:
            success, msg, cookies = await asyncio.to_thread(
                _vendor_call,
                _vendor_check_status,
                poll_data,
            )
            self._vendor_cookies = cookies
        except Exception as e:
            return AuthStatus(
                success=False,
                status=QRStatus.WAITING,
                message=f"轮询失败: {e}",
            )

        if success:
            return AuthStatus(success=True, status=QRStatus.SUCCESS, message=msg)
        if msg == "二维码已过期":
            return AuthStatus(success=False, status=QRStatus.EXPIRED, message=msg)

        status = QRStatus.SCANNED if "确认" in msg else QRStatus.WAITING
        return AuthStatus(success=False, status=status, message=msg)

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        now = time.time()
        return PlatformTokens(
            platform="xhs",
            cookies={k: v for k, v in self._vendor_cookies.items() if v},
            obtained_at=now,
            expires_at=now + 7 * 86400,
        )

    async def qr_login(
        self,
        on_status: Callable[[AuthStatus], None] | None = None,
    ) -> PlatformTokens:
        """使用 vendor XHSLoginApi 完成 QR 扫码登录全流程。"""
        _ = on_status  # vendor 内部自行处理状态

        # ── Step 1: init cookies + QR（在 thread 中运行） ──
        init_data = await asyncio.to_thread(_vendor_call, _vendor_init_qr)

        # ── Step 2: 在主线程显示 QR 码 ──
        display_qr_in_terminal(init_data["qr_url"])

        # ── Step 3: 轮询 + 获取 session（在 thread 中运行） ──
        deadline = time.monotonic() + 180
        cookies = await asyncio.to_thread(_vendor_call, _vendor_poll_login, init_data, deadline)

        now = time.time()
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
        a1 = tokens.cookies.get("a1", "")
        try:
            from platforms.xiaohongshu.signer import get_xhs_sign

            api = "/api/sns/web/v2/user/me"
            sign = get_xhs_sign(api, a1=a1, method="GET")
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Origin": XHS_HOME_URL,
                "Referer": f"{XHS_HOME_URL}/",
                "Cookie": cookie_str,
                "x-s": sign["xs"],
                "x-t": sign["xt"],
                "x-s-common": sign["xs_common"],
            }
            async with session.get(
                XHS_API_BASE + api,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                return data.get("success", False) and bool(data.get("data", {}).get("nickname"))
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
