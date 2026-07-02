"""单消息操作路由（Web UI）。

目前仅提供永久失败消息的重试入口::

    POST /messages/{msg_id}/retry

背景：PR #45 引入 ``permanent_error`` 机制后，``error != ""`` 的消息会被
cron 永久跳过（``engine.py`` 内 ``if msg.error: continue``）。本路由给
用户提供 Web 入口，让 ``MessageStore.reset_specific`` 把消息回退到当前
失败阶段（清零 ``error`` / ``retry_count`` / ``last_error``），让 cron
下次循环重新处理该阶段。

**target phase 决策**：reset 到 ``msg.phase``（当前失败阶段）而非更早的
``DISCOVERED``，避免重做已成功的下载/转写阶段。
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.engine import PipelineEngine
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import Phase
from web.routes.check import make_log_callback

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/messages/{msg_id}/retry", response_class=HTMLResponse)
async def retry_message(msg_id: str, request: Request) -> HTMLResponse:
    """重置单条消息到当前阶段，让 cron 重新处理。

    - URL 中的 ``msg_id`` 经 FastAPI 自动 decode（``bili:BV1xx`` 这种含冒号
      的 ID 会被客户端编码为 ``bili%3ABV1xx``）。
    - 调用 :meth:`MessageStore.reset_specific` 清零 ``error`` / ``retry_count``
      / ``last_error``，phase 回退到当前阶段（原地重试，不重做更早阶段）。
    - 成功：返回一个 span（绿色）替换按钮区，附带 HX-Trigger toast。
    - 失败：返回一个 span（红色，``role="alert"``）替换按钮区，附带 HX-Trigger toast。

    Args:
        msg_id: URL-decoded 后的消息 ID，如 ``bili:BV1xx``。
        request: FastAPI Request（用于取 data_dir 配置）。

    Raises:
        HTTPException 404: msg_id 在 store 中不存在。
        HTTPException 400: reset_specific 返回 0（phase 已是最早或 ID 异常）。
    """
    config = await load_config()
    store = MessageStore(config.general.data_dir)

    # 查找消息记录，确定 target phase（= 当前 phase）
    all_msgs = store.get_messages_in_window()
    target_msg = next((m for m in all_msgs if m.msg_id == msg_id), None)
    if target_msg is None:
        logger.warning("⚠️ 重试失败: msg_id 不存在: %s", msg_id)
        raise HTTPException(status_code=404, detail="消息不存在")

    target_phase: Phase = target_msg.phase
    count = store.reset_specific([msg_id], target=target_phase)

    if count == 0:
        # phase 已早于 target（理论上不该发生，因为 target = current），但兜底
        logger.warning("⚠️ 重试失败: reset_specific 返回 0, msg_id=%s", msg_id)
        # 返回错误 span（HTMX 外层替换），toast 由 HX-Trigger 发出（key 走 latin-1 安全路径）
        return HTMLResponse(
            content=(
                '<span role="alert" class="inline-flex items-center gap-1.5 text-xs '
                'text-red-500 font-medium">重试失败，请刷新页面重试</span>'
            ),
            headers={
                "HX-Trigger": '{"toast": {"key": "message.retry_failed", "type": "error"}}',
            },
            status_code=200,  # HTMX 不能 swap 4xx body，用 200 + 错误 UI
        )

    logger.info("✓ 消息重试已重置: %s → phase=%s", msg_id, target_phase.name)
    # 成功：返回一个确认 span 替换按钮区。Cron 下次循环会拾起此消息。
    return HTMLResponse(
        content=(
            '<span class="inline-flex items-center gap-1.5 text-xs '
            'text-[var(--color-primary)] font-medium">'
            '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<polyline points="20 6 9 17 4 12"/></svg>'
            "已重置，等待 cron 处理</span>"
        ),
        headers={
            "HX-Trigger": '{"toast": {"key": "message.retry_success", "type": "success"}}',
        },
    )


@router.post("/messages/batch-reprocess")
async def batch_reprocess(request: Request) -> dict[str, str]:
    """批量重跑选中消息（issue #71 批量多选入口）。

    接收表单参数：
    - msg_ids: 逗号分隔的消息 ID 列表（如 "bili:BV1,xhs:N1"）
    - reset_phase: 可选，重跑起始阶段（缺省 SUMMARIZED）
    - skip_push: 可选，"on"/"true"/"1" 时禁止重新推送（默认 False，允许重推）。
      前端 checkbox 默认勾选发送 "on" 与 CLI --skip-push 默认 True 行为一致

    异步触发：调 PipelineEngine.run_specific_messages（background task），
    立即返回 {"status": "started"}。前端通过轮询 /check/status 或 SSE 监听进度。

    与 /check/run 手动模式共用 state.check_running 单锁互斥。

    Raises:
        HTTPException 400: msg_ids 为空。
        HTTPException 409: 已有检查在运行（state.check_running=True）。
    """
    state = request.app.state
    # 共享单锁：与 /check/run 互斥
    if state.check_running:
        raise HTTPException(status_code=409, detail="已有检查任务在运行")

    form = await request.form()
    msg_ids_raw = (form.get("msg_ids") or "").strip()
    if not msg_ids_raw:
        raise HTTPException(status_code=400, detail="msg_ids 不能为空")

    msg_ids = [mid.strip() for mid in msg_ids_raw.split(",") if mid.strip()]
    if not msg_ids:
        raise HTTPException(status_code=400, detail="msg_ids 不能为空")

    reset_phase_str = (form.get("reset_phase") or "").strip()
    if reset_phase_str:
        try:
            target_phase = Phase[reset_phase_str.upper()]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"未知的重跑阶段: {reset_phase_str}") from None
    else:
        target_phase = Phase.SUMMARIZED
    # 统一 skip_push 解析（与 /check/run 一致）：default False（允许重推），
    # 前端 checkbox 默认勾选发送 "on" 与 CLI --skip-push 默认 True 行为一致
    skip_push = form.get("skip_push") in ("on", "true", "1")

    # 占锁 + reset 状态
    state.check_running = True
    state.check_processed_count = 0
    state.check_started_at = time.time()
    state.log_history.clear()

    cb = make_log_callback(state)

    async def _run() -> None:
        try:
            config = await load_config()
            store = MessageStore(config.general.data_dir)
            await PipelineEngine.run_specific_messages(
                msg_ids=msg_ids,
                from_phase=target_phase,
                skip_push=skip_push,
                config=config,
                store=store,
                log_callback=cb,
            )
        except Exception as exc:
            err_item = {
                "type": "error",
                "message": f"批量重跑失败: {exc}",
                "time": time.strftime("%H:%M:%S"),
                "_ts": time.time(),
            }
            state.log_history.append(err_item)
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(err_item)
                except asyncio.QueueFull:
                    pass
        finally:
            state.check_running = False
            state.check_started_at = None
            state.check_task = None
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_run())
    return {"status": "started"}
