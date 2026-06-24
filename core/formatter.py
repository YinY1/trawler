"""跨平台评论格式化 — 将统一 CommentHighlight 列表格式化为纯文本 (plain text)

为何独立文件而非放在 handlers 或 protocols 中：
- `shared/protocols.py` 是纯数据模型层，不应包含展示逻辑
- 各平台 `handlers.py` 之前各自维护了相同的格式化代码，
  抽取到 `core/formatter.py` 实现一次编写、三平台（B站/微博/小红书）共用
"""

from __future__ import annotations

from shared.protocols import CommentHighlight


def format_comment_highlights(highlights: list[CommentHighlight]) -> str:
    """将评论亮点列表格式化为纯文本（plain text）。

    输出无 markdown 语法，每条评论单行，便于推送端原样显示。

    兼容统一 CommentHighlight 模型的字段：
    - user_name, content, like_count
    - is_author → "作者" 标签
    - is_pinned → "置顶" 标签
    - reply_to + parent_content → 对话链路

    Args:
        highlights: CommentHighlight 列表

    Returns:
        纯文本字符串（无 markdown 标记）
    """
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_author", False)
        is_pinned = getattr(h, "is_pinned", False)
        reply_to = getattr(h, "reply_to", "")
        parent_content = getattr(h, "parent_content", "")

        tags: list[str] = []
        if is_author:
            tags.append("作者")
        if is_pinned:
            tags.append("置顶")
        tag = f" ({', '.join(tags)})" if tags else ""

        if reply_to and parent_content:
            parts.append(f"  ↳ {reply_to}: {parent_content}\n{name}{tag} (👍{like}): {content}")
        else:
            parts.append(f"• {name}{tag} (👍{like}): {content}")
    return "\n".join(parts)
