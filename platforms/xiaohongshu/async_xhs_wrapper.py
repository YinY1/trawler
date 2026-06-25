"""Async wrapper around the synchronous ReaJason/xhs library.

Design:
- All public methods of XhsClient are wrapped with asyncio.to_thread so
  callers stay in async land without holding the event loop.
- Does NOT expose the underlying requests.Session — auth.py talks only
  to the high-level methods declared here.
- The wrapper owns the XhsClient lifecycle; close() releases its session.

Rationale: see docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md
"""

from __future__ import annotations

# pyright: basic
import asyncio
import logging
from typing import Any

from xhs.core import XhsClient

from platforms.xiaohongshu.signer import get_xhs_sign

logger = logging.getLogger("trawler.xiaohongshu.async_wrapper")


def _sign_adapter(url: str, data: Any = None, *, a1: str = "", web_session: str = "") -> dict[str, str]:
    """适配 xhs 库 external_sign 调用约定 → 项目现有 get_xhs_sign。

    xhs 库 ``_pre_headers`` 对主 API 端点(is_creator=False)会调用
    ``external_sign(url, data, a1=..., web_session=...)`` 期望返回头 dict。

    xhs 库传入的 ``url`` 实际形态:
      - POST: 纯 path ``/api/sns/web/v1/login/qrcode/create`` (data 是 dict)
      - GET : path + query ``/api/...?qr_id=x&code=y`` (data 是 None)

    本 adapter 据此推断 method 并委托给项目 ``get_xhs_sign``(底层 xhshow,
    和原 aiohttp XhsClient 走同一签名实现,保证一致性)。
    """
    is_get = data is None and "?" in url
    method = "GET" if is_get else "POST"
    # get_xhs_sign 期望 api 是纯 path(不含 query),GET 时它内部会从 data 取 params
    api = url.split("?", 1)[0] if "?" in url else url
    return get_xhs_sign(api, data if isinstance(data, dict) else "", a1, method)


class AsyncXhsClient:
    """Asynchronous facade over the synchronous xhs.XhsClient.

    Each method delegates to the underlying sync client via asyncio.to_thread.
    Exceptions from xhs.exception (DataFetchError / IPBlockError / SignError /
    NeedVerifyError, all subclasses of requests.RequestException) propagate
    unchanged; translation to trawler's exception hierarchy is the caller's
    responsibility (see platforms.xiaohongshu.auth._wrap_xhs_call).

    Args:
        cookie: Initial cookie string (``"k1=v1; k2=v2"``). May be empty;
            empty/None lets the underlying library start with a clean jar.
    """

    def __init__(self, cookie: str = "") -> None:
        # external_sign 必须是 callable: xhs 库 _pre_headers 对主 API 端点
        # (is_creator=False) 会调用 external_sign(url, data, a1=, web_session=)。
        # 用 xhs.help.sign 包一个适配器,算法是 xhs 库原生(canvas 指纹 + md5)。
        self._client: XhsClient | None = XhsClient(cookie=cookie or None, sign=_sign_adapter)

    async def get_qrcode(self) -> dict[str, Any]:
        """生成 QR 二维码。返回 ``{qr_id, code, url, multi_flag}``。"""
        assert self._client is not None
        # xhs lib 运行时返回 dict, 但类型存根声明为 Response | Any (lib typing 不准).
        return await asyncio.to_thread(self._client.get_qrcode)  # type: ignore[return-value]

    async def check_qrcode(self, qr_id: str, code: str) -> dict[str, Any]:
        """轮询 QR 状态。返回含 ``code_status`` 字段(2=success)。"""
        assert self._client is not None
        return await asyncio.to_thread(  # type: ignore[return-value]
            self._client.check_qrcode, qr_id, code
        )

    async def activate(self) -> dict[str, Any]:
        """激活 session,拿 web_session 写入 cookie jar。"""
        assert self._client is not None
        return await asyncio.to_thread(self._client.activate)  # type: ignore[return-value]

    async def get_self_info(self) -> dict[str, Any]:
        """获取当前登录用户信息(含 nickname)。"""
        assert self._client is not None
        return await asyncio.to_thread(self._client.get_self_info)  # type: ignore[return-value]

    @property
    def cookie(self) -> str:
        """当前 cookie jar 字符串(``"k1=v1; k2=v2"``)。"""
        assert self._client is not None
        return self._client.cookie

    async def close(self) -> None:
        """关闭内部 xhs 库的 requests.Session。

        XhsClient 自身没有 close() 方法,wrapper 调它的 session.close()。
        防御性实现:P1-1b 已硬性核实 XhsClient 暴露 .session attribute;
        即使未来版本变更,getattr 兜底保证不抛异常。
        """
        sync_client = self._client
        self._client = None
        if sync_client is None:
            return
        session = getattr(sync_client, "session", None)
        if session is not None:
            await asyncio.to_thread(session.close)
