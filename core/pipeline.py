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

from rich.console import Console

from shared.config import Config
from shared.protocols import Phase

console = Console()
logger = logging.getLogger(__name__)

# ── 下载 ──────────────────────────────────────────────────
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


# ═══════════════════════════════════════════════════════════
# B站完整流程
# ═══════════════════════════════════════════════════════════


async def run_bili_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """B站完整检查流程"""
    from core.engine import PipelineEngine

    await PipelineEngine.run_platform(config, "bili", from_phase=from_phase)


# ═══════════════════════════════════════════════════════════
# 小红书完整流程
# ═══════════════════════════════════════════════════════════


async def run_xhs_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """小红书完整检查流程"""
    from core.engine import PipelineEngine

    await PipelineEngine.run_platform(config, "xhs", from_phase=from_phase)


# ═══════════════════════════════════════════════════════════
# 微博完整流程
# ═══════════════════════════════════════════════════════════


async def run_weibo_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """微博完整检查流程"""
    from core.engine import PipelineEngine

    await PipelineEngine.run_platform(config, "weibo", from_phase=from_phase)


# ═══════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════


async def run_check_once(
    config: Config,
    platform: str = "all",
    config_path: str = "config.toml",
    from_phase: str | None = None,
) -> None:
    """统一检查入口

    Args:
        config: 全局配置
        platform: "all" | "bili" | "xhs" | "weibo"
        config_path: 配置文件路径，用于 token 续期后的磁盘写入
        from_phase: 可选，从指定阶段重新开始处理
    """
    global _run_stats  # noqa: PLW0603
    _run_stats = _Stats()

    # 将字符串转换为 Phase 枚举
    _phase: Phase | None = None
    if from_phase is not None:
        _phase = Phase[from_phase.upper()]

    console.print()
    console.rule("[bold]Trawler v0.1.0[/bold]")
    console.print()

    if platform in ("all", "bili"):
        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens("bilibili", config, config_path)
        await run_bili_check_once(config, from_phase=_phase)

    if platform in ("all", "xhs") and config.xiaohongshu.enabled:
        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens("xhs", config, config_path)
        await run_xhs_check_once(config, from_phase=_phase)

    if platform in ("all", "weibo") and config.weibo.enabled:
        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens("weibo", config, config_path)
        await run_weibo_check_once(config, from_phase=_phase)

    # 打印统计
    console.print()
    console.rule("[bold]运行统计[/bold]")
    console.print(_run_stats.report())
    console.print()

    # 关闭全局 aiohttp session
    await close_session()
