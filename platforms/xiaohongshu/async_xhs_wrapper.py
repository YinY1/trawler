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
import contextlib
import functools
import io
import logging
import os
from collections.abc import Callable
from typing import Any, TypeVar

import requests
from xhs.core import XhsClient
from xhs.exception import DataFetchError, IPBlockError, NeedVerifyError, SignError

from platforms.xiaohongshu.signer import get_xhs_sign
from shared.dump import DUMP_ENABLED, dump_response
from shared.exceptions import CaptchaError, DataError, IpBlockError, RetryableError

logger = logging.getLogger("trawler.xiaohongshu.async_wrapper")

_F = TypeVar("_F", bound=Callable[..., Any])


def _wrap_xhs_call(func: _F) -> _F:
    """Translate xhs library exceptions to trawler's exception hierarchy.

    Mapping (spec §3.1.2) — except 顺序不可调,具体在前,RequestException 兜底最后:
      NeedVerifyError → CaptchaError
      IPBlockError    → IpBlockError   (库大写 P,项目小写 p)
      SignError       → RetryableError
      DataFetchError  → DataError
      RequestException→ RetryableError  (catch-all, ordered LAST)
      KeyError        → CaptchaError    (471/461 missing Verifytype header)
    """
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except NeedVerifyError as e:
            raise CaptchaError(
                f"XHS captcha challenge: {e}",
                verify_type=e.verify_type,
                verify_uuid=e.verify_uuid,
            ) from e
        except IPBlockError as e:
            raise IpBlockError(f"XHS IP blocked: {e}") from e
        except SignError as e:
            raise RetryableError(f"XHS sign error: {e}") from e
        except DataFetchError as e:
            raise DataError(f"XHS data fetch error: {e}") from e
        except requests.RequestException as e:
            raise RetryableError(f"XHS network error: {e}") from e
        except KeyError as e:
            raise CaptchaError(f"XHS captcha challenge (missing header): {e}") from e

    return wrapper  # type: ignore[return-value]


# TEMP DEBUG: xhs 库内部 print(data) 副作用会污染 stdout。
# 抑制(print 副作用)是无条件的——xhs 库的 print 在生产也会破坏 Rich console
# 和 JSON 输出,所以无论 dump 是否开启都必须吞掉。
# 但"把吞掉的内容 tee 到文件"是 debug-only 行为,跟 dump 工具共用 TRAWLER_DUMP
# 开关:默认关闭零开销,开启时(且 target 含 "xhs_selfinfo")才落盘。
_SUPPRESS = os.environ.get("TRAWLER_XHS_SUPPRESS_STDOUT", "1") != "0"


@contextlib.contextmanager
def _suppress_xhs_stdout() -> Any:
    """临时吞掉 xhs 库内部的 print(data) 副作用。

    抑制本身无条件(见模块 docstring);若同时开启了 dump,则把吞掉的内容
    通过 dump_response(tag="xhs_selfinfo") 落盘,失败回退到原始行为(不抛)。

    注意:不在此处用 ``except Exception`` 吞异常——xhs 库抛出的
    DataFetchError / IPBlockError 等必须穿出本上下文,由 auth.py 的
    ``_wrap_xhs_call`` 翻译到 trawler 异常体系。
    """
    if not _SUPPRESS:
        yield None
        return
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            yield sink
    finally:
        out = sink.getvalue() if sink else ""
        if out:
            dump_response("xhs_selfinfo", {"stdout": out})


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
    if is_get:
        from urllib.parse import parse_qs

        query = url.split("?", 1)[1]
        params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
        return get_xhs_sign(api, params, a1, method)
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
        self._client: XhsClient | None = XhsClient(
            cookie=cookie or None,
            sign=_sign_adapter,
        )

    async def get_qrcode(self) -> dict[str, Any]:
        """生成 QR 二维码。返回 ``{qr_id, code, url, multi_flag}``。"""
        assert self._client is not None
        # xhs lib 运行时返回 dict, 但类型存根声明为 Response | Any (lib typing 不准).
        with _suppress_xhs_stdout():
            return await asyncio.to_thread(self._client.get_qrcode)  # type: ignore[return-value]

    async def check_qrcode(self, qr_id: str, code: str) -> dict[str, Any]:
        """轮询 QR 状态。返回含 ``code_status`` 字段(2=success)。"""
        assert self._client is not None
        with _suppress_xhs_stdout():
            return await asyncio.to_thread(  # type: ignore[return-value]
                self._client.check_qrcode, qr_id, code
            )

    async def activate(self) -> dict[str, Any]:
        """激活 session,拿 web_session 写入 cookie jar。"""
        assert self._client is not None
        with _suppress_xhs_stdout():
            return await asyncio.to_thread(self._client.activate)  # type: ignore[return-value]

    async def get_self_info(self) -> dict[str, Any]:
        """获取当前登录用户信息(含 nickname)。"""
        assert self._client is not None
        with _suppress_xhs_stdout():
            result: dict[str, Any] = await asyncio.to_thread(self._client.get_self_info)  # type: ignore[assignment]
        # TEMP DEBUG DUMP: xhs 库 get_self_info 完整返回落盘
        if DUMP_ENABLED:
            dump_response("xhs_selfinfo", result)
        return result

    @_wrap_xhs_call
    async def get_user_notes(self, user_id: str, cursor: str = "") -> dict[str, Any]:
        """取用户笔记列表(单页, xhs 库写死 num=30)。

        Returns:
            完整 data dict: ``{notes, cursor, has_more}``。**不解包**,
            让调用方显式处理层级。
        """
        assert self._client is not None
        result: dict[str, Any] = await asyncio.to_thread(  # type: ignore[assignment]
            self._client.get_user_notes, user_id, cursor
        )
        if DUMP_ENABLED:
            dump_response(
                "xhs_user_notes",
                {"user_id": user_id, "cursor": cursor, "result": result},
            )
        return result

    @_wrap_xhs_call
    async def get_note_by_id(
        self, note_id: str, xsec_token: str = "", xsec_source: str = "pc_feed"
    ) -> dict[str, Any]:
        """取笔记详情。

        xhs 库的 ``get_note_by_id`` 只接受 note_id(不传 xsec_token),
        这里直接用 ``post`` 调 feed 接口以携带 token。

        Args:
            note_id: 笔记 ID
            xsec_token: 从笔记列表拿到的 token(分享链路必需)
            xsec_source: feed 链路。默认 ``pc_feed``(downloader 第一层无 token 时);
                downloader 第一层有 token 时 + 第二层(分享链路)显式传 ``pc_share``。

        Returns:
            note_card dict(xhs 库已解包 items[0].note_card)。
        """
        assert self._client is not None
        body: dict[str, Any] = {
            "source_note_id": note_id,
            "image_scenes": ["CRD_WM_WEBP"],
        }
        if xsec_token:
            body["xsec_token"] = xsec_token
        if xsec_source:
            body["xsec_source"] = xsec_source
        result: dict[str, Any] = await asyncio.to_thread(  # type: ignore[assignment]
            self._client.post, "/api/sns/web/v1/feed", body
        )
        if DUMP_ENABLED:
            dump_response(
                "xhs_note_by_id",
                {"note_id": note_id, "xsec_source": xsec_source, "result": result},
            )
        # xhs 库 get_note_by_id 会解包 items[0].note_card; 保持兼容
        if isinstance(result, dict) and "items" in result:
            items = result.get("items", [])
            if items:
                return items[0].get("note_card", result)
        return result

    @_wrap_xhs_call
    async def get_note_comments(
        self, note_id: str, cursor: str = "", xsec_token: str = ""
    ) -> dict[str, Any]:
        """取笔记评论(单页)。

        Args:
            note_id: 笔记 ID
            cursor: 分页游标(首页传空串)
            xsec_token: 笔记 token

        Returns:
            完整 data dict: ``{comments, cursor, has_more}``。
        """
        assert self._client is not None
        result: dict[str, Any] = await asyncio.to_thread(  # type: ignore[assignment]
            self._client.get_note_comments, note_id, cursor
        )
        if DUMP_ENABLED:
            dump_response(
                "xhs_note_comments",
                {"note_id": note_id, "cursor": cursor, "result": result},
            )
        return result

    @_wrap_xhs_call
    async def get_user_by_keyword(self, keyword: str, page: int = 1) -> dict[str, Any]:
        """搜索用户。

        Args:
            keyword: 搜索关键词(用户昵称)
            page: 页码(从 1 开始)

        Returns:
            完整 data dict: ``{users: [...]}``。
        """
        assert self._client is not None
        result: dict[str, Any] = await asyncio.to_thread(  # type: ignore[assignment]
            self._client.get_user_by_keyword, keyword, page
        )
        if DUMP_ENABLED:
            dump_response(
                "xhs_user_by_keyword",
                {"keyword": keyword, "page": page, "result": result},
            )
        return result

    async def captcha_init(self, verify_type: str, verify_uuid: str) -> dict[str, Any]:
        """初始化二次验证码(风控扫码),返回完整响应 dict(含 ``data.rid``)。

        xhs 库 ``request()`` 的返回值陷阱:
        - ``{"success": true, "data": null}`` → 返回 ``None`` (dict.get 有 key 但值 null)
        - 响应无 ``"success": true`` 字段 → 抛 ``DataFetchError``
        这里统一归一化成 dict。

        POST /api/redcaptcha/v2/qr/init
        """
        assert self._client is not None
        uri = "/api/redcaptcha/v2/qr/init"
        body = {
            "verifyType": verify_type,
            "verifyUuid": verify_uuid,
            "verifyBiz": "471",
            "sourceSite": "",
        }
        try:
            with _suppress_xhs_stdout():
                result = await asyncio.to_thread(self._client.post, uri, body)  # type: ignore[assignment]
                return result if isinstance(result, dict) else {}
        except (DataFetchError, NeedVerifyError, SignError, IPBlockError):
            return {}

    async def captcha_query_status(self, verify_type: str, verify_uuid: str, rid: str) -> dict[str, Any]:
        """查询二次验证码扫码状态(4=已确认)。

        POST /api/redcaptcha/v2/qr/status/query
        """
        assert self._client is not None
        uri = "/api/redcaptcha/v2/qr/status/query"
        body = {
            "verifyType": verify_type,
            "verifyUuid": verify_uuid,
            "verifyBiz": "471",
            "sourceSite": "",
            "rid": rid,
        }
        try:
            with _suppress_xhs_stdout():
                result = await asyncio.to_thread(self._client.post, uri, body)  # type: ignore[assignment]
                return result if isinstance(result, dict) else {}
        except (DataFetchError, NeedVerifyError, SignError, IPBlockError):
            return {}

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
