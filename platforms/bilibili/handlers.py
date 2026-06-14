"""B站流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

import logging

from rich.console import Console

from core.engine import PipelineEngine
from core.notifier import notify_new_video
from core.summarizer import extract_keywords, generate_summary
from core.transcriber import cleanup_media, transcribe_file_async
from platforms.bilibili.comments import fetch_comment_highlights
from platforms.bilibili.monitor import fetch_user_videos
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext

logger = logging.getLogger(__name__)
console = Console()


# -- Detector ----------------------------------------------------


@PipelineEngine.register_detector("bili")
async def bili_detector(config: Config, store: MessageStore) -> None:
    """检测新注册的 UP 主视频并加入 store。"""
    from shared.config import BiliSubscription

    for sub in config.bilibili.subscriptions:
        assert isinstance(sub, BiliSubscription)
        videos = await fetch_user_videos(
            uid=sub.uid,
            config=config,
            max_count=config.bilibili.monitor.max_videos_per_check,
        )
        for v in videos:
            store.add_new(
                msg_id=f"bili:{v.bvid}",
                platform="bili",
                content_type=ContentType.VIDEO,
                pubdate=v.pubdate,
                title=v.title,
                author=v.author,
            )


@PipelineEngine.register_detector("bili_dynamic")
async def bili_dynamic_detector(config: Config, store: MessageStore) -> None:
    """检测 B站 UP 主动态并加入 store。"""
    if not config.bilibili.monitor.watch_dynamic:
        return

    from platforms.bilibili.dynamic import fetch_new_dynamics

    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.DYNAMIC,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
            )


# -- Phase: DOWNLOADED -------------------------------------------


def _format_comment_highlights(highlights: list) -> str:
    """将评论亮点列表格式化为 Markdown 文本。"""
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_up_owner", False) or getattr(h, "is_author", False)
        is_pinned = getattr(h, "is_pinned", False)
        reply_to = getattr(h, "reply_to", "")
        parent_content = getattr(h, "parent_content", "")

        tags: list[str] = []
        if is_author:
            tags.append("UP主")
        if is_pinned:
            tags.append("置顶")
        tag = f" ({', '.join(tags)})" if tags else ""

        if reply_to and parent_content:
            parts.append(
                f"- **{reply_to}**:\n"
                f"  > {parent_content}\n"
                f"  **{name}**{tag} (👍{like}):\n"
                f"  {content}"
            )
        else:
            parts.append(f"- **{name}**{tag} (👍{like}):\n  {content}")
    return "\n".join(parts)


@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool:
    """下载 B站视频音频。"""
    bvid = ctx.msg.msg_id.replace("bili:", "")
    console.print(f"  [dim]⬇ 下载 {ctx.msg.title} ({bvid})...[/]")

    from shared.downloader import download_video

    try:
        result = await download_video(bvid=bvid, config=ctx.config, title=ctx.msg.title)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        console.print(f"  [red]✗ {ctx.error}[/]")
        logger.exception("Download failed for %s", bvid)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        console.print(f"  [yellow]⚠️  {ctx.error}[/]")
        return False

    ctx.downloaded_filepath = result.filepath
    console.print("  [green]✓ 下载完成[/]")
    return True


# -- Phase: TRANSCRIBED -----------------------------------------


@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。"""
    if ctx.msg.content_type != ContentType.VIDEO:
        return True

    filepath = ctx.downloaded_filepath
    if filepath is None or not filepath.exists():
        console.print("  [yellow]⚠️  无可用媒体文件，跳过转写[/]")
        return True

    source_id = ctx.msg.msg_id
    console.print(f"  [dim]📝 转写 {source_id}...[/]")

    try:
        transcript = await transcribe_file_async(
            filepath=filepath,
            config=ctx.config,
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
        )
        if transcript.success:
            ctx.transcript_text = transcript.text
            console.print("  [green]✓ 转写完成[/]")
        else:
            console.print(f"  [yellow]⚠️  转写未成功: {transcript.error}[/]")
    except ImportError:
        console.print("  [dim]⏭  转写依赖未安装，跳过[/]")
    except Exception as exc:
        console.print(f"  [red]✗ 转写失败: {exc}[/]")
        logger.exception("Transcribe failed for %s", source_id)

    return True


# -- Phase: SUMMARIZED ------------------------------------------


@PipelineEngine.register("*", Phase.SUMMARIZED)
async def summarize_phase(ctx: PhaseContext) -> bool:
    """生成摘要+关键词+评论亮点（跨平台共用 handler）。"""
    source_id = ctx.msg.msg_id
    console.print("  [dim]💬 获取评论亮点...[/]")

    if ctx.msg.platform == "bili" and ctx.msg.content_type == ContentType.VIDEO:
        bvid = source_id.replace("bili:", "")
        try:
            highlights = await fetch_comment_highlights(bvid=bvid, config=ctx.config)
            ctx.comment_highlights = _format_comment_highlights(highlights)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
            logger.warning("Comment highlights failed for %s: %s", source_id, exc)

    console.print("  [dim]🤖 生成摘要...[/]")

    text_to_summarize = ctx.transcript_text or ctx.content_text
    try:
        summary_text, _source, _is_ai = generate_summary(
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=text_to_summarize,
            config=ctx.config,
        )
        ctx.summary_text = summary_text
    except Exception as exc:
        console.print(f"  [red]✗ 摘要生成失败: {exc}[/]")
        logger.exception("Summary failed for %s", source_id)

    try:
        ctx.keywords = extract_keywords(
            text=ctx.summary_text,
            title=ctx.msg.title,
            author=ctx.msg.author,
            config=ctx.config,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/]")
        logger.warning("Keywords failed for %s: %s", source_id, exc)

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("bili", Phase.PUSHED)
async def bili_push(ctx: PhaseContext) -> bool:
    """推送 B站视频通知。"""
    bvid = ctx.msg.msg_id.replace("bili:", "")
    console.print("  [dim]🔔 推送通知...[/]")

    try:
        await notify_new_video(
            bvid=bvid,
            title=ctx.msg.title,
            author=ctx.msg.author,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
            config=ctx.config.bilibili.notification,
        )
        console.print("  [green]✓ 通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("Notify failed for %s: %s", bvid, exc)

    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=bvid)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("Cleanup failed for %s: %s", bvid, exc)

    return True


@PipelineEngine.register("bili_dynamic", Phase.PUSHED)
async def bili_dynamic_push(ctx: PhaseContext) -> bool:
    """推送 B站动态通知。"""
    from core.notifier import notify_dynamic

    console.print("  [dim]🔔 推送动态通知...[/]")

    try:
        await notify_dynamic(
            dynamic_info={
                "user": ctx.msg.author,
                "content": ctx.summary_text or ctx.msg.title,
                "dynamic_id": ctx.msg.msg_id.replace("bili_dyn:", ""),
                "type": "动态",
                "url": f"https://t.bilibili.com/{ctx.msg.msg_id.replace('bili_dyn:', '')}",
            },
            config=ctx.config.bilibili.notification,
        )
        console.print("  [green]✓ 动态通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  动态通知推送失败: {exc}[/]")
        logger.warning("Dynamic notify failed for %s: %s", ctx.msg.msg_id, exc)

    return True
