"""Tests for comments — fetch_xhs_comment_highlights 切 AsyncXhsClient。

See docs/superpowers/plans/2026-06-26-xhs-unify.md Task 7.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from shared.protocols import CommentHighlight


class TestFetchXhsCommentHighlightsDelegation:
    """验证 comments 调 wrapper.get_note_comments 的参数顺序 + 解包。"""

    async def test_first_call_uses_empty_cursor(self) -> None:
        """首页调用:get_note_comments(note_id, cursor='', xsec_token=t)。"""
        from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

        mock_client = MagicMock()
        mock_client.get_note_comments = AsyncMock(
            return_value={"comments": [], "has_more": False, "cursor": ""}
        )
        mock_client.close = AsyncMock()

        with (
            patch("platforms.xiaohongshu.comments.AsyncXhsClient", return_value=mock_client),
            patch("platforms.xiaohongshu.comments.get_xhs_cookie", return_value="c"),
        ):
            await fetch_xhs_comment_highlights("n1", config=MagicMock(), xsec_token="t1")

        mock_client.get_note_comments.assert_awaited_once()
        call_args = mock_client.get_note_comments.call_args
        assert call_args.args[0] == "n1"
        # cursor 默认 ""(签名),原代码首页不显式传 cursor,所以 kwargs.cursor == ""
        assert call_args.kwargs.get("cursor", "") == ""
        assert call_args.kwargs.get("xsec_token") == "t1"

    async def test_second_page_uses_returned_cursor(self) -> None:
        """has_more=True + cursor 非空 + 不足 max_count → 取第二页,cursor 传入。

        patch _parse_comment 返回固定 CommentHighlight,隔离解析逻辑。

        注:本测试的 mock_client.close 是 AsyncMock 空操作,不会真把 _client 置 None,
        所以无法抓到"close 后第二页调用"的语义 bug。该 bug 的预防依赖
        fetch_xhs_comment_highlights 的代码结构(close 必须在第二页之后),
        由 test_second_page_failure_returns_first_page 间接覆盖降级行为。
        """
        from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

        mock_client = MagicMock()
        mock_client.get_note_comments = AsyncMock(
            side_effect=[
                {
                    "comments": [{"content": "c1", "user_info": {"nickname": "u1"}}],
                    "has_more": True,
                    "cursor": "page2",
                },
                {"comments": [], "has_more": False, "cursor": ""},
            ]
        )
        mock_client.close = AsyncMock()

        fake_comment = CommentHighlight(
            content="c1",
            user_name="u1",
            is_author=False,
            like_count=0,
        )
        with (
            patch("platforms.xiaohongshu.comments.AsyncXhsClient", return_value=mock_client),
            patch("platforms.xiaohongshu.comments.get_xhs_cookie", return_value="c"),
            patch("platforms.xiaohongshu.comments._parse_comment", return_value=fake_comment),
        ):
            await fetch_xhs_comment_highlights("n1", config=MagicMock(), max_count=10)

        second_call = mock_client.get_note_comments.await_args_list[1]
        assert second_call.args[0] == "n1"
        # 第二页 cursor 是 kwarg(代码: get_note_comments(note_id, cursor=cursor, ...))
        assert second_call.kwargs.get("cursor") == "page2"

    async def test_returns_empty_on_exception(self) -> None:
        """wrapper 抛异常 → 返回 [],不抛。"""
        from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

        mock_client = MagicMock()
        mock_client.get_note_comments = AsyncMock(side_effect=RuntimeError("net"))
        mock_client.close = AsyncMock()

        with (
            patch("platforms.xiaohongshu.comments.AsyncXhsClient", return_value=mock_client),
            patch("platforms.xiaohongshu.comments.get_xhs_cookie", return_value="c"),
        ):
            result = await fetch_xhs_comment_highlights("n1", config=MagicMock())

        assert result == []

    async def test_second_page_failure_returns_first_page(self) -> None:
        """第二页抛异常 → 降级返回第一页已收集结果,不抛。

        验证重构后的 try 块结构:第一页成功 + 第二页失败时,返回第一页的解析结果
        而非空列表(降级行为)。同时间接覆盖 close 必须在第二页之后执行的契约。
        """
        from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

        mock_client = MagicMock()
        mock_client.get_note_comments = AsyncMock(
            side_effect=[
                {"comments": [{"content": "c1"}], "has_more": True, "cursor": "p2"},
                RuntimeError("page2 failed"),  # 第二页抛
            ]
        )
        mock_client.close = AsyncMock()

        fake_comment = CommentHighlight(
            content="c1",
            user_name="u1",
            is_author=False,
            like_count=0,
        )
        with (
            patch("platforms.xiaohongshu.comments.AsyncXhsClient", return_value=mock_client),
            patch("platforms.xiaohongshu.comments.get_xhs_cookie", return_value="c"),
            patch("platforms.xiaohongshu.comments._parse_comment", return_value=fake_comment),
        ):
            result = await fetch_xhs_comment_highlights("n1", config=MagicMock(), max_count=10)

        # 第二页失败 → 降级返回第一页已解析的 fake_comment,不抛
        assert len(result) >= 1
        assert result[0] == fake_comment
