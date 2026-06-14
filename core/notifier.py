"""Gotify 推送通知模块 - 支持多平台内容推送"""

from __future__ import annotations

import asyncio
from datetime import datetime

import aiohttp
from rich.console import Console

from shared.config import NotificationConfig
from shared.constants import GOTIFY_MAX_RETRIES, GOTIFY_TIMEOUT

console = Console()


# ── 底层 Gotify 接口 ─────────────────────────────────────────────


async def send_gotify(
    title: str,
    message: str,
    config: NotificationConfig,
    priority: int | None = None,
) -> bool:
    """通过 Gotify 发送推送通知

    支持最多 3 次重试，采用指数退避策略。每次失败后等待时间
    依次为 1s、2s、4s。

    Args:
        title: 通知标题
        message: 通知正文（支持 Markdown 格式）
        config: 通知推送配置（含 gotify_url 和 gotify_token）
        priority: 消息优先级（None 则使用配置中的默认值）

    Returns:
        是否发送成功
    """
    if not config.enabled:
        console.log("[dim]通知推送已禁用[/]")
        return False

    if not config.gotify_url or not config.gotify_token:
        console.log("[yellow]Gotify 配置不完整（缺少 URL 或 Token）[/]")
        return False

    url = f"{config.gotify_url.rstrip('/')}/message"
    params = {"token": config.gotify_token}
    payload: dict[str, str | int] = {
        "title": title,
        "message": message,
        "priority": priority if priority is not None else config.priority,
    }

    max_retries = GOTIFY_MAX_RETRIES
    for attempt in range(1, max_retries + 1):
        try:
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.post(
                    url,
                    params=params,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=GOTIFY_TIMEOUT),
                ) as resp:
                    resp.raise_for_status()
                console.log(f"[green]Gotify 通知发送成功: {title}[/]")
                return True

        except asyncio.TimeoutError:
            console.log(f"[yellow]Gotify 请求超时 (尝试 {attempt}/{max_retries})[/]")
        except aiohttp.ClientConnectionError:
            console.log(f"[yellow]Gotify 连接失败 (尝试 {attempt}/{max_retries})[/]")
        except aiohttp.ClientResponseError as e:
            console.log(f"[yellow]Gotify HTTP 错误 (尝试 {attempt}/{max_retries}): {e}[/]")
        except Exception as e:
            console.log(f"[yellow]Gotify 发送异常 (尝试 {attempt}/{max_retries}): {e}[/]")

        # 指数退避：1s, 2s, 4s
        if attempt < max_retries:
            wait = 2 ** (attempt - 1)
            console.log(f"[dim]等待 {wait}s 后重试...[/]")
            await asyncio.sleep(wait)

    console.log(f"[bold red]Gotify 通知发送失败（已重试 {max_retries} 次）: {title}[/]")
    return False


# ── B 站视频通知 ─────────────────────────────────────────────────


async def notify_new_video(
    bvid: str,
    title: str,
    author: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
    config: NotificationConfig | None = None,
    *,
    # 允许直接传入配置参数作为备选
    gotify_url: str = "",
    gotify_token: str = "",
) -> bool:
    """发送 B 站新视频通知

    构造 Markdown 格式的通知消息，包含视频信息和 AI 摘要。

    Args:
        bvid: 视频BV号
        title: 视频标题
        author: UP主名称
        summary: AI 摘要文本
        keywords: 关键词列表
        comment_highlights: 评论区精选内容（可选）
        config: 通知配置对象
        gotify_url: Gotify URL（备选参数）
        gotify_token: Gotify Token（备选参数）

    Returns:
        是否发送成功
    """
    if config is None:
        config = NotificationConfig(
            enabled=True,
            gotify_url=gotify_url,
            gotify_token=gotify_token,
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    keywords_str = "；".join(keywords) if keywords else "无"
    video_url = f"https://www.bilibili.com/video/{bvid}"

    # 构造 Markdown 消息
    parts: list[str] = [
        f"**UP主:** {author}",
        f"**链接:** [{bvid}]({video_url})",
        f"**发布时间:** {now}",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend(
            [
                "",
                "**评论区补充:**",
                comment_highlights,
            ]
        )

    message = "\n".join(parts)
    return await send_gotify(
        title=f"📹 {title}",
        message=message,
        config=config,
    )


# ── 小红书笔记通知 ───────────────────────────────────────────────


async def notify_new_xhs_note(
    note_id: str,
    title: str,
    author: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
    xhs_noti_config: NotificationConfig | None = None,
    *,
    gotify_url: str = "",
    gotify_token: str = "",
) -> bool:
    """发送小红书新笔记通知

    构造 Markdown 格式的通知消息，包含笔记信息和 AI 摘要。

    Args:
        note_id: 小红书笔记 ID
        title: 笔记标题
        author: 作者名称
        summary: AI 摘要文本
        keywords: 关键词列表
        comment_highlights: 评论区精选内容（可选）
        xhs_noti_config: 小红书通知配置
        gotify_url: Gotify URL（备选参数）
        gotify_token: Gotify Token（备选参数）

    Returns:
        是否发送成功
    """
    if xhs_noti_config is None:
        xhs_noti_config = NotificationConfig(
            enabled=True,
            gotify_url=gotify_url,
            gotify_token=gotify_token,
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    keywords_str = "；".join(keywords) if keywords else "无"
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"

    parts: list[str] = [
        f"**作者:** {author}",
        f"**链接:** [{note_id}]({note_url})",
        f"**发布时间:** {now}",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend(
            [
                "",
                "**评论区补充:**",
                comment_highlights,
            ]
        )

    message = "\n".join(parts)
    return await send_gotify(
        title=f"📕 {title}",
        message=message,
        config=xhs_noti_config,
    )


# ── 动态通知 ─────────────────────────────────────────────────────


async def notify_dynamic(
    dynamic_info: dict[str, str],
    config: NotificationConfig,
) -> bool:
    """发送动态通知（较简短格式）

    用于推送 UP 主动态信息，消息格式比视频通知更简短。

    Args:
        dynamic_info: 动态信息字典，包含以下字段：
            - user: 用户名
            - content: 动态内容
            - dynamic_id: 动态 ID（可选）
            - type: 动态类型（可选，如 "转发"、"投稿" 等）
            - url: 动态链接（可选）
        config: 通知推送配置

    Returns:
        是否发送成功
    """
    user = dynamic_info.get("user", "未知用户")
    content = dynamic_info.get("content", "")
    dynamic_id = dynamic_info.get("dynamic_id", "")
    dynamic_type = dynamic_info.get("type", "动态")
    url = dynamic_info.get("url", "")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    parts: list[str] = [
        f"**用户:** {user}",
        f"**时间:** {now}",
    ]

    if url:
        link_text = dynamic_id if dynamic_id else "查看详情"
        parts.append(f"**链接:** [{link_text}]({url})")

    parts.extend(
        [
            "",
            "---",
            "",
            str(content),
        ]
    )

    message = "\n".join(parts)
    title = f"📢 {user} 的{dynamic_type}"
    if dynamic_type != "动态":
        title = f"📢 {user} - {dynamic_type}"

    return await send_gotify(
        title=title,
        message=message,
        config=config,
    )


# ═══════════════════════════════════════════════════════════


async def notify_new_weibo_post(
    post_id: str,
    title: str,
    author: str,
    summary: str,
    keywords: list[str],
    comment_highlights: str | None = None,
    weibo_noti_config: NotificationConfig | None = None,
    *,
    gotify_url: str = "",
    gotify_token: str = "",
) -> bool:
    """发送微博新帖子通知。

    构造 Markdown 格式的通知消息，包含帖子和 AI 摘要。

    Args:
        post_id: 微博帖子 ID
        title: 帖子标题/摘要
        author: 作者名称
        summary: AI 摘要文本
        keywords: 关键词列表
        comment_highlights: 评论区精选内容（可选）
        weibo_noti_config: 微博通知配置
        gotify_url: Gotify URL（备选参数）
        gotify_token: Gotify Token（备选参数）

    Returns:
        是否发送成功
    """
    if weibo_noti_config is None:
        weibo_noti_config = NotificationConfig(
            enabled=True,
            gotify_url=gotify_url,
            gotify_token=gotify_token,
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    keywords_str = "；".join(keywords) if keywords else "无"
    post_url = f"https://weibo.com/{post_id}"

    parts: list[str] = [
        f"**作者:** {author}",
        f"**链接:** [{post_id}]({post_url})",
        f"**发布时间:** {now}",
        f"**关键词:** {keywords_str}",
        "",
        "---",
        "",
        "**详情:**",
        summary,
    ]

    if comment_highlights:
        parts.extend(
            [
                "",
                "**评论区补充:**",
                comment_highlights,
            ]
        )

    message = "\n".join(parts)
    return await send_gotify(
        title=f"🐦 {title}",
        message=message,
        config=weibo_noti_config,
    )
