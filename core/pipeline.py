"""流程编排模块 - 纯编排层，不包含业务逻辑

所有跨模块调用严格匹配各模块的实际函数签名：
- comments.fetch_comment_highlights(bvid, config)
- downloader.download_video(bvid, config)
- transcriber.transcribe_file(filepath, config, source_id, title, author)
- transcriber.cleanup_media(filepath, source_id)
- summarizer.generate_summary(source_id, title, author, text, config) -> (str, str, bool)
- summarizer.extract_keywords(text, title, author, config)
- notifiers.send_to_subscription(config, platform, endpoint_names, content) -> list[SendResult]
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from shared.config import Config
from shared.constants import VERSION_DISPLAY
from shared.protocols import Phase

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
    log_callback: Callable[[str, str], None] | None = None,
) -> None:
    """统一检查入口 — 遍历 ``PLATFORM_REGISTRY`` 执行各平台。

    新增平台只需向 ``PLATFORM_REGISTRY`` 添加一项，无需修改此函数。

    Args:
        config: 全局配置
        platform: ``"all"`` | 平台 key（如 ``"bili"``）
        config_path: 配置文件路径，用于 token 续期后的磁盘写入
        from_phase: 可选，从指定阶段重新开始处理
        log_callback: 可选，``(event_type, message)`` 回调，用于流式日志输出
    """
    # 将字符串转换为 Phase 枚举
    _phase: Phase | None = None
    if from_phase is not None:
        _phase = Phase[from_phase.upper()]

    logger.info(f"▶ Trawler {VERSION_DISPLAY}")

    # 选出本次需要执行的平台（保持 PLATFORM_REGISTRY 顺序）
    selected = [
        (pkey, pdef)
        for pkey, pdef in PLATFORM_REGISTRY.items()
        if (platform in ("all", pkey)) and pdef.enabled_check(config)
    ]

    from shared.auth.scheduler import check_and_renew_tokens
    from shared.message_store import MessageStore

    if platform == "all" and len(selected) > 1:
        # 并发执行多平台：共享同一个 MessageStore 实例，避免各实例
        # 内存快照互相覆盖导致数据丢失。单线程事件循环下 MessageStore
        # 的同步写方法天然原子，无需加锁。
        # token 续期涉及磁盘写入，串行执行避免配置文件并发写
        for _pkey, pdef in selected:
            logger.info("🔑 检查 %s token 状态...", pdef.auth_name)
            await check_and_renew_tokens(pdef.auth_name, config, config_path)

        shared_store = MessageStore(config.general.data_dir)
        shared_store.cleanup(24)
        if log_callback:
            log_callback("log", "🧹 已清理超过 24 小时的消息")

        from core.engine import PipelineEngine

        tasks = [
            PipelineEngine.run_platform(config, pkey, from_phase=_phase, log_callback=log_callback, store=shared_store)
            for pkey, _pdef in selected
        ]
        # return_exceptions=True 防止单个平台失败中断其他平台
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for (pkey, _pdef), result in zip(selected, results, strict=True):
            # BaseException 包含 CancelledError：取消通常来自 shutdown，记 warning 即可，
            # 不当作普通错误打扰前端；Exception 才是真正的平台执行失败。
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                logger.warning("✗ 平台 %s 被取消/中断: %s", pkey, result)
                if log_callback:
                    log_callback("log", f"⏹ {pkey} 平台被取消: {result}")
            elif isinstance(result, Exception):
                logger.error("✗ 平台 %s 检查失败: %s", pkey, result, exc_info=result)
                if log_callback:
                    log_callback("error", f"✗ {pkey} 平台检查失败: {result}")
        # 每个 run_platform 内部已各自 save()，共享 store 无需额外落盘
    else:
        # 单平台或未启用其他平台：保持原有串行路径（兼容现有调用方）
        for pkey, pdef in selected:
            logger.info("🔑 检查 %s token 状态...", pdef.auth_name)
            await check_and_renew_tokens(pdef.auth_name, config, config_path)

            from core.engine import PipelineEngine

            await PipelineEngine.run_platform(config, pkey, from_phase=_phase, log_callback=log_callback)
