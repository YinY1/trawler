"""微博流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

# pyright: basic
import logging

from core.engine import PipelineEngine
from core.formatter import format_comment_highlights
from core.notifiers import send_to_subscription
from core.summarizer import extract_keywords, generate_summary
from platforms.weibo.api import fetch_user_posts
from platforms.weibo.comments import fetch_weibo_comment_highlights
from platforms.weibo.downloader import download_weibo_media
from platforms.weibo.parser import parse_weibo_post
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NotificationContent, Phase, PhaseContext

logger = logging.getLogger("trawler.weibo.handlers")


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
                subscription_ref=sub.user_id,
            )


# -- Phase: DOWNLOADED -------------------------------------------


@PipelineEngine.register("weibo", Phase.DOWNLOADED)
async def weibo_download(ctx: PhaseContext) -> bool:
    """下载微博媒体、解析内容、生成摘要和关键词。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")
    logger.info("⬇ 下载 %s (%s)...", ctx.msg.title, post_id)

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
        logger.error("✗ %s", ctx.error)
        logger.exception("Weibo download failed for %s", post_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        logger.warning("⚠️  %s", ctx.error)
        return False

    ctx.image_paths = result.image_paths
    ctx.content_text = result.text
    logger.info("✓ 下载完成")

    # Parse content
    try:
        parsed = parse_weibo_post(post=post, download_result=result)
        if parsed:
            ctx.content_text = parsed.get("text", ctx.content_text)
    except Exception as exc:
        logger.warning("⚠️  内容解析失败: %s", exc)
        logger.warning("Weibo parse failed for %s: %s", post_id, exc)

    # Generate summary and keywords (TEXT type skips SUMMARIZED phase)
    try:
        summary_text, source, _ = await generate_summary(
            source_id=post_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=ctx.content_text,
            config=ctx.config,
        )
        ctx.summary_text = summary_text
        logger.info("📝 摘要 (%s)", source)
    except Exception as exc:
        logger.warning("⚠️  摘要生成失败: %s", exc)
        ctx.summary_text = ctx.content_text[:500]

    try:
        ctx.keywords = await extract_keywords(
            text=ctx.content_text,
            title=ctx.msg.title,
            author=ctx.msg.author,
            config=ctx.config,
        )
    except Exception as exc:
        logger.warning("⚠️  关键词提取失败: %s", exc)
        ctx.keywords = []

    # Fetch comment highlights
    try:
        highlights = await fetch_weibo_comment_highlights(
            post_id=post_id,
            config=ctx.config,
        )
        ctx.comment_highlights = format_comment_highlights(highlights)
        if highlights:
            logger.info("💬 获取到 %d 条热门评论", len(highlights))
    except Exception as exc:
        logger.warning("⚠️  评论获取失败: %s", exc)
        ctx.comment_highlights = ""

    return True


# -- Phase: PUSHED ----------------------------------------------


@PipelineEngine.register("weibo", Phase.PUSHED)
async def weibo_push(ctx: PhaseContext) -> bool:
    """推送微博通知。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")

    matched = None
    for sub in ctx.config.weibo.subscriptions:
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
        platform="weibo",
        source_id=post_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
    )
    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(ctx.config, "weibo", matched.notify_endpoints, content)
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))
    return True
