"""流程编排模块 - 纯编排层，不包含业务逻辑

所有跨模块调用严格匹配各模块的实际函数签名：
- comments.fetch_comment_highlights(bvid, config)
- downloader.download_video(bvid, config)
- transcriber.transcribe_file(filepath, config, source_id, title, author)
- transcriber.cleanup_media(filepath, source_id)
- summarizer.generate_summary(source_id, title, author, text, config) -> (str, str, bool)
- summarizer.extract_keywords(text, title, author, config)
- notifier.notify_new_video(bvid, title, author, summary, keywords, comment_highlights, config)
- notifier.notify_new_xhs_note(note_id, title, author, summary, keywords, comment_highlights, xhs_noti_config)
- notifier.notify_dynamic(dynamic_info: dict, config: NotificationConfig) -> bool
"""

from __future__ import annotations

import logging

from rich.console import Console

from shared.config import Config
from shared.protocols import Phase

console = Console()
logger = logging.getLogger(__name__)

from shared.http import close_session  # noqa: E402

# ═══════════════════════════════════════════════════════════
# B站完整流程
# ═══════════════════════════════════════════════════════════


async def run_bili_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """B站完整检查流程（视频+动态统一通过 PipelineEngine 处理）"""
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

    # 关闭全局 aiohttp session
    await close_session()
