"""小红书流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

# pyright: basic
import logging

from core.engine import PipelineEngine
from core.notifiers import send_to_subscription
from core.transcriber import cleanup_media
from platforms.xiaohongshu.downloader import download_note
from platforms.xiaohongshu.monitor import fetch_user_notes
from platforms.xiaohongshu.parser import parse_note_content
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NoteInfo, NotificationContent, Phase, PhaseContext

logger = logging.getLogger(__name__)


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
                subscription_ref=sub.user_id,
            )


# -- Phase: DOWNLOADED -------------------------------------------


@PipelineEngine.register("xhs", Phase.DOWNLOADED)
async def xhs_download(ctx: PhaseContext) -> bool:
    """下载小红书笔记（图片或视频）。"""
    note_id = ctx.msg.msg_id.replace("xhs:", "")
    logger.info("⬇ 下载 %s (%s)...", ctx.msg.title, note_id)

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
        logger.error("✗ %s", ctx.error)
        logger.exception("XHS download failed for %s", note_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        logger.warning("⚠️  %s", ctx.error)
        return False

    ctx.downloaded_filepath = result.filepath
    ctx.image_paths = result.image_paths
    ctx.content_text = result.content_text
    logger.info("✓ 下载完成")

    # Parse content for text
    try:
        parsed = parse_note_content(note=note, download_result=result)
        if parsed:
            ctx.content_text = parsed.text
    except Exception as exc:
        logger.warning("⚠️  内容解析失败: %s", exc)
        logger.warning("XHS parse failed for %s: %s", note_id, exc)

    # Fetch comment highlights for TEXT (图文) notes — skip SUMMARIZED phase
    if ctx.msg.content_type == ContentType.TEXT:
        try:
            from core.formatter import format_comment_highlights
            from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

            highlights = await fetch_xhs_comment_highlights(note_id=note_id, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
            if highlights:
                logger.info("💬 获取到 %d 条热门评论", len(highlights))
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("XHS comment highlights failed for %s: %s", note_id, exc)

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("xhs", Phase.PUSHED)
async def xhs_push(ctx: PhaseContext) -> bool:
    """推送小红书笔记通知。"""
    # 手动重跑模式（plan 2026-06-28 D4/D7）：skip_push=True 时跳过 send_to_subscription，
    # 但 phase 仍推进到 PUSHED。提前 return 同时跳过 media cleanup（有意为之，
    # 保留本地文件以便后续重跑；xhs 视频笔记需重新下载成本高）。
    if ctx.skip_push:
        logger.info("⏭ 跳过推送（skip_push=True）: %s", ctx.msg.msg_id)
        return True

    note_id = ctx.msg.msg_id.replace("xhs:", "")

    matched = None
    for sub in ctx.config.xiaohongshu.subscriptions:
        if sub.user_id == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅", ctx.msg.subscription_ref)
        return True
    if not matched.notify_endpoints:
        logger.info("订阅未配置 endpoints")
        return True

    content = NotificationContent(
        platform="xhs",
        source_id=note_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
    )
    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(ctx.config, "xhs", matched.notify_endpoints, content)
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))

    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=note_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)
    return True
