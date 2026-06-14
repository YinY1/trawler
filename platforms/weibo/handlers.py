"""微博流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

# pyright: basic
import logging

from rich.console import Console

from core.engine import PipelineEngine
from core.formatter import format_comment_highlights
from core.notifier import notify_new_weibo_post
from core.summarizer import extract_keywords, generate_summary
from platforms.weibo.api import fetch_user_posts
from platforms.weibo.comments import fetch_weibo_comment_highlights
from platforms.weibo.downloader import download_weibo_media
from platforms.weibo.parser import parse_weibo_post
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext

logger = logging.getLogger(__name__)
console = Console()


# -- Detector ----------------------------------------------------


@PipelineEngine.register_detector("weibo")
async def weibo_detector(config: Config, store: MessageStore) -> None:
    """检测新微博帖子并加入 store。"""
    for sub in config.weibo.subscriptions:
        posts = await fetch_user_posts(
            cookie=config.weibo.auth.cookie,
            user_id=sub.user_id,
            max_posts=10,
        )
        for p in posts:
            store.add_new(
                msg_id=f"weibo:{p.post_id}",
                platform="weibo",
                content_type=ContentType.TEXT,
                pubdate=p.pubdate,
                title=p.clean_text[:50] if p.clean_text else p.post_id,
                author=p.author,
            )


# -- Phase: DOWNLOADED -------------------------------------------


@PipelineEngine.register("weibo", Phase.DOWNLOADED)
async def weibo_download(ctx: PhaseContext) -> bool:
    """下载微博媒体、解析内容、生成摘要和关键词。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")
    console.print(f"  [dim]⬇ 下载 {ctx.msg.title} ({post_id})...[/]")

    # Reconstruct WeiboPost from MessageRecord
    from shared.protocols import WeiboPost

    post = WeiboPost(
        post_id=post_id,
        text="",
        clean_text=ctx.msg.title,
        author=ctx.msg.author,
        user_id="",
        pubdate=ctx.msg.pubdate,
    )

    # 尝试获取完整长文（如果标题被截断）
    cookie = ctx.config.weibo.auth.cookie
    if cookie:
        from platforms.weibo.api import _fetch_long_text

        full_text = await _fetch_long_text(cookie, post_id)
        if full_text:
            post.clean_text = full_text

    try:
        result = await download_weibo_media(post=post, config=ctx.config)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        console.print(f"  [red]✗ {ctx.error}[/]")
        logger.exception("Weibo download failed for %s", post_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        console.print(f"  [yellow]⚠️  {ctx.error}[/]")
        return False

    ctx.image_paths = result.image_paths
    ctx.content_text = result.text
    console.print("  [green]✓ 下载完成[/]")

    # Parse content
    try:
        parsed = parse_weibo_post(post=post, download_result=result)
        if parsed:
            ctx.content_text = parsed.get("text", ctx.content_text)
    except Exception as exc:
        console.print(f"  [yellow]⚠️  内容解析失败: {exc}[/]")
        logger.warning("Weibo parse failed for %s: %s", post_id, exc)

    # Generate summary and keywords (TEXT type skips SUMMARIZED phase)
    try:
        summary_text, source, _ = generate_summary(
            source_id=post_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=ctx.content_text,
            config=ctx.config,
        )
        ctx.summary_text = summary_text
        console.print(f"  [dim]📝 摘要 ({source})[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  摘要生成失败: {exc}[/]")
        ctx.summary_text = ctx.content_text[:500]

    try:
        ctx.keywords = extract_keywords(
            text=ctx.content_text,
            title=ctx.msg.title,
            author=ctx.msg.author,
            config=ctx.config,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/]")
        ctx.keywords = []

    # Fetch comment highlights
    try:
        highlights = await fetch_weibo_comment_highlights(
            post_id=post_id,
            config=ctx.config,
        )
        ctx.comment_highlights = format_comment_highlights(highlights)
        if highlights:
            console.print(f"  [dim]💬 获取到 {len(highlights)} 条热门评论[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
        ctx.comment_highlights = ""

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("weibo", Phase.PUSHED)
async def weibo_push(ctx: PhaseContext) -> bool:
    """推送微博通知。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")
    display_title = ctx.msg.title
    console.print("  [dim]🔔 推送通知...[/]")

    try:
        await notify_new_weibo_post(
            post_id=post_id,
            title=display_title,
            author=ctx.msg.author,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
            weibo_noti_config=ctx.config.weibo.notification,
        )
        console.print("  [green]✓ 通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("Weibo notify failed for %s: %s", post_id, exc)

    return True
