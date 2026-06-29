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

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import Phase

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
