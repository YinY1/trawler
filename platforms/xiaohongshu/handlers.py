"""小红书流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

# pyright: basic
import logging

from rich.console import Console

from core.engine import PipelineEngine
from core.notifier import notify_new_xhs_note
from core.transcriber import cleanup_media
from platforms.xiaohongshu.downloader import download_note
from platforms.xiaohongshu.monitor import fetch_user_notes
from platforms.xiaohongshu.parser import parse_note_content
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NoteInfo, Phase, PhaseContext

logger = logging.getLogger(__name__)
console = Console()


# -- Detector ----------------------------------------------------


@PipelineEngine.register_detector("xhs")
async def xhs_detector(config: Config, store: MessageStore) -> None:
    """检测新笔记并加入 store。"""
    for sub in config.xiaohongshu.subscriptions:
        notes = await fetch_user_notes(
            user_id=sub.user_id,
            name=sub.name,
            config=config,
        )
        for n in notes:
            store.add_new(
                msg_id=f"xhs:{n.note_id}",
                platform="xhs",
                content_type=ContentType.VIDEO if n.note_type == "video" else ContentType.TEXT,
                pubdate=n.pubdate,
                title=n.title,
                author=n.author,
            )


# -- Phase: DOWNLOADED -------------------------------------------


@PipelineEngine.register("xhs", Phase.DOWNLOADED)
async def xhs_download(ctx: PhaseContext) -> bool:
    """下载小红书笔记（图片或视频）。"""
    note_id = ctx.msg.msg_id.replace("xhs:", "")
    console.print(f"  [dim]⬇ 下载 {ctx.msg.title} ({note_id})...[/]")

    # Reconstruct NoteInfo from MessageRecord
    note = NoteInfo(
        note_id=note_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        user_id="",
        note_type="video" if ctx.msg.content_type == ContentType.VIDEO else "normal",
        pubdate=ctx.msg.pubdate,
    )

    try:
        result = await download_note(note=note, config=ctx.config)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        console.print(f"  [red]✗ {ctx.error}[/]")
        logger.exception("XHS download failed for %s", note_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        console.print(f"  [yellow]⚠️  {ctx.error}[/]")
        return False

    ctx.downloaded_filepath = result.filepath
    ctx.image_paths = result.image_paths
    ctx.content_text = result.content_text
    console.print("  [green]✓ 下载完成[/]")

    # Parse content for text
    try:
        parsed = parse_note_content(note=note, download_result=result)
        if parsed:
            ctx.content_text = parsed.text
    except Exception as exc:
        console.print(f"  [yellow]⚠️  内容解析失败: {exc}[/]")
        logger.warning("XHS parse failed for %s: %s", note_id, exc)

    # Fetch comment highlights for TEXT (图文) notes — skip SUMMARIZED phase
    if ctx.msg.content_type == ContentType.TEXT:
        try:
            from core.formatter import format_comment_highlights
            from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

            highlights = await fetch_xhs_comment_highlights(note_id=note_id, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
            if highlights:
                console.print(f"  [dim]💬 获取到 {len(highlights)} 条热门评论[/]")
        except Exception as exc:
            console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
            logger.warning("XHS comment highlights failed for %s: %s", note_id, exc)

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("xhs", Phase.PUSHED)
async def xhs_push(ctx: PhaseContext) -> bool:
    """推送小红书笔记通知。"""
    note_id = ctx.msg.msg_id.replace("xhs:", "")
    console.print("  [dim]🔔 推送通知...[/]")

    try:
        await notify_new_xhs_note(
            note_id=note_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
            xhs_noti_config=ctx.config.xiaohongshu.notification,
        )
        console.print("  [green]✓ 通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("XHS notify failed for %s: %s", note_id, exc)

    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=note_id)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("XHS cleanup failed for %s: %s", note_id, exc)

    return True
