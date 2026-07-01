"""通知内容渲染层 — 跨 Provider 共享的纯文本 (plain text) 渲染。"""

from __future__ import annotations

from shared.constants import GIT_SHA
from shared.protocols import NotificationContent

# 通知正文最大长度（字符数）。TEXT 类型 summary 承载原文，可能数千字，
# 超长会导致 Telegram 4096 上限 400 错误或刷屏。1000 字符兼顾可读性与通道限制。
MAX_NOTIFICATION_SUMMARY_LENGTH = 1000


def _truncate_summary(text: str, max_len: int = MAX_NOTIFICATION_SUMMARY_LENGTH) -> str:
    """截断超长通知正文，加 ... 后缀。短文本不受影响。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# 各 platform 的 title emoji 和"作者"标签
_PLATFORM_STYLE: dict[str, dict[str, str]] = {
    "bili": {"emoji": "📹", "author_label": "UP主"},
    "xhs": {"emoji": "📕", "author_label": "作者"},
    "weibo": {"emoji": "🐦", "author_label": "作者"},
}


def _build_url(content: NotificationContent) -> str:
    if content.url:
        return content.url
    if content.platform == "bili":
        return f"https://www.bilibili.com/video/{content.source_id}"
    if content.platform == "xhs":
        return f"https://www.xiaohongshu.com/explore/{content.source_id}"
    if content.platform == "weibo":
        return f"https://weibo.com/{content.source_id}"
    return ""


def render_markdown(content: NotificationContent) -> tuple[str, str]:
    """渲染通知为 (title, message_text)。

    输出为纯文本 (plain text)，不含任何 markdown 标记；函数名 render_markdown
    保留作为兼容外部接口（改名会破坏 import）。

    根据 content.platform 选择 emoji 和"作者"标签；
    根据 content.type == "dynamic" 使用更简短的动态模板。
    """
    style = _PLATFORM_STYLE.get(content.platform, {"emoji": "📣", "author_label": "作者"})
    keywords_str = "；".join(content.keywords) if content.keywords else "无"
    url = _build_url(content)

    # 健康告警（issue #55）：简化模板 + 版本 footer
    # 决策 5 限定：仅 health_alert 分支追加版本号，content/dynamic 不动
    if content.type == "health_alert":
        parts = [_truncate_summary(content.summary or content.title), "", f"(trawler@{GIT_SHA[:7]})"]
        return content.title, "\n".join(parts)

    if content.type == "dynamic":
        # 动态：简短格式，无 keywords/comment
        parts: list[str] = [f"{style['author_label']}: {content.author}"]
        if url:
            parts.append(f"链接: {content.source_id} {url}")
        parts.extend(["", _truncate_summary(content.summary or content.title)])
        return f"📢 {content.author} 的动态", "\n".join(parts)

    # 默认：完整内容模板
    parts = [
        f"{style['author_label']}: {content.author}",
        f"链接: {content.source_id} {url}" if url else "",
        f"关键词: {keywords_str}",
        "",
        "详情:",
        _truncate_summary(content.summary),
    ]
    if content.comment_highlights:
        parts.extend(["", "评论区补充:", content.comment_highlights])
    return f"{style['emoji']} {content.title}", "\n".join(parts)
