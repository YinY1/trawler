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
from shared.constants import MAX_SUMMARY_RETRIES
from shared.message_store import MessageStore
from shared.protocols import PHASE_FLOW, ContentType, MessageRecord, Phase, PhaseContext

logger = logging.getLogger(__name__)

# Handler 类型：接收 PhaseContext，返回 bool（True=成功）
PhaseHandler = Callable[[PhaseContext], Awaitable[bool]]

# body 硬截断上限（plan D3）：xhs/weibo 长文截断 5000 字 + 省略号；
# store 层不做限制，保持薄层
_BODY_MAX_CHARS = 5000


def _flush_ctx_to_store(msg_id: str, ctx: PhaseContext, store: MessageStore, just_completed: Phase) -> None:
    """阶段推进成功后，把 ctx 上对应阶段的产出回写到 store（plan D5）。

    - DOWNLOADED 完成：ctx.content_text → body（截断到 _BODY_MAX_CHARS）
    - SUMMARIZED 完成：ctx.summary_text → summary

    spec §6 / issue #46 PR-2: 移除 weibo 内联摘要路径后,
    summary 只在 SUMMARIZED 阶段 flush(不再有 DOWNLOADED 双 if 兼容)。
    """
    if just_completed == Phase.DOWNLOADED and ctx.content_text:
        body = ctx.content_text[:_BODY_MAX_CHARS]
        if len(ctx.content_text) > _BODY_MAX_CHARS:
            body += "…"
        store.mark_body(msg_id, body)
    if just_completed == Phase.SUMMARIZED and ctx.summary_text:
        store.mark_summary(msg_id, ctx.summary_text)


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
        assert isinstance(msg, MessageRecord), f"expected MessageRecord, got {type(msg)}"
        ctx = PhaseContext(msg=msg, config=config, skip_push=getattr(msg, "_skip_push", False))
        phases = PHASE_FLOW[msg.content_type]

        # Bug 3 fix: cross-process state recovery. MessageStore only persists
        # MessageRecord fields, so a VIDEO message that crashed after marking
        # DOWNLOADED but before the next save() loses ctx.downloaded_filepath
        # in the next cron process. If a VIDEO message resumes at DOWNLOADED or
        # later without a filepath, rewind to DISCOVERED so the download phase
        # re-runs and produces the filepath again.
        if msg.content_type == ContentType.VIDEO and msg.phase != Phase.DISCOVERED and ctx.downloaded_filepath is None:
            logger.warning(
                "▶ %s:%s 处于 %s 阶段但 downloaded_filepath 缺失（跨进程状态丢失），回退到 DISCOVERED 重新下载",
                msg.platform,
                msg.msg_id,
                msg.phase.name,
            )
            msg.phase = Phase.DISCOVERED
            ctx.msg.phase = Phase.DISCOVERED
            store.mark_phase(msg.msg_id, Phase.DISCOVERED)
            store.save()

        start_idx = phases.index(msg.phase)
        logger.info("▶ 处理消息 %s:%s (%s)", msg.platform, msg.msg_id, msg.title)
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
                # 失败处理：三档策略
                # 1. ctx.permanent_error=True（handler 主动标记永久失败）：
                #    直接 mark_error 跳过 retry，cron 永久跳过。
                #    用于 fail-fast 场景：transcribe 文件路径缺失、access_limited 等
                #    重试无意义的失败（Issue 6 / R10）。
                # 2. retry_count < MAX：写 last_error，cron 仍会重试此消息
                # 3. retry_count >= MAX：写 error，cron 永久跳过（避免无限重试）
                current = store.get_message(msg.msg_id)
                current_count = current.retry_count if current else 0
                if ctx.permanent_error:
                    store.mark_error(msg.msg_id, ctx.error)
                    logger.warning(
                        "⛔ %s:%s 永久失败（handler 标记 permanent_error）: %s（cron 将跳过）",
                        msg.platform,
                        msg.msg_id,
                        ctx.error,
                    )
                elif current_count + 1 >= MAX_SUMMARY_RETRIES:
                    store.mark_error(msg.msg_id, ctx.error)
                    logger.warning(
                        "⛔ %s:%s 连续失败 %d 次达到上限，标记永久错误（cron 将跳过）",
                        msg.platform,
                        msg.msg_id,
                        current_count + 1,
                    )
                else:
                    store.mark_retry_failure(msg.msg_id, ctx.error)
                    logger.info(
                        "↻ %s:%s 失败（第 %d/%d 次），将在下次 cron 重试",
                        msg.platform,
                        msg.msg_id,
                        current_count + 1,
                        MAX_SUMMARY_RETRIES,
                    )
                store.save()
                break

            msg.phase = next_phase
            store.mark_phase(msg.msg_id, next_phase)
            _flush_ctx_to_store(msg.msg_id, ctx, store, next_phase)
            # 成功推进：重置 retry_count（之前失败过的消息恢复后清状态）
            updated = store.get_message(msg.msg_id)
            if updated and updated.retry_count > 0:
                store.mark_retry_reset(msg.msg_id)
            logger.info("%s:%s → %s ✓", msg.platform, msg.msg_id, next_phase.name)
            store.save()

    @classmethod
    async def run_platform(
        cls,
        config: Config,
        platform: str,
        from_phase: Phase | None = None,
        log_callback: Callable[[str, str], None] | None = None,
        store: MessageStore | None = None,
    ) -> None:
        """统一平台入口：cleanup -> detect -> process。

        Detector 支持前缀匹配：``"bili"`` 会运行所有以 ``"bili"``
        开头的 detector key（如 ``"bili"`` 和 ``"bili_dynamic"``）。

        Args:
            config: 全局配置
            platform: 平台标识 ("bili" | "xhs" | "weibo")
            from_phase: 可选，将所有消息回退到指定阶段后重新处理
            log_callback: 可选，``(event_type, message)`` 回调，用于流式日志输出
            store: 可选，共享的 MessageStore 实例。并发执行多平台时由
                调用方传入同一实例以避免各实例内存快照互相覆盖。为 None
                时本方法会自行创建（单平台调用路径，保持向后兼容）。

        并发安全：在单线程 asyncio 事件循环中，MessageStore 的所有写
        方法均为纯同步（不含 await），不会让出事件循环，因此共享同一
        实例时对 ``_messages`` 的多步操作天然原子。
        """
        # 共享实例时跳过 cleanup（应由首次创建者执行一次），
        # 避免三个并发平台各自扫描同一份 _messages 造成重复工作
        owns_store = store is None
        if owns_store:
            store = MessageStore(config.general.data_dir)
            store.cleanup(24)
            if log_callback:
                log_callback("log", "🧹 已清理超过 24 小时的消息")

        if log_callback:
            log_callback("log", f"🔍 开始检查 {platform} 平台...")

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
        pending = list(store.get_messages(phase=Phase.PUSHED, exclude=True, platform=platform))
        if log_callback:
            log_callback("log", f"📋 {platform} 发现 {len(pending)} 条待处理消息")
        for msg in pending:
            if msg.error:
                # 跳过已有错误的消息，避免永久失败的消息无限重试
                logger.info("⏭ 跳过错误消息: %s (%s)", msg.title, msg.error)
                continue
            await cls.process_message(msg, config, store)

        if log_callback:
            log_callback("done", f"✅ {platform} 检查完成")

        store.save()

    @classmethod
    async def run_specific_messages(
        cls,
        msg_ids: list[str],
        from_phase: Phase,
        skip_push: bool,
        config: Config,
        store: MessageStore,
    ) -> None:
        """手动重跑指定消息的流水线（plan 2026-06-28-manual-content-check）。

        与 ``run_platform`` 的区别：
        - 不跑 detector（只对已存在的消息重跑）
        - 不调 cleanup（D6：避免误删超 24h 的历史消息）
        - 支持 skip_push 标志（D4：默认禁止重新推送）

        Args:
            msg_ids: 要重跑的消息 ID 列表
            from_phase: 起始阶段（reset 后从这里开始 process）
            skip_push: True 时 push handler 跳过通知
            config: 全局配置
            store: MessageStore 实例（共享调用方创建的）

        ⚠️ 并发安全：此方法不持有文件锁。避免与 cron ``run_check_once`` 同时运行，
        否则两个进程的 MessageStore 内存快照会互相覆盖（D10）。

        ⚠️ VIDEO + from_phase=summarized 行为：``process_message`` 的 Bug-3 修复
        会在 ``ctx.downloaded_filepath is None`` 时把 VIDEO 消息回退到 DISCOVERED。
        手动模式每次创建新 ctx，filepath 必为 None（跨进程不可恢复），所以
        ``--reset-phase summarized`` 对 VIDEO 消息实际会从 download 阶段重新跑全流水线。
        这是已知行为，不是 bug。
        """
        count = store.reset_specific(msg_ids, from_phase)
        if count == 0:
            logger.info("⏭ 无消息需要 reset（msg_ids=%s, target=%s）", msg_ids, from_phase.name)
            return

        logger.info("▶ 手动重跑 %d 条消息（from %s, skip_push=%s）", count, from_phase.name, skip_push)

        # 延迟导入所有平台 handler 模块（触发装饰器注册）
        for module_path in cls._HANDLER_MODULES.values():
            importlib.import_module(module_path)

        for msg_id in msg_ids:
            msg = store.get_message(msg_id)
            if msg is None:
                continue
            if msg.phase != from_phase:
                # reset_specific 跳过了某些（phase < target）
                continue
            # oracle Issue 3: content_type 与 from_phase 不兼容时跳过
            # （如 TEXT 消息 reset 到 transcribed，PHASE_FLOW[TEXT] 无 TRANSCRIBED）
            if from_phase not in PHASE_FLOW[msg.content_type]:
                logger.warning(
                    "⏭ 跳过 %s：%s 类型消息的阶段流不含 %s",
                    msg_id, msg.content_type.name, from_phase.name,
                )
                continue
            # 通过临时属性透传 skip_push 到 PhaseContext（避免污染 MessageRecord schema）
            setattr(msg, "_skip_push", skip_push)
            await cls.process_message(msg, config, store)

        store.save()
