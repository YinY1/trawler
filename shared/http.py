"""HTTP 客户端 — httpx 工厂

设计：
- ``create_client()`` 是唯一的 HTTP 客户端创建方式
- SSL 验证受 ``config.general.disable_ssl_verify`` 控制
- 每次调用返回独立 ``httpx.AsyncClient``（上下文管理器确保自动关闭）
"""

from __future__ import annotations

from typing import Any

import httpx

import shared.config as cfg


def create_client(
    *,
    verify: bool | None = None,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """创建预配置的 httpx.AsyncClient。

    Args:
        verify: SSL 证书验证（覆盖配置值），None 表示使用配置值
        **kwargs: 透传给 httpx.AsyncClient 的参数。

    Returns:
        已配置的 httpx.AsyncClient 实例。

    Usage::

        from shared.http import create_client

        async with create_client() as client:
            resp = await client.get("https://api.example.com/data")

        # 或手动管理生命周期：
        client = create_client()
        try:
            resp = await client.get("https://api.example.com/data")
        finally:
            await client.aclose()
    """
    kwargs.setdefault("timeout", httpx.Timeout(30.0))
    if verify is None:
        try:
            _cfg = cfg.load_config()
            kwargs.setdefault("verify", not _cfg.general.disable_ssl_verify)
        except Exception:
            kwargs.setdefault("verify", True)
    else:
        kwargs["verify"] = verify
    return httpx.AsyncClient(**kwargs)
