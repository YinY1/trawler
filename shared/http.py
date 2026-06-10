"""全局 aiohttp ClientSession 管理器

提供复用的 ClientSession，避免在每次请求时创建新 session。
"""

from __future__ import annotations

import aiohttp

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    """获取全局 aiohttp.ClientSession（懒创建，自动复用）。"""
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    """关闭全局 aiohttp.ClientSession 并重置引用。"""
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None
