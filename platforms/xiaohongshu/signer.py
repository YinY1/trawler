"""小红书 API 签名模块 - 纯 Python (xhshow)

设计：
- ``get_xhs_sign()`` 保持旧契约，返回 ``{xs, xt, xs_common}``（短键名）
- ``get_xhs_sign_full()`` 返回完整 7-key header set，供新代码使用
- 内部委托给 ``xhshow.Xhshow.sign_headers()`` 处理 GET/POST 统一签名

Spike verified (2026-06-15, xhshow 0.2.0):
- ``sign_headers(method, uri, cookies, params|payload)`` 同时支持 GET/POST
- 0.1.x 时代的 ``a3_hash`` GET 签名 bug 已在上游修复，无需 MediaCrawler 的
  ``crypto_processor.build_payload_array`` workaround
"""

from __future__ import annotations

# pyright: basic
import logging
from typing import Any, Literal
from urllib.parse import parse_qs

from xhshow import Xhshow

logger = logging.getLogger(__name__)

# Module-level singleton — Xhshow() is stateless, safe to share
_xhs = Xhshow()


def _sign(
    api: str,
    data: dict[str, Any] | str,
    a1: str,
    method: Literal["GET", "POST"],
) -> dict[str, str]:
    """Call xhshow.sign_headers and return the full header dict (hyphenated keys).

    Args:
        api: API path, e.g. "/api/sns/web/v1/user_posted"
        data: dict body (POST), dict params (GET), or query string (GET)
        a1: a1 cookie value (may be empty for unauthenticated calls)
        method: "GET" or "POST"

    Returns:
        Complete header set from xhshow (7 keys for current xhshow versions).
    """
    cookies = f"a1={a1}" if a1 else ""
    payload: dict[str, Any] | None = None
    params: dict[str, Any] | None = None

    if method == "GET":
        if isinstance(data, str) and data:
            params = {k: v[0] for k, v in parse_qs(data).items()}
        elif isinstance(data, dict):
            params = data
    else:
        payload = data if isinstance(data, dict) else None

    return _xhs.sign_headers(method=method, uri=api, cookies=cookies, params=params, payload=payload)


def get_xhs_sign(
    api: str,
    data: dict[str, Any] | str = "",
    a1: str = "",
    method: Literal["GET", "POST"] = "POST",
) -> dict[str, str]:
    """Generate XHS API signature headers (short-form 3-key contract).

    Maintained for backward compatibility with existing callers. New code should
    prefer ``get_xhs_sign_full()`` to obtain the complete header set (the XHS
    server now validates ``x-b3-traceid`` / ``x-xray-traceid`` / ``x-mns`` /
    ``xy-direction`` in addition to the legacy 3).

    Args:
        api: API path, e.g. "/api/sns/web/v1/user_posted"
        data: dict body (POST), dict params (GET), or query string (GET)
        a1: a1 cookie value (may be empty for initial requests)
        method: "GET" or "POST" (default POST)

    Returns:
        Dict with keys: ``xs``, ``xt``, ``xs_common`` (short-form names).
    """
    headers = _sign(api, data, a1, method)
    return {
        "xs": headers["x-s"],
        "xt": headers["x-t"],
        "xs_common": headers["x-s-common"],
    }


def get_xhs_sign_full(
    api: str,
    data: dict[str, Any] | str = "",
    a1: str = "",
    method: Literal["GET", "POST"] = "POST",
) -> dict[str, str]:
    """Generate XHS API signature headers (full header set).

    Like ``get_xhs_sign()`` but returns every header xhshow produces:
    ``x-s``, ``x-t``, ``x-s-common``, ``x-b3-traceid``, ``x-mns``,
    ``x-xray-traceid``, ``xy-direction``.

    Args:
        api: API path
        data: dict body (POST), dict params (GET), or query string (GET)
        a1: a1 cookie value
        method: "GET" or "POST"

    Returns:
        Complete header dict with hyphenated keys, ready to spread into HTTP
        request headers.
    """
    return _sign(api, data, a1, method)
