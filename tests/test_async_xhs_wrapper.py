"""Tests for AsyncXhsClient — verify asyncio.to_thread wrapping of sync xhs library.

Strategy: patch the underlying xhs.core.XhsClient class, then assert the async
wrapper delegates to the right method and returns/raises the same value/error.
Does NOT test the real xhs library HTTP layer.

See docs/superpowers/plans/2026-06-26-xhs-auth-xhs-library-migration.md (Phase 1).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
from shared.exceptions import DataError

# ── ──


class TestGetQrcode:
    async def test_delegates_to_get_qrcode_and_returns_dict(self) -> None:
        """get_qrcode returns {qr_id, code, url, multi_flag} from underlying client."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_qrcode.return_value = {
                "qr_id": "q1",
                "code": "c1",
                "url": "u1",
                "multi_flag": 0,
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_qrcode()

            mock_instance.get_qrcode.assert_called_once_with()
            assert result == {"qr_id": "q1", "code": "c1", "url": "u1", "multi_flag": 0}


class TestCheckQrcode:
    async def test_delegates_with_qr_id_and_code(self) -> None:
        """check_qrcode passes (qr_id, code) positionally; returns raw dict.

        Regression: real field name is snake_case 'code_status' (spec §1.2 根因 #1).
        """
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.check_qrcode.return_value = {"code_status": 2, "code_msg": "ok"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.check_qrcode("qr_abc", "code_123")

            mock_instance.check_qrcode.assert_called_once_with("qr_abc", "code_123")
            assert result["code_status"] == 2


class TestActivate:
    async def test_delegates_to_activate(self) -> None:
        """activate runs with no args, returns web_session info."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.activate.return_value = {"web_session": "ws"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.activate()

            mock_instance.activate.assert_called_once_with()
            assert result == {"web_session": "ws"}


class TestGetSelfInfo:
    async def test_delegates_to_get_self_info_returns_dict(self) -> None:
        """get_self_info returns user info (e.g. nickname, user_id)."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_self_info.return_value = {"nickname": "alice", "user_id": "u1"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_self_info()

            mock_instance.get_self_info.assert_called_once_with()
            assert result["nickname"] == "alice"


class TestCookieProperty:
    async def test_cookie_getter_returns_underlying_str(self) -> None:
        """cookie property is a transparent passthrough of underlying str."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            # The underlying cookie property returns a "k=v;k=v" string
            type(mock_instance).cookie = property(lambda self: "a1=v1; web_session=ws")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            assert client.cookie == "a1=v1; web_session=ws"


class TestClose:
    async def test_close_closes_underlying_session(self) -> None:
        """XhsClient has no close(); wrapper must close its .session instead."""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_session = MagicMock()
            mock_instance.session = mock_session
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.close()

            mock_session.close.assert_called_once_with()


class TestExceptionPassthrough:
    async def test_xhs_data_fetch_error_propagates(self) -> None:
        """xhs library exceptions must propagate unchanged (translation is auth.py's job)."""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_self_info.side_effect = DataFetchError("boom")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            with pytest.raises(DataFetchError, match="boom"):
                await client.get_self_info()


class TestSuppressXhsStdout:
    """Cover the ``_suppress_xhs_stdout`` contextmanager (zero coverage before)."""

    def test_captures_print_without_polluting_stdout(self) -> None:
        """被包装函数的 print → 被 sink 捕获,不污染真实 stdout。

        验证两点:
        1. with 块内执行不抛异常;
        2. 退出后 ``sys.stdout`` 已恢复(被 ``redirect_stdout`` 的 ``__exit__``
           还原为原对象)。
        """
        import sys

        from platforms.xiaohongshu.async_xhs_wrapper import _suppress_xhs_stdout

        old_stdout = sys.stdout
        with _suppress_xhs_stdout():
            print("this should not reach real stdout")  # noqa: T201
        # 真实 stdout 没被污染(restored by redirect_stdout __exit__)
        assert sys.stdout is old_stdout

    def test_exception_propagates(self) -> None:
        """被包装函数抛异常 → 异常正常穿出,不被吞。

        这是 contextmanager 的硬性契约:xhs 库的 DataFetchError / IPBlockError
        必须穿出本上下文交由 auth.py 翻译。
        """

        from platforms.xiaohongshu.async_xhs_wrapper import _suppress_xhs_stdout

        class CustomError(Exception):
            pass

        with pytest.raises(CustomError, match="should propagate"):
            with _suppress_xhs_stdout():
                raise CustomError("should propagate")


class TestGetUserNotes:
    """get_user_notes: 单页取用户笔记列表,返回完整 data dict(不解包)。"""

    async def test_delegates_positional_user_id_and_cursor(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_notes.return_value = {
                "notes": [{"note_id": "n1"}],
                "cursor": "next",
                "has_more": True,
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_user_notes("u1", cursor="c1")

            mock_instance.get_user_notes.assert_called_once_with("u1", "c1")
            assert result["notes"] == [{"note_id": "n1"}]
            assert result["has_more"] is True

    async def test_default_cursor_is_empty_string(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_notes.return_value = {"notes": []}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_user_notes("u1")

            mock_instance.get_user_notes.assert_called_once_with("u1", "")

    async def test_translates_data_fetch_error_to_data_error(self) -> None:
        """wrapper 方法现在自带 _wrap_xhs_call 翻译(spec §3.1.2 下沉后)。"""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_notes.side_effect = DataFetchError("denied")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            with pytest.raises(DataError, match="denied"):
                await client.get_user_notes("u1")


class TestGetNoteById:
    """get_note_by_id: 直接 POST /api/sns/web/v1/feed,从 items[0].note_card 解包。"""

    async def test_default_body_has_source_note_id_and_image_scenes(self) -> None:
        """只传 note_id → body 含 source_note_id + image_scenes=["CRD_WM_WEBP"]。

        xsec_source 默认 "pc_feed" 也会进 body(``if xsec_source:`` 恒真)。
        """
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = {"note_id": "n1"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_note_by_id("n1")

            mock_instance.post.assert_called_once_with(
                "/api/sns/web/v1/feed",
                {
                    "source_note_id": "n1",
                    "image_scenes": ["CRD_WM_WEBP"],
                    "xsec_source": "pc_feed",
                },
            )

    async def test_includes_xsec_token_and_source_when_provided(self) -> None:
        """传 xsec_token / xsec_source → body 追加这两个字段。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = {"note_id": "n1"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_note_by_id("n1", xsec_token="t1", xsec_source="pc_share")

            mock_instance.post.assert_called_once_with(
                "/api/sns/web/v1/feed",
                {
                    "source_note_id": "n1",
                    "image_scenes": ["CRD_WM_WEBP"],
                    "xsec_token": "t1",
                    "xsec_source": "pc_share",
                },
            )

    async def test_returns_note_card_unchanged(self) -> None:
        """post 返回值没有 items key → 原样透传。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = {"note_id": "n1", "desc": "x"}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_note_by_id("n1")

            assert result == {"note_id": "n1", "desc": "x"}

    async def test_unwraps_items_0_note_card(self) -> None:
        """post 返回 {items: [{note_card: {...}}]} → 解包出 items[0].note_card。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = {
                "items": [{"note_card": {"note_id": "n1"}}]
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_note_by_id("n1")

            assert result == {"note_id": "n1"}

    async def test_translates_data_fetch_error_to_data_error(self) -> None:
        """wrapper 方法现在自带 _wrap_xhs_call 翻译(spec §3.1.2 下沉后)。"""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.side_effect = DataFetchError("denied")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            with pytest.raises(DataError, match="denied"):
                await client.get_note_by_id("n1")


class TestGetNoteComments:
    """get_note_comments: 单页评论,cursor 在 xsec_token 前(对齐库签名)。"""

    async def test_delegates_with_note_id_cursor_xsec_token_order(self) -> None:
        """库签名是 (note_id, cursor),xsec_token 不传(库内部处理)。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_note_comments.return_value = {
                "comments": [{"id": "c1"}],
                "cursor": "next",
                "has_more": False,
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_note_comments("n1", cursor="cur", xsec_token="t1")

            mock_instance.get_note_comments.assert_called_once_with("n1", "cur")

    async def test_defaults_cursor_and_token_empty(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_note_comments.return_value = {"comments": []}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_note_comments("n1")

            mock_instance.get_note_comments.assert_called_once_with("n1", "")

    async def test_returns_full_dict_with_has_more(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_note_comments.return_value = {
                "comments": [],
                "has_more": True,
                "cursor": "abc",
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_note_comments("n1")

            assert result["has_more"] is True
            assert result["cursor"] == "abc"

    async def test_translates_data_fetch_error_to_data_error(self) -> None:
        """wrapper 方法现在自带 _wrap_xhs_call 翻译(spec §3.1.2 下沉后)。"""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_note_comments.side_effect = DataFetchError("denied")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            with pytest.raises(DataError, match="denied"):
                await client.get_note_comments("n1")


class TestGetUserByKeyword:
    """get_user_by_keyword: 搜索用户,返回 {users: [...]} dict。"""

    async def test_delegates_keyword_and_page(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_by_keyword.return_value = {
                "users": [{"user_id": "u1", "nickname": "alice"}]
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_user_by_keyword("alice", page=2)

            mock_instance.get_user_by_keyword.assert_called_once_with("alice", 2)

    async def test_default_page_is_one(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_by_keyword.return_value = {"users": []}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.get_user_by_keyword("test")

            mock_instance.get_user_by_keyword.assert_called_once_with("test", 1)

    async def test_returns_users_dict_unchanged(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_by_keyword.return_value = {
                "users": [{"user_id": "u1"}]
            }
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.get_user_by_keyword("test")

            assert result["users"] == [{"user_id": "u1"}]

    async def test_translates_data_fetch_error_to_data_error(self) -> None:
        """wrapper 方法现在自带 _wrap_xhs_call 翻译(spec §3.1.2 下沉后)。"""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_user_by_keyword.side_effect = DataFetchError("denied")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            with pytest.raises(DataError, match="denied"):
                await client.get_user_by_keyword("test")


class TestSignAdapterGetIncludesQuery:
    """GET 请求签名必须包含 query params（否则服务端返回 406）。"""

    def test_get_request_passes_parsed_params_to_signer(self) -> None:
        """GET URL 的 query params 必须传给签名函数，不能传空字符串。"""
        from platforms.xiaohongshu.async_xhs_wrapper import _sign_adapter

        with patch("platforms.xiaohongshu.async_xhs_wrapper.get_xhs_sign") as mock_sign:
            mock_sign.return_value = {"x-s": "test", "x-t": "123"}
            url = "/api/sns/web/v1/user_posted?num=30&cursor=&user_id=u1&image_scenes=FD_WM_WEBP"
            _sign_adapter(url, data=None, a1="test_a1")

        mock_sign.assert_called_once()
        call_args = mock_sign.call_args
        # 第二个参数应该是解析后的 params dict，不是空字符串
        params = call_args.args[1]
        assert isinstance(params, dict)
        assert params["num"] == "30"
        assert params["user_id"] == "u1"
        assert params["cursor"] == ""  # keep_blank_values=True 保留空值
        assert params["image_scenes"] == "FD_WM_WEBP"

    def test_post_request_passes_data_dict_unchanged(self) -> None:
        """POST 请求传 data dict，不变。"""
        from platforms.xiaohongshu.async_xhs_wrapper import _sign_adapter

        with patch("platforms.xiaohongshu.async_xhs_wrapper.get_xhs_sign") as mock_sign:
            mock_sign.return_value = {"x-s": "test", "x-t": "123"}
            _sign_adapter("/api/sns/web/v1/feed", data={"source_note_id": "n1"}, a1="test_a1")

        mock_sign.assert_called_once()
        call_args = mock_sign.call_args
        assert call_args.args[1] == {"source_note_id": "n1"}


class TestAsyncXhsClientCaptcha:
    """captcha_init / captcha_query_status: 风控扫码二次验证。

    与其他方法不同:这两个方法**不**走 ``_wrap_xhs_call`` 装饰器,
    而是自带 try/except 把 DataFetchError / NeedVerifyError / SignError /
    IPBlockError 统一归一化成 ``{}``(空 dict),并把非 dict 返回值也归一化成 ``{}``。
    """

    async def test_captcha_init_posts_correct_endpoint(self) -> None:
        """POST /api/redcaptcha/v2/qr/init,body 含 verifyType/verifyUuid/verifyBiz/sourceSite。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = {"data": {"rid": "r1"}}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.captcha_init("type1", "uuid1")

            mock_instance.post.assert_called_once_with(
                "/api/redcaptcha/v2/qr/init",
                {
                    "verifyType": "type1",
                    "verifyUuid": "uuid1",
                    "verifyBiz": "471",
                    "sourceSite": "",
                },
            )

    async def test_captcha_init_normalizes_none_to_empty_dict(self) -> None:
        """xhs 库返回 ``{"success": true, "data": null}`` 时 post() 返回 None → wrapper 归一化成 {}。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = None
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_init("type1", "uuid1")

            assert result == {}

    async def test_captcha_init_normalizes_non_dict_to_empty_dict(self) -> None:
        """非 dict 返回值(如 requests.Response 对象)→ 归一化成 {}。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            # requests.Response 是典型的非 dict 返回值陷阱
            mock_instance.post.return_value = MagicMock(name="fake_response")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_init("type1", "uuid1")

            assert result == {}

    async def test_captcha_init_returns_dict_unchanged_when_dict(self) -> None:
        """正常 dict 返回值原样透传(含 data.rid)。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            expected = {"success": True, "data": {"rid": "rid-abc"}}
            mock_instance.post.return_value = expected
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_init("type1", "uuid1")

            assert result is expected

    async def test_captcha_init_datafetch_error_returns_empty_dict(self) -> None:
        """DataFetchError 被方法内部 try/except 吞掉,返回 {}(不向外抛)。"""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.side_effect = DataFetchError("denied")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_init("type1", "uuid1")

            assert result == {}

    async def test_captcha_query_status_posts_correct_endpoint(self) -> None:
        """POST /api/redcaptcha/v2/qr/status/query,body 在 captcha_init 基础上多 rid 字段。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = {"data": {"status": 4}}
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            await client.captcha_query_status("type1", "uuid1", "rid-abc")

            mock_instance.post.assert_called_once_with(
                "/api/redcaptcha/v2/qr/status/query",
                {
                    "verifyType": "type1",
                    "verifyUuid": "uuid1",
                    "verifyBiz": "471",
                    "sourceSite": "",
                    "rid": "rid-abc",
                },
            )

    async def test_captcha_query_status_normalizes_none_to_empty_dict(self) -> None:
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.return_value = None
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_query_status("type1", "uuid1", "rid-abc")

            assert result == {}

    async def test_captcha_query_status_returns_status_code(self) -> None:
        """正常 dict(status=4 表示已确认)原样透传。"""
        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            expected = {"data": {"status": 4}}
            mock_instance.post.return_value = expected
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_query_status("type1", "uuid1", "rid-abc")

            assert result is expected
            assert result["data"]["status"] == 4

    async def test_captcha_query_status_datafetch_error_returns_empty_dict(self) -> None:
        """DataFetchError 同样被吞,返回 {}。"""
        from xhs.exception import DataFetchError

        with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.post.side_effect = DataFetchError("denied")
            mock_cls.return_value = mock_instance

            client = AsyncXhsClient(cookie="")
            result = await client.captcha_query_status("type1", "uuid1", "rid-abc")

            assert result == {}


class TestWrapXhsCallLivesInWrapper:
    """_wrap_xhs_call 现在住在 async_xhs_wrapper(spec §3.1.2 下沉)。"""

    def test_decorator_is_importable_from_wrapper(self) -> None:
        from platforms.xiaohongshu.async_xhs_wrapper import _wrap_xhs_call

        assert callable(_wrap_xhs_call)

    async def test_decorator_translates_ipblock_error_case_sensitive(self) -> None:
        """xhs.exception.IPBlockError(大写 P) → shared.exceptions.IpBlockError(小写 p)。

        这是 spec §3.1.2 强调的大小写差异回归点。
        """
        from xhs.exception import IPBlockError

        from platforms.xiaohongshu.async_xhs_wrapper import _wrap_xhs_call
        from shared.exceptions import IpBlockError

        @_wrap_xhs_call
        async def boom() -> None:
            raise IPBlockError("blocked")

        with pytest.raises(IpBlockError, match="blocked"):
            await boom()
