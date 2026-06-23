"""B站流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

# pyright: basic
import logging

from core.engine import PipelineEngine
from core.formatter import format_comment_highlights
from core.notifiers import send_to_subscription
from core.summarizer import analyze_content
from core.transcriber import cleanup_media, transcribe_file_async
from platforms.bilibili.comments import fetch_comment_highlights
from platforms.bilibili.monitor import fetch_user_videos
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NotificationContent, Phase, PhaseContext

logger = logging.getLogger("trawler.bilibili.handlers")


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
                subscription_ref=str(sub.uid),
            )


@PipelineEngine.register_detector("bili_dynamic")
async def bili_dynamic_detector(config: Config, store: MessageStore) -> None:
    """检测 B站 UP 主动态并加入 store。

    去重规则：UP 主发布视频时会自动生成一条动态。如果动态的
    ``linked_bvid`` 指向的视频已经被 ``bili_detector`` 注册（同一次检查
    里 ``bili`` detector 先于 ``bili_dynamic`` 执行），则：
      - 不再注册该动态为独立 DYNAMIC 消息（避免重复推送）
      - 如果动态有额外的文字内容（UP 主的补充说明），追加到对应视频
        消息的 ``dynamic_text`` 字段，供摘要阶段使用
    没有 linked_bvid 的纯文字/图文动态照常注册。
    """
    if not config.bilibili.monitor.watch_dynamic:
        return

    from platforms.bilibili.dynamic import fetch_new_dynamics

    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            if dyn.linked_bvid:
                # 视频型动态：检查对应视频是否已被 bili_detector 注册
                video_msg_id = f"bili:{dyn.linked_bvid}"
                if store.is_known(video_msg_id):
                    # 视频已注册，跳过动态；如有附加文字则追加到视频消息
                    if dyn.content.strip():
                        store.append_dynamic_text(video_msg_id, dyn.content.strip())
                    logger.debug(
                        "动态 %s 与已注册视频 %s 重复，跳过注册",
                        dyn.dynamic_id,
                        dyn.linked_bvid,
                    )
                    continue
                # 罕见：动态先于视频被发现（视频超出时间窗口或抓取失败）
                # 仍保留 linked_bvid 信息但不阻塞，按独立 DYNAMIC 注册
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.DYNAMIC,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )


# -- Phase: DOWNLOADED -------------------------------------------


@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool:
    """下载 B站视频音频。"""
    bvid = ctx.msg.msg_id.replace("bili:", "")
    logger.info("⬇ 下载 %s (%s)...", ctx.msg.title, bvid)

    from shared.downloader import download_video

    try:
        result = await download_video(bvid=bvid, config=ctx.config, title=ctx.msg.title)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        logger.error("✗ %s", ctx.error)
        logger.exception("Download failed for %s", bvid)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        logger.warning("⚠️  %s", ctx.error)
        return False

    ctx.downloaded_filepath = result.filepath
    logger.info("✓ 下载完成")
    return True


# -- Phase: TRANSCRIBED -----------------------------------------


@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。"""
    if ctx.msg.content_type != ContentType.VIDEO:
        return True

    filepath = ctx.downloaded_filepath
    if filepath is None or not filepath.exists():
        logger.warning("⚠️  无可用媒体文件，跳过转写")
        return True

    source_id = ctx.msg.msg_id
    logger.info("📝 转写 %s...", source_id)

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
            logger.info("✓ 转写完成")
        else:
            logger.warning("⚠️  转写未成功: %s", transcript.error)
    except ImportError:
        logger.info("⏭  转写依赖未安装，跳过")
    except Exception as exc:
        logger.error("✗ 转写失败: %s", exc)
        logger.exception("Transcribe failed for %s", source_id)

    return True


# -- Phase: SUMMARIZED ------------------------------------------


@PipelineEngine.register("*", Phase.SUMMARIZED)
async def summarize_phase(ctx: PhaseContext) -> bool:
    """生成摘要+关键词+评论亮点（跨平台共用 handler）。"""
    source_id = ctx.msg.msg_id
    logger.info("💬 获取评论亮点...")

    if ctx.msg.platform == "bili" and ctx.msg.content_type == ContentType.VIDEO:
        bvid = source_id.replace("bili:", "")
        try:
            highlights = await fetch_comment_highlights(bvid=bvid, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("Comment highlights failed for %s: %s", source_id, exc)

    elif ctx.msg.platform == "xhs":
        note_id = source_id.replace("xhs:", "")
        try:
            from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

            highlights = await fetch_xhs_comment_highlights(note_id=note_id, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("XHS comment highlights failed for %s: %s", source_id, exc)

    logger.info("🤖 生成摘要...")

    text_to_summarize = ctx.transcript_text or ctx.content_text
    # 如果消息附带动态内容（动态-视频去重场景），拼到摘要输入文本前面，
    # 让 LLM 在摘要时一并考虑 UP 主在动态里的补充说明。
    if ctx.msg.dynamic_text:
        text_to_summarize = f"【动态内容】{ctx.msg.dynamic_text}\n\n{text_to_summarize}"

    try:
        analysis = await analyze_content(
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=text_to_summarize,
            config=ctx.config,
        )
        ctx.summary_text = analysis.summary
        ctx.keywords = analysis.keywords
    except Exception as exc:
        # analyze_content 内部已吞异常并返回空结果；这里兜底防极端情况
        logger.error("✗ 摘要/关键词生成失败: %s", exc)
        logger.exception("Analysis failed for %s", source_id)

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("bili", Phase.PUSHED)
async def bili_push(ctx: PhaseContext) -> bool:
    """推送 B站通知（视频 / 动态），fan-out 到订阅声明的所有 endpoints。"""
    is_dynamic = ctx.msg.content_type == ContentType.DYNAMIC
    source_id = ctx.msg.msg_id.replace("bili_dyn:" if is_dynamic else "bili:", "")

    # 通过 subscription_ref 精确匹配订阅
    matched = None
    for sub in ctx.config.bilibili.subscriptions:
        if str(sub.uid) == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅，跳过通知", ctx.msg.subscription_ref)
        return True

    if not matched.notify_endpoints:
        logger.info("订阅 %s 未配置 endpoints，跳过通知", ctx.msg.msg_id)
        return True

    content = NotificationContent(
        platform="bili",
        source_id=source_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
        url=(f"https://t.bilibili.com/{source_id}" if is_dynamic else f"https://www.bilibili.com/video/{source_id}"),
        type="dynamic" if is_dynamic else "content",
    )

    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(
        ctx.config,
        "bili",
        matched.notify_endpoints,
        content,
    )
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))

    # 媒体清理（仅视频）
    if not is_dynamic and ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=source_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)

    return True
