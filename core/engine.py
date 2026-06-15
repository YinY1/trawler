"""流水线引擎 — 注册表模式 + 统一流水线编排

核心概念：
- ``PipelineEngine`` 提供 ``@register(platform, phase)`` 和 ``@register_detector(platform)`` 装饰器
- 各平台在 ``handlers.py`` 中通过装饰器注册 handler
- 跨平台共用 handler 使用 ``"*"`` 作为 platform 通配符
- ``run_platform()`` 是统一入口：cleanup -> detect -> process

重要：模块被导入时通过装饰器自动注册 handler。
``run_platform()`` 在生产路径下运行时自动延迟导入对应 handler 模块。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import PHASE_FLOW, Phase, PhaseContext

logger = logging.getLogger(__name__)

# Handler 类型：接收 PhaseContext，返回 bool（True=成功）
PhaseHandler = Callable[[PhaseContext], Awaitable[bool]]


class PipelineEngine:
    """统一流水线引擎。

    使用类变量注册表（允许跨模块导入时自动注册）：
    - ``_handlers``:  {(platform, phase): handler}
    - ``_detectors``: {platform: detector}

    平台 handler 模块路径映射（供 ``run_platform()`` 延迟导入）。
    """

    _handlers: dict[tuple[str, Phase], PhaseHandler] = {}
    _detectors: dict[str, Callable[..., Awaitable[None]]] = {}
    _HANDLER_MODULES: dict[str, str] = {
        "bili": "platforms.bilibili.handlers",
        "xhs": "platforms.xiaohongshu.handlers",
        "weibo": "platforms.weibo.handlers",
    }

    # ── 注册 ─────────────────────────────────────────────────

    @classmethod
    def register(cls, platform: str, phase: Phase) -> Callable[[PhaseHandler], PhaseHandler]:
        """装饰器：注册某平台某阶段的 handler。

        跨平台共用 handler 使用 ``"*"`` 作为 platform 值。
        查找时优先精确匹配 (platform, phase)，fallback 到 ("*", phase)。

        Usage::

            @PipelineEngine.register("bili", Phase.DOWNLOADED)
            async def bili_download(ctx: PhaseContext) -> bool:
                ...
        """

        def decorator(handler: PhaseHandler) -> PhaseHandler:
            cls._handlers[(platform, phase)] = handler
            return handler

        return decorator

    @classmethod
    def register_detector(cls, platform: str) -> Callable[..., Any]:
        """装饰器：注册某平台的 detector 函数。

        Usage::

            @PipelineEngine.register_detector("bili")
            async def bili_detector(config: Config, store: MessageStore) -> None:
                ...
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            cls._detectors[platform] = func
            return func

        return decorator

    # ── 处理 ─────────────────────────────────────────────────

    @classmethod
    async def process_message(
        cls,
        msg: Any,  # MessageRecord (avoid circular import issue; typed at runtime)
        config: Config,
        store: MessageStore,
    ) -> None:
        """从当前 phase 开始逐阶段推进消息。

        每推进一个阶段立即 ``save()``，避免中途崩溃丢失进度。
        如果某阶段未注册 handler，记录错误并停止（不推进 phase）。
        """
        from shared.protocols import MessageRecord

        assert isinstance(msg, MessageRecord), f"expected MessageRecord, got {type(msg)}"
        ctx = PhaseContext(msg=msg, config=config)
        phases = PHASE_FLOW[msg.content_type]

        start_idx = phases.index(msg.phase)
        for next_phase in phases[start_idx + 1 :]:
            handler = cls._handlers.get((msg.platform, next_phase))
            if handler is None:
                handler = cls._handlers.get(("*", next_phase))
            if handler is None:
                logger.error("No handler for %s / %s — stopping", msg.platform, next_phase)
                ctx.error = f"missing handler: {msg.platform}/{next_phase.name}"
                store.mark_error(msg.msg_id, ctx.error)
                store.save()
                break

            success = await handler(ctx)
            if not success:
                store.mark_error(msg.msg_id, ctx.error)
                store.save()
                break

            msg.phase = next_phase
            store.mark_phase(msg.msg_id, next_phase)
            store.save()

    @classmethod
    async def run_platform(
        cls,
        config: Config,
        platform: str,
        from_phase: Phase | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """统一平台入口：cleanup -> detect -> process。

        Detector 支持前缀匹配：``"bili"`` 会运行所有以 ``"bili"``
        开头的 detector key（如 ``"bili"`` 和 ``"bili_dynamic"``）。

        Args:
            config: 全局配置
            platform: 平台标识 ("bili" | "xhs" | "weibo")
            from_phase: 可选，将所有消息回退到指定阶段后重新处理
            log_callback: 可选，``(event_type, message)`` 回调，用于流式日志输出
        """
        store = MessageStore(config.general.data_dir)
        store.cleanup(24)

        if log_callback:
            log_callback("log", f"🔍 开始检查 {platform} 平台...")
            log_callback("log", "🧹 已清理超过 24 小时的消息")

        if from_phase is not None:
            store.reset_to_phase(from_phase, platform=platform)

        # 延迟导入对应平台的 handler 模块（触发装饰器注册）
        module_path = cls._HANDLER_MODULES.get(platform)
        if module_path is not None:
            importlib.import_module(module_path)

        # 前缀匹配 detector：导入 handler 后，_detectors 中可能有
        # 多个以 platform 开头的 key（如 "bili" + "bili_dynamic"）
        matching_keys = [key for key in cls._detectors if key == platform or key.startswith(f"{platform}_")]
        for key in matching_keys:
            detector = cls._detectors.get(key)
            if detector is not None:
                await detector(config, store)

        # 消息处理：仍使用原始 platform 字符串
        # （MessageRecord.platform 统一为 "bili"，不区分 video/dynamic）
        for msg in store.get_messages(phase=Phase.PUSHED, exclude=True, platform=platform):
            await cls.process_message(msg, config, store)

        if log_callback:
            log_callback("done", f"✅ {platform} 检查完成")

        store.save()
