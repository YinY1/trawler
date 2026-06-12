"""小红书认证与签名模块 - Cookie 管理 + API 签名参数生成"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any

from rich.console import Console

from shared.config import Config

logger = logging.getLogger("trawler.xiaohongshu.auth")
console = Console()

XHS_BASE_URL = "https://www.xiaohongshu.com"

# 常用浏览器 User-Agent
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


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
