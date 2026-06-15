"""Cookie 工具函数 — 解析/构建 cookie 字符串

设计：
- ``parse_cookie_str()`` 解析 ``"k1=v1; k2=v2"`` 为 ``dict``
- ``parse_set_cookie_headers()`` 解析 Set-Cookie 响应头列表（SimpleCookie + 手工 fallback）
- ``build_cookie_str()`` 将 ``dict`` 拼回 ``"k1=v1; k2=v2"``
- ``extract_cookie_value()`` 从字符串或 dict 中提取单条 cookie 值
"""

from __future__ import annotations

import re
from http.cookies import SimpleCookie

_SET_COOKIE_PAIR_RE = re.compile(r"([^=]+)=([^;]*)")


def parse_cookie_str(cookie_str: str) -> dict[str, str]:
    """Parse ``"k1=v1; k2=v2"`` into ``{"k1": "v1", "k2": "v2"}``.

    Args:
        cookie_str: Semicolon-delimited cookie string. May be empty.

    Returns:
        Dict of cookie name → value. Malformed pairs are silently dropped.
    """
    result: dict[str, str] = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            key = k.strip()
            if key:
                result[key] = v.strip()
    return result


def parse_set_cookie_headers(headers: list[str]) -> dict[str, str]:
    """Parse a list of ``Set-Cookie`` response headers into a flat dict.

    Uses ``http.cookies.SimpleCookie`` first (handles quoted values, expires,
    max-age, etc.). If ``SimpleCookie`` drops entries (XHS sometimes sends
    malformed headers with spaces around ``=``), falls back to a manual regex
    that extracts the first ``key=value`` pair.

    Args:
        headers: Raw ``Set-Cookie`` header values (from
            ``resp.headers.getall("Set-Cookie")`` or similar).

    Returns:
        Flat dict of cookie name → value. Last value wins for duplicates.
    """
    result: dict[str, str] = {}

    for raw in headers:
        # Try stdlib first
        parsed_ok = False
        try:
            sc = SimpleCookie(raw)
            for morsel in sc.values():
                result[morsel.key] = morsel.value
                parsed_ok = True
        except Exception:
            pass

        if parsed_ok:
            continue

        # Fallback: extract first key=value pair
        m = _SET_COOKIE_PAIR_RE.match(raw)
        if m:
            result[m.group(1).strip()] = m.group(2).strip()

    return result


def build_cookie_str(cookies: dict[str, str]) -> str:
    """Build ``"k1=v1; k2=v2"`` from a dict.

    Args:
        cookies: Dict of cookie name → value.

    Returns:
        Semicolon-delimited cookie string. Empty dict produces ``""``.
    """
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def extract_cookie_value(
    cookie: str | dict[str, str],
    name: str,
) -> str:
    """Extract a single cookie value by name.

    Args:
        cookie: Cookie string (``"a1=xxx; b2=yyy"``) or dict.
        name: Cookie name to extract (e.g. ``"a1"``).

    Returns:
        Cookie value, or ``""`` if not found.
    """
    if isinstance(cookie, dict):
        return cookie.get(name, "")
    return parse_cookie_str(cookie).get(name, "")
