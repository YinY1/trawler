"""Tests for core/formatter.py — format_comment_highlights plain text 输出。"""

from __future__ import annotations

from core.formatter import format_comment_highlights
from shared.protocols import CommentHighlight


def test_empty_returns_empty_string():
    assert format_comment_highlights([]) == ""


def test_single_highlight_plain_format():
    h = CommentHighlight(content="好视频", user_name="张三", is_author=False, like_count=5)
    out = format_comment_highlights([h])
    assert "张三" in out
    assert "好视频" in out
    assert "5" in out
    # plain text：无 markdown
    assert "**" not in out
    assert "\n  " not in out  # 不再使用续行缩进
    assert "- " not in out  # 不使用 markdown 列表


def test_author_and_pinned_tags():
    h = CommentHighlight(
        content="置顶评论",
        user_name="UP",
        is_author=True,
        is_pinned=True,
        like_count=10,
    )
    out = format_comment_highlights([h])
    assert "作者" in out
    assert "置顶" in out


def test_reply_chain_compact():
    h = CommentHighlight(
        content="回复内容",
        user_name="B",
        is_author=False,
        like_count=1,
        reply_to="A",
        parent_content="原话",
    )
    out = format_comment_highlights([h])
    assert "A" in out
    assert "原话" in out
    assert "B" in out
    assert "回复内容" in out
