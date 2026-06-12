"""流程编排模块 - 纯编排层，不包含业务逻辑

所有跨模块调用严格匹配各模块的实际函数签名：
- monitor.check_new_videos(uid, config, store)
- rss_monitor.RSSMonitor(config).check_up(uid, name, store)
- dynamic.check_new_dynamics(uid, config, store)
- comments.fetch_comment_highlights(bvid, config)
- downloader.download_video(bvid, config)
- transcriber.transcribe_file(filepath, config, source_id, title, author)
- transcriber.cleanup_media(filepath, source_id)
- summarizer.generate_summary(source_id, title, author, text, config) -> (str, str, bool)
- summarizer.extract_keywords(text, title, author, config)
- notifier.notify_new_video(bvid, title, author, summary, keywords, comment_highlights, config)
- notifier.notify_new_xhs_note(note_id, title, author, summary, keywords, comment_highlights, xhs_noti_config)
- notifier.notify_dynamic(dynamic_info: dict, config: NotificationConfig) -> bool
- xhs_monitor.check_new_notes(user_id, name, config, store)
- xhs_downloader.download_note(note, config)
- xhs_parser.parse_note_content(note, download_result)
- xhs_comments.fetch_xhs_comment_highlights(note_id, config)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from rich.console import Console

from shared.config import Config
from shared.protocols import (
    CommentHighlight,
    DownloadResult,
    DynamicInfo,
    NoteInfo,
    ParsedNote,
    TranscriptResult,
    VideoInfo,
    WeiboCommentHighlight,
    WeiboDownloadResult,
    WeiboPost,
    XhsCommentHighlight,
    XhsDownloadResult,
)

console = Console()
logger = logging.getLogger(__name__)

# ── B站 ───────────────────────────────────────────────────
from core.notifier import notify_dynamic, notify_new_video, notify_new_weibo_post, notify_new_xhs_note  # noqa: E402
from core.summarizer import extract_keywords, generate_summary  # noqa: E402

# ── 核心处理 ──────────────────────────────────────────────
from core.transcriber import cleanup_media, transcribe_file_async  # noqa: E402
from platforms.bilibili.comments import fetch_comment_highlights  # noqa: E402
from platforms.bilibili.dynamic import check_new_dynamics  # noqa: E402
from platforms.bilibili.monitor import SubscriptionStore, check_new_videos  # noqa: E402
from platforms.bilibili.rss_monitor import RSSAllFailedError, RSSMonitor  # noqa: E402
from platforms.weibo.comments import fetch_weibo_comment_highlights  # noqa: E402
from platforms.weibo.downloader import download_weibo_media  # noqa: E402

# ── 微博 ──────────────────────────────────────────────────
from platforms.weibo.monitor import (  # noqa: E402
    WeiboSubscriptionStore,
    check_new_weibo_posts,
)
from platforms.weibo.parser import parse_weibo_post  # noqa: E402
from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights  # noqa: E402
from platforms.xiaohongshu.downloader import (  # noqa: E402
    download_note,
)

# ── 小红书 ────────────────────────────────────────────────
from platforms.xiaohongshu.monitor import (  # noqa: E402
    XhsSubscriptionStore,
    check_new_notes,
)
from platforms.xiaohongshu.parser import parse_note_content  # noqa: E402

# ── 下载 ──────────────────────────────────────────────────
from shared.downloader import download_video  # noqa: E402
from shared.http import close_session  # noqa: E402

# ── 统计 ──────────────────────────────────────────────────


class _Stats:
    """单次运行统计"""

    def __init__(self) -> None:
        self.videos_processed: int = 0
        self.videos_succeeded: int = 0
        self.videos_failed: int = 0
        self.dynamics_processed: int = 0
        self.dynamics_succeeded: int = 0
        self.dynamics_failed: int = 0
        self.notes_processed: int = 0
        self.notes_succeeded: int = 0
        self.notes_failed: int = 0
        self.weibo_posts_processed: int = 0
        self.weibo_posts_succeeded: int = 0
        self.weibo_posts_failed: int = 0

    def report(self) -> str:
        lines: list[str] = []
        if self.videos_processed:
            lines.append(
                f"  视频: {self.videos_processed} 处理, {self.videos_succeeded} 成功, {self.videos_failed} 失败"
            )
        if self.dynamics_processed:
            lines.append(
                f"  动态: {self.dynamics_processed} 处理, {self.dynamics_succeeded} 成功, {self.dynamics_failed} 失败"
            )
        if self.notes_processed:
            lines.append(f"  笔记: {self.notes_processed} 处理, {self.notes_succeeded} 成功, {self.notes_failed} 失败")
        if self.weibo_posts_processed:
            lines.append(
                f"  微博: {self.weibo_posts_processed} 处理, "
                f"{self.weibo_posts_succeeded} 成功, "
                f"{self.weibo_posts_failed} 失败"
            )
        return "\n".join(lines) if lines else "  无内容需要处理"


# 模块级统计，供 CLI 读取
_run_stats: _Stats | None = None


def get_last_stats() -> _Stats | None:
    """返回最近一次运行的统计"""
    return _run_stats


# ── 辅助 ──────────────────────────────────────────────────


def _format_comment_highlights(highlights: list[Any]) -> str:
    """将评论亮点列表格式化为 Markdown 文本"""
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_up_owner", False) or getattr(h, "is_author", False)
        tag = " (UP主)" if is_author else ""
        parts.append(f"- **{name}**{tag} (👍{like}):\n  {content}")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════
# B站完整流程
# ═══════════════════════════════════════════════════════════


async def run_bili_check_once(config: Config) -> None:
    """B站完整检查流程"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None  # 由 run_check_once 初始化

    store = SubscriptionStore("data")

    # ── 1. 检查新视频 ──────────────────────────────────────
    new_videos: list[VideoInfo] = []

    if config.bilibili.monitor.mode == "rss":
        try:
            console.print("[cyan]🔍 使用 RSS 模式检查新视频…[/]")
            rss_monitor = RSSMonitor(config)
            for sub in config.bilibili.subscriptions:
                try:
                    videos, _ok = await rss_monitor.check_up(
                        uid=sub.uid,
                        name=sub.name,
                        store=store,
                    )
                    new_videos.extend(videos)
                except RSSAllFailedError:
                    raise  # 向上传递
                except Exception as exc:
                    console.print(f"[yellow]⚠️  RSS 检查 {sub.name}({sub.uid}) 失败: {exc}[/]")
        except RSSAllFailedError as exc:
            console.print(f"[yellow]⚠️  RSS 全部失败，降级到 API 模式: {exc}[/]")
            logger.warning("RSS all failed, falling back to API: %s", exc)
            new_videos = await _api_check(config, store)
    else:
        console.print("[cyan]🔍 使用 API 模式检查新视频…[/]")
        new_videos = await _api_check(config, store)

    # ── 2. 处理新视频 ──────────────────────────────────────
    for video in new_videos:
        if _run_stats.videos_processed >= config.bilibili.monitor.max_videos_per_check:
            break
        _run_stats.videos_processed += 1
        try:
            await process_video(
                bvid=video.bvid,
                title=video.title,
                author=video.author,
                uid=video.uid,
                pubdate=video.pubdate,
                duration=video.duration,
                config=config,
                store=store,
            )
            _run_stats.videos_succeeded += 1
        except Exception as exc:
            _run_stats.videos_failed += 1
            console.print(f"[red]✗ 处理视频 {video.bvid} 失败: {exc}[/]")
            logger.exception("Failed to process video %s", video.bvid)

    # ── 3. 检查新动态 ──────────────────────────────────────
    if config.bilibili.monitor.watch_dynamic:
        console.print("[cyan]🔍 检查新动态…[/]")
        for sub in config.bilibili.subscriptions:
            try:
                new_dynamics = await check_new_dynamics(
                    uid=sub.uid,
                    config=config,
                    store=store,
                )
                for dyn in new_dynamics:
                    _run_stats.dynamics_processed += 1
                    try:
                        await process_dynamic(dyn, config, store)
                        _run_stats.dynamics_succeeded += 1
                    except Exception as exc:
                        _run_stats.dynamics_failed += 1
                        console.print(f"[red]✗ 处理动态失败: {exc}[/]")
                        logger.exception("Failed to process dynamic")
            except Exception as exc:
                console.print(f"[yellow]⚠️  检查 {sub.name}({sub.uid}) 动态失败: {exc}[/]")
                logger.warning(
                    "Failed to check dynamics for %s(%s): %s",
                    sub.name,
                    sub.uid,
                    exc,
                )

    # 持久化 Store（所有视频/动态处理完成后统一保存）
    store.save()
    console.print("[green]✓ B站检查完成[/]")


async def _api_check(config: Config, store: SubscriptionStore) -> list[VideoInfo]:
    """API 模式逐个检查订阅 UP 主"""
    all_new: list[VideoInfo] = []
    for sub in config.bilibili.subscriptions:
        try:
            new = await check_new_videos(uid=sub.uid, config=config, store=store)
            all_new.extend(new)
        except Exception as exc:
            console.print(f"[yellow]⚠️  检查 {sub.name}({sub.uid}) 失败: {exc}[/]")
            logger.warning("API check failed for %s(%s): %s", sub.name, sub.uid, exc)
    return all_new


# ═══════════════════════════════════════════════════════════
# B站视频处理流水线
# ═══════════════════════════════════════════════════════════


async def process_video(
    bvid: str,
    title: str,
    author: str,
    uid: int,
    pubdate: int,
    duration: int,
    config: Config,
    store: SubscriptionStore,
) -> None:
    """处理单个 B站视频的完整流水线"""
    console.print(f"[bold blue]▶ 处理视频[/] {title} ({bvid})")

    # Step 1: 下载
    dl_result: DownloadResult | None = None
    try:
        console.print("  [dim]⬇ 下载中…[/]")
        dl_result = await download_video(bvid=bvid, config=config)
    except Exception as exc:
        console.print(f"  [red]✗ 下载失败: {exc}[/]")
        logger.exception("Download failed for %s", bvid)
        # 下载失败仍标记为已知，避免重复尝试
        store.mark_known(bvid)
        return

    if not dl_result.success:
        console.print(f"  [yellow]⚠️  下载未成功: {dl_result.error}[/]")
        store.mark_known(bvid)
        return

    # Step 2: 转写
    transcript: TranscriptResult | None = None
    try:
        console.print("  [dim]📝 转写中…[/]")
        # filepath 可能为 None（下载成功但路径不确定）
        _fp = dl_result.filepath or Path()
        if not _fp.exists():
            console.print("  [yellow]⚠️  下载文件路径无效，跳过转写[/]")
        else:
            transcript = await transcribe_file_async(
                filepath=_fp,
                config=config,
                source_id=bvid,
                title=title,
                author=author,
            )
    except Exception as exc:
        console.print(f"  [red]✗ 转写失败: {exc}[/]")
        logger.exception("Transcribe failed for %s", bvid)

    # Step 3: 评论亮点
    highlights: list[CommentHighlight] = []
    try:
        console.print("  [dim]💬 获取评论亮点…[/]")
        highlights = await fetch_comment_highlights(bvid=bvid, config=config)
    except Exception as exc:
        console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
        logger.warning("Comment highlights failed for %s: %s", bvid, exc)

    # Step 4: 生成摘要
    summary_text: str = ""
    try:
        console.print("  [dim]🤖 生成摘要…[/]")
        transcript_text = transcript.text if transcript and transcript.success else ""
        summary_text, _source, _is_ai = generate_summary(
            source_id=bvid,
            title=title,
            author=author,
            text=transcript_text,
            config=config,
        )
    except Exception as exc:
        console.print(f"  [red]✗ 摘要生成失败: {exc}[/]")
        logger.exception("Summary failed for %s", bvid)

    # Step 5: 提取关键词
    keywords: list[str] = []
    try:
        keywords = extract_keywords(
            text=summary_text,
            title=title,
            author=author,
            config=config,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/]")
        logger.warning("Keywords failed for %s: %s", bvid, exc)

    # Step 6: 通知推送
    try:
        comment_md = _format_comment_highlights(highlights)
        await notify_new_video(
            bvid=bvid,
            title=title,
            author=author,
            summary=summary_text,
            keywords=keywords,
            comment_highlights=comment_md,
            config=config.bilibili.notification,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("Notify failed for %s: %s", bvid, exc)

    # Step 7: 清理媒体
    if config.transcribe.delete_after_transcribe and dl_result.filepath:
        try:
            cleanup_media(filepath=dl_result.filepath, source_id=bvid)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("Cleanup failed for %s: %s", bvid, exc)

    # Step 8: 标记已知
    store.mark_known(bvid)

    console.print("  [green]✓ 视频处理完成[/]")


# ═══════════════════════════════════════════════════════════
# B站动态处理
# ═══════════════════════════════════════════════════════════


async def process_dynamic(
    dynamic_info: DynamicInfo,
    config: Config,
    store: SubscriptionStore,
) -> None:
    """处理单条 B站动态"""
    console.print(f"[bold blue]▶ 处理动态[/] {dynamic_info.dynamic_id}")

    # 如果动态关联了视频，触发视频处理（跳过已处理的视频）
    if dynamic_info.linked_bvid:
        if store.is_known(dynamic_info.linked_bvid):
            console.print(f"  [dim]关联视频 {dynamic_info.linked_bvid} 已处理过，跳过[/]")
        else:
            try:
                await process_video(
                    bvid=dynamic_info.linked_bvid,
                    title=dynamic_info.title or "",
                    author=dynamic_info.author or "",
                    uid=dynamic_info.uid,
                    pubdate=dynamic_info.pubdate,
                    duration=0,  # 动态中没有 duration
                    config=config,
                    store=store,
                )
            except Exception as exc:
                console.print(f"[red]✗ 动态关联视频处理失败: {exc}[/]")
                logger.exception(
                    "Linked video process failed for dynamic %s",
                    dynamic_info.dynamic_id,
                )

    # 通知推送
    try:
        await notify_dynamic(
            dynamic_info={
                "user": dynamic_info.author,
                "content": dynamic_info.content or dynamic_info.title,
                "dynamic_id": dynamic_info.dynamic_id,
                "type": "动态",
                "url": dynamic_info.link,
            },
            config=config.bilibili.notification,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  动态通知推送失败: {exc}[/]")
        logger.warning("Dynamic notify failed for %s: %s", dynamic_info.dynamic_id, exc)

    # 标记动态已知
    dedup_key = f"dyn_{dynamic_info.dynamic_id}"
    store.mark_known(dedup_key)

    console.print("  [green]✓ 动态处理完成[/]")


# ═══════════════════════════════════════════════════════════
# 小红书完整流程
# ═══════════════════════════════════════════════════════════


async def run_xhs_check_once(config: Config) -> None:
    """小红书完整检查流程"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None  # 由 run_check_once 初始化

    store = XhsSubscriptionStore("data")

    console.print("[cyan]🔍 检查小红书新笔记…[/]")

    for sub in config.xiaohongshu.subscriptions:
        try:
            new_notes = await check_new_notes(
                user_id=sub.user_id,
                name=sub.name,
                config=config,
                store=store,
            )
            for note in new_notes:
                _run_stats.notes_processed += 1
                try:
                    await process_xhs_note(note, config, store)
                    _run_stats.notes_succeeded += 1
                except Exception as exc:
                    _run_stats.notes_failed += 1
                    console.print(f"[red]✗ 处理笔记 {note.note_id} 失败: {exc}[/]")
                    logger.exception("Failed to process note %s", note.note_id)
        except Exception as exc:
            console.print(f"[yellow]⚠️  检查 {sub.name}({sub.user_id}) 失败: {exc}[/]")
            logger.warning("XHS check failed for %s(%s): %s", sub.name, sub.user_id, exc)

    # 持久化 Store（所有笔记处理完成后统一保存）
    store.save()
    console.print("[green]✓ 小红书检查完成[/]")


# ═══════════════════════════════════════════════════════════
# 小红书笔记处理流水线
# ═══════════════════════════════════════════════════════════


async def process_xhs_note(
    note: NoteInfo,
    config: Config,
    store: XhsSubscriptionStore,
) -> None:
    """处理单个小红书笔记的完整流水线"""
    console.print(f"[bold magenta]▶ 处理笔记[/bold magenta] {note.title} ({note.note_id})")

    # Step 1: 下载
    dl_result: XhsDownloadResult | None = None
    try:
        console.print("  [dim]⬇ 下载中…[/]")
        dl_result = await download_note(note=note, config=config)
    except Exception as exc:
        console.print(f"  [red]✗ 下载失败: {exc}[/]")
        logger.exception("XHS download failed for %s", note.note_id)
        store.mark_known_note(note)
        return

    if not dl_result.success:
        console.print(f"  [yellow]⚠️  下载未成功: {dl_result.error}[/]")
        store.mark_known_note(note)
        return

    # Step 2: 解析笔记内容
    parsed: ParsedNote | None = None
    try:
        console.print("  [dim]📄 解析内容…[/]")
        parsed = parse_note_content(note=note, download_result=dl_result)
    except Exception as exc:
        console.print(f"  [red]✗ 内容解析失败: {exc}[/]")
        logger.exception("XHS parse failed for %s", note.note_id)

    # Step 3: 视频笔记转写
    transcript_text: str = ""
    is_video = parsed.is_video if parsed else (note.note_type == "video")
    video_path = parsed.video_path if parsed else dl_result.filepath

    if is_video and video_path and video_path.exists():
        try:
            console.print("  [dim]📝 视频转写中…[/]")
            transcript = await transcribe_file_async(
                filepath=video_path,
                config=config,
                source_id=note.note_id,
                title=note.title,
                author=note.author,
            )
            if transcript.success:
                transcript_text = transcript.text
        except Exception as exc:
            console.print(f"  [yellow]⚠️  视频转写失败: {exc}[/]")
            logger.warning("XHS transcribe failed for %s: %s", note.note_id, exc)

    # Step 4: 评论亮点
    highlights: list[XhsCommentHighlight] = []
    try:
        console.print("  [dim]💬 获取评论亮点…[/]")
        highlights = await fetch_xhs_comment_highlights(note_id=note.note_id, config=config)
    except Exception as exc:
        console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
        logger.warning("XHS comment highlights failed for %s: %s", note.note_id, exc)

    # Step 5: 生成摘要
    summary_text: str = ""
    content_text = (parsed.text if parsed else "") or note.desc or ""
    combined = f"{content_text}\n{transcript_text}".strip()

    try:
        console.print("  [dim]🤖 生成摘要…[/]")
        summary_text, _source, _is_ai = generate_summary(
            source_id=note.note_id,
            title=note.title,
            author=note.author,
            text=combined,
            config=config,
        )
    except Exception as exc:
        console.print(f"  [red]✗ 摘要生成失败: {exc}[/]")
        logger.exception("XHS summary failed for %s", note.note_id)

    # Step 6: 提取关键词
    keywords: list[str] = []
    try:
        keywords = extract_keywords(
            text=summary_text,
            title=note.title,
            author=note.author,
            config=config,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/]")
        logger.warning("XHS keywords failed for %s: %s", note.note_id, exc)

    # Step 7: 通知推送
    try:
        comment_md = _format_comment_highlights(highlights)
        await notify_new_xhs_note(
            note_id=note.note_id,
            title=note.title,
            author=note.author,
            summary=summary_text,
            keywords=keywords,
            comment_highlights=comment_md,
            xhs_noti_config=config.xiaohongshu.notification,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("XHS notify failed for %s: %s", note.note_id, exc)

    # Step 8: 标记已知 & 清理
    store.mark_known_note(note)

    if config.transcribe.delete_after_transcribe and is_video and video_path:
        try:
            cleanup_media(filepath=video_path, source_id=note.note_id)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("XHS cleanup failed for %s: %s", note.note_id, exc)

    console.print("  [green]✓ 笔记处理完成[/]")


# ═══════════════════════════════════════════════════════════
# 微博完整流程
# ═══════════════════════════════════════════════════════════


async def run_weibo_check_once(config: Config) -> None:
    """微博完整检查流程"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None  # 由 run_check_once 初始化

    store = WeiboSubscriptionStore("data")

    console.print("[cyan]🔍 检查微博新帖子…[/]")

    for sub in config.weibo.subscriptions:
        try:
            new_posts = await check_new_weibo_posts(
                user_id=sub.user_id,
                name=sub.name,
                config=config,
                store=store,
            )
            for post in new_posts:
                _run_stats.weibo_posts_processed += 1
                try:
                    await process_weibo_post(post, config, store)
                    _run_stats.weibo_posts_succeeded += 1
                except Exception as exc:
                    _run_stats.weibo_posts_failed += 1
                    console.print(f"[red]✗ 处理微博 {post.post_id} 失败: {exc}[/]")
                    logger.exception("Failed to process weibo post %s", post.post_id)
        except Exception as exc:
            console.print(f"[yellow]⚠️  检查 {sub.name}({sub.user_id}) 失败: {exc}[/]")
            logger.warning("Weibo check failed for %s(%s): %s", sub.name, sub.user_id, exc)

    # 持久化 Store
    store.save()
    console.print("[green]✓ 微博检查完成[/]")


# ═══════════════════════════════════════════════════════════
# 微博帖子处理流水线
# ═══════════════════════════════════════════════════════════


async def process_weibo_post(
    post: WeiboPost,
    config: Config,
    store: WeiboSubscriptionStore,
) -> None:
    """处理单个微博帖子的完整流水线"""
    display_title = post.clean_text[:50] if post.clean_text else post.post_id
    console.print(f"[bold yellow]▶ 处理微博[/] {display_title} ({post.post_id})")

    # Step 1: 下载媒体
    dl_result: WeiboDownloadResult | None = None
    try:
        console.print("  [dim]⬇ 下载图片…[/]")
        dl_result = await download_weibo_media(post=post, config=config)
    except Exception as exc:
        console.print(f"  [red]✗ 下载失败: {exc}[/]")
        logger.exception("Weibo download failed for %s", post.post_id)
        store.mark_known_weibo_post(post)
        return

    if not dl_result.success:
        console.print(f"  [yellow]⚠️  下载未成功: {dl_result.error}[/]")
        store.mark_known_weibo_post(post)
        return

    # Step 2: 解析内容
    parsed: dict = {}
    try:
        console.print("  [dim]📄 解析内容…[/]")
        parsed = parse_weibo_post(post=post, download_result=dl_result)
    except Exception as exc:
        console.print(f"  [red]✗ 内容解析失败: {exc}[/]")
        logger.exception("Weibo parse failed for %s", post.post_id)

    # Step 3: 评论亮点
    highlights: list[WeiboCommentHighlight] = []
    try:
        console.print("  [dim]💬 获取评论亮点…[/]")
        highlights = await fetch_weibo_comment_highlights(
            post_id=post.post_id,
            config=config,
            author_user_id=post.user_id,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
        logger.warning("Weibo comment highlights failed for %s: %s", post.post_id, exc)

    # Step 4: 生成摘要
    summary_text: str = ""
    content_text = parsed.get("text", "") or post.clean_text or ""

    try:
        console.print("  [dim]🤖 生成摘要…[/]")
        summary_text, _source, _is_ai = generate_summary(
            source_id=post.post_id,
            title=display_title,
            author=post.author,
            text=content_text,
            config=config,
        )
    except Exception as exc:
        console.print(f"  [red]✗ 摘要生成失败: {exc}[/]")
        logger.exception("Weibo summary failed for %s", post.post_id)

    # Step 5: 提取关键词
    keywords: list[str] = []
    topics = parsed.get("topics", [])
    try:
        keywords = extract_keywords(
            text=summary_text,
            title=display_title,
            author=post.author,
            config=config,
        )
        # 合并话题标签
        if topics:
            keywords = list(dict.fromkeys(topics + keywords))  # 去重保序
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/]")
        logger.warning("Weibo keywords failed for %s: %s", post.post_id, exc)
        keywords = topics  # 降级：使用话题标签

    # Step 6: 通知推送
    try:
        comment_md = _format_comment_highlights(highlights)
        await notify_new_weibo_post(
            post_id=post.post_id,
            title=display_title,
            author=post.author,
            summary=summary_text,
            keywords=keywords,
            comment_highlights=comment_md,
            weibo_noti_config=config.weibo.notification,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("Weibo notify failed for %s: %s", post.post_id, exc)

    # Step 7: 标记已知
    store.mark_known_weibo_post(post)

    console.print("  [green]✓ 微博处理完成[/]")


# ═══════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════


async def run_check_once(config: Config, platform: str = "all") -> None:
    """统一检查入口

    Args:
        config: 全局配置
        platform: "all" | "bili" | "xhs" | "weibo"
    """
    global _run_stats  # noqa: PLW0603
    _run_stats = _Stats()

    console.print()
    console.rule("[bold]Trawler v0.1.0[/bold]")
    console.print()

    if platform in ("all", "bili"):
        await run_bili_check_once(config)

    if platform in ("all", "xhs") and config.xiaohongshu.enabled:
        await run_xhs_check_once(config)

    if platform in ("all", "weibo") and config.weibo.enabled:
        await run_weibo_check_once(config)

    # 打印统计
    console.print()
    console.rule("[bold]运行统计[/bold]")
    console.print(_run_stats.report())
    console.print()

    # 关闭全局 aiohttp session
    await close_session()
