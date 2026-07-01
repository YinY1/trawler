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
            if dyn.has_video:
                # 视频型动态(spec §2 case 1/2):
                # - case 1: linked_bvid 对应视频已被 bili_detector 注册 → 追加 dynamic_text
                # - case 2: 视频未注册 → 以 bili:{bvid} 注册为 VIDEO,动态正文作 dynamic_text
                video_msg_id = f"bili:{dyn.linked_bvid}"
                content_text = dyn.content.strip()
                if store.is_known(video_msg_id):
                    # case 1: 视频已注册,追加附加文字(若有)
                    if content_text:
                        store.append_dynamic_text(video_msg_id, content_text)
                    logger.debug(
                        "视频型动态 %s 与已注册视频 %s 重复,追加 dynamic_text",
                        dyn.dynamic_id,
                        dyn.linked_bvid,
                    )
                    continue

                # case 2: 视频未注册,以 bili:{bvid} 注册为 VIDEO
                # (spec §2 提到的「反查 bvid」在当前 _parse_dynamic 实现下不需要——
                #  linked_bvid 已从动态 API 的 major.archive.bvid 直接拿到)
                new_msg = store.add_new(
                    msg_id=video_msg_id,
                    platform="bili",
                    content_type=ContentType.VIDEO,
                    pubdate=dyn.pubdate,
                    title=dyn.title or f"bili:{dyn.linked_bvid}",
                    author=dyn.author,
                    subscription_ref=str(sub.uid),
                )
                if new_msg is not None and content_text:
                    # plan D7: 动态正文作为 dynamic_text 附加到 VIDEO 消息
                    store.append_dynamic_text(video_msg_id, content_text)
                continue

            # case 3: 纯文字 / 图文动态 → 注册为 TEXT
            new_msg = store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.TEXT,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )
            if new_msg is not None and dyn.content.strip():
                # plan D3: detector 同步把动态正文写入 body,供 push 阶段渲染全文
                store.mark_body(f"bili_dyn:{dyn.dynamic_id}", dyn.content.strip())


# -- Phase: DOWNLOADED -------------------------------------------


@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool:
    """下载 B站视频音频。

    纯文字动态(bili_dyn: 前缀, plan D3)无媒体可下载,no-op 推进。
    detector 已通过 store.mark_body 写入的正文复制到 ctx.content_text,
    让 push 阶段能拿到动态正文。
    """
    if ctx.msg.msg_id.startswith("bili_dyn:"):
        ctx.content_text = ctx.msg.body
        return True

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
        # downloader 层标记的永久失败（凭证缺失/BVID 不存在等）→ engine 直接 mark_error
        if result.permanent:
            ctx.permanent_error = True
        logger.warning("⚠️  %s", ctx.error)
        return False

    ctx.downloaded_filepath = result.filepath
    logger.info("✓ 下载完成")
    return True


# -- Phase: TRANSCRIBED -----------------------------------------


@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。

    仅 VIDEO 类型消息会到达此阶段(PHASE_FLOW 保证:TEXT flow 不含 TRANSCRIBED),
    所以不需要 content_type 特判(spec §5 / issue #46 重构)。

    Bug 3 fix:
    - ``filepath`` 缺失时不再静默 return True，而是 ``ctx.error='downloaded_filepath missing'``
      并 return False，让消息停留在当前阶段并暴露在 dashboard 上，避免
      空 transcript 推送低质量通知。``process_message`` 的 rewind 网关通常
      会先一步重新下载，这里只是兜底。
    - ``transcribe_file_async`` 真异常时记 WARNING 并降级用 ``content_text``
      继续流程（return True），保持既有的优雅降级语义。
    """
    filepath = ctx.downloaded_filepath
    if filepath is None or not filepath.exists():
        ctx.error = "downloaded_filepath missing"
        # 永久失败：filepath 缺失重试也不会变（Bug 3 兜底；正常路径 engine rewind 已先一步重试）。
        # 标记 permanent_error 让 engine 直接 mark_error 跳过 retry，避免 5 次无意义刷日志。
        ctx.permanent_error = True
        logger.warning("⚠️  %s — 转写阶段无可用媒体文件", ctx.error)
        return False

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
            logger.warning("⚠️  转写未成功: %s — 降级用 content_text 继续流程", transcript.error)
    except ImportError:
        logger.info("⏭  转写依赖未安装，跳过（降级用 content_text）")
    except Exception as exc:
        # 真异常：记 warning，不阻塞流程（return True），下游用 content_text
        logger.warning("⚠️  转写失败: %s — 降级用 content_text 继续流程", exc)
        logger.warning("Transcribe failed for %s: %s", source_id, exc)

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

    elif ctx.msg.platform == "weibo":
        # spec §5 / issue #46 PR-2: weibo VIDEO 抓评论(TEXT 不走 SUMMARIZED 阶段)
        post_id = source_id.replace("weibo:", "")
        try:
            from platforms.weibo.comments import fetch_weibo_comment_highlights

            highlights = await fetch_weibo_comment_highlights(post_id=post_id, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("Weibo comment highlights failed for %s: %s", source_id, exc)

    logger.info("🤖 生成摘要...")

    text_to_summarize = ctx.transcript_text or ctx.content_text
    # 如果消息附带动态内容（动态-视频去重场景），拼到摘要输入文本前面，
    # 让 LLM 在摘要时一并考虑 UP 主在动态里的补充说明。
    if ctx.msg.dynamic_text:
        text_to_summarize = f"【动态内容】{ctx.msg.dynamic_text}\n\n{text_to_summarize}"

    # analyze_content 内部已吞所有异常并返回 failed=True；
    # 走到这里 analysis 一定有效（failed 或成功），无需外层 try/except 兜底。
    analysis = await analyze_content(
        source_id=source_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        text=text_to_summarize,
        config=ctx.config,
    )
    if analysis.failed:
        # fallback 链全部失败：标记 ctx.error 让 engine 处理 retry
        # （engine 会读 retry_count 决定是 mark_retry_failure 还是 mark_error）
        ctx.error = "AI 摘要失败：所有 provider 不可用"
        logger.warning("⚠️  %s — 消息将卡在 SUMMARIZED 阶段等待重试", ctx.error)
        return False
    # Issue #56: 解析成功（HTTP 200 + 无异常）但 summary 为空 —— silent empty。
    # 接受空 summary 继续推进（避免重试爆炸），但必须打 warning 让运维可见。
    # core/engine.py:49 的 `if ctx.summary_text:` 守卫会确保空 summary 不落 messages.json，
    # 这是预期行为，handler 只负责让"摘要丢失"这件事可观测。
    if not analysis.summary:
        logger.warning(
            "⚠️  AI 摘要解析为空（source_id=%s, source=%s, raw 长度=%d）— 检查 LLM 输出格式或 reasoning_content 兜底",
            source_id,
            analysis.source,
            len(analysis.raw),
        )
    ctx.summary_text = analysis.summary
    ctx.keywords = analysis.keywords

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("bili", Phase.PUSHED)
async def bili_push(ctx: PhaseContext) -> bool:
    """推送 B站通知（视频 / 动态），fan-out 到订阅声明的所有 endpoints。"""
    # 手动重跑模式（plan 2026-06-28 D4/D7）：skip_push=True 时跳过 send_to_subscription，
    # 但 phase 仍推进到 PUSHED（dashboard 状态正确）。
    # 注意：skip_push 提前 return 同时跳过 media cleanup（这是有意为之，
    # 保留本地视频文件以便后续手动重跑时不需重新下载）。
    if ctx.skip_push:
        logger.info("⏭ 跳过推送（skip_push=True）: %s", ctx.msg.msg_id)
        return True

    is_dynamic = ctx.msg.msg_id.startswith("bili_dyn:")
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

    # TEXT 类型(纯文字动态)不走 SUMMARIZED 阶段, ctx.summary_text 恒空;
    # 此时 fallback 到 ctx.content_text(detector/download 已填充的正文原文),
    # 让通知正文不再丢失 (issue #80)。
    # 复用 summary 字段承载:VIDEO 类型 = AI 摘要;TEXT 类型 = 正文原文。
    summary_text = ctx.summary_text or ctx.content_text
    content = NotificationContent(
        platform="bili",
        source_id=source_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=summary_text,
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

    # plan D6: 媒体清理条件改为 downloaded_filepath is not None
    # (改造后 TEXT 类型无视频文件,filepath 始终 None,条件自然不成立)
    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=source_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)

    return True
