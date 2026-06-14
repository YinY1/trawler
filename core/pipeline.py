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
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Console

from shared.config import Config
from shared.protocols import Phase

console = Console()
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 平台注册表
# ═══════════════════════════════════════════════════════════


@dataclass
class PlatformDef:
    """平台注册定义。

    每个平台在 ``PLATFORM_REGISTRY`` 中注册一项即可自动接入
    ``run_check_once`` 的统一编排。

    Attributes:
        platform_key: 引擎内标识符（如 ``"bili"``）
        auth_name: Token 续期用平台名（如 ``"bilibili"``）
        enabled_check: 接收 Config 返回是否启用
    """

    platform_key: str
    auth_name: str
    enabled_check: Callable[[Config], bool]


PLATFORM_REGISTRY: dict[str, PlatformDef] = {
    "bili": PlatformDef(
        platform_key="bili",
        auth_name="bilibili",
        enabled_check=lambda _: True,
    ),
    "xhs": PlatformDef(
        platform_key="xhs",
        auth_name="xhs",
        enabled_check=lambda c: c.xiaohongshu.enabled,
    ),
    "weibo": PlatformDef(
        platform_key="weibo",
        auth_name="weibo",
        enabled_check=lambda c: c.weibo.enabled,
    ),
}


# ═══════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════


async def run_check_once(
    config: Config,
    platform: str = "all",
    config_path: str = "config/config.toml",
    from_phase: str | None = None,
) -> None:
    """统一检查入口 — 遍历 ``PLATFORM_REGISTRY`` 执行各平台。

    新增平台只需向 ``PLATFORM_REGISTRY`` 添加一项，无需修改此函数。

    Args:
        config: 全局配置
        platform: ``"all"`` | 平台 key（如 ``"bili"``）
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

    for pkey, pdef in PLATFORM_REGISTRY.items():
        if platform not in ("all", pkey):
            continue
        if not pdef.enabled_check(config):
            continue

        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens(pdef.auth_name, config, config_path)

        from core.engine import PipelineEngine

        await PipelineEngine.run_platform(config, pkey, from_phase=_phase)
