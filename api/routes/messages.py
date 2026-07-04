"""``/api/v1/messages*`` 路由 — 消息查询与批量重跑（T3）。

与 ``web/routes/messages.py`` 的 ``/messages/batch-reprocess`` 对称，但面向
机器调用：JSON in / out，token 鉴权。

**与 /check/run 共享互斥锁**：``POST /messages/rerun`` 走 ``state.check_running``
单锁（与 Web UI ``POST /messages/batch-reprocess``、Web UI/API ``POST /check/run``
四方互斥），防止 detector 与 rerun 并发写同一 store（重复消息、覆盖快照）。

**复用关系**（不复制业务逻辑）：
- ``parse_since`` 从 ``run_check`` import
- ``MessageStore.query_messages`` / ``get_message`` 直接调用
- ``PipelineEngine.run_specific_messages`` 直接 await
- ``make_log_callback`` 从 ``web.routes.check`` import（rerun 接入 SSE，与
  /check/run 行为一致）
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from api.auth import require_token
from api.schemas import (
    MessageListResponse,
    MessageOut,
    RerunRequest,
    RerunResponse,
)
from core.engine import PipelineEngine
from run_check import parse_since
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import MessageRecord, Phase
from web.routes.check import make_log_callback

logger = logging.getLogger(__name__)
router = APIRouter()


def _record_to_out(rec: MessageRecord) -> MessageOut:
    """``MessageRecord`` → ``MessageOut``（处理枚举字段序列化）。

    ``MessageRecord.content_type`` 和 ``.phase`` 都是 Enum（见
    ``shared/message_store.py:_msg_from_dict:89-108``，用
    ``ContentType(...)`` 和 ``Phase(...)`` 构造），``.name`` 安全。
    """
    return MessageOut(
        msg_id=rec.msg_id,
        platform=rec.platform,
        content_type=rec.content_type.name,
        phase=rec.phase.name,
        pubdate=rec.pubdate,
        title=rec.title,
        author=rec.author,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
        error=rec.error,
        dynamic_text=rec.dynamic_text,
        subscription_ref=rec.subscription_ref,
        xsec_token=rec.xsec_token,
        body=rec.body,
        summary=rec.summary,
        retry_count=rec.retry_count,
        last_error=rec.last_error,
        permanent_error=rec.permanent_error,
    )


def _parse_since_or_int(value: str) -> int:
    """``since`` 支持 unix 时间戳（纯数字）或 ``parse_since`` 字符串格式。

    优先尝试 int 解析（``"1700000000"`` 直接当 ts）；失败回退 ``parse_since``
    （``"24h"`` / ``"7d"`` / ``"2026-06-01"``）。两者都失败抛 ``ValueError``，
    由调用方转 422。
    """
    try:
        return int(value)
    except ValueError:
        return parse_since(value)


# ═══════════════════════════════════════════════════════════
# GET /messages
# ═══════════════════════════════════════════════════════════


@router.get("/messages", response_model=MessageListResponse)
async def list_messages(
    request: Request,
    since: str | None = Query(None),
    title: str | None = Query(None),
    author: str | None = Query(None),
    platform: str | None = Query(None),
    phase: str | None = Query(None),
    _token_name: str = Depends(require_token),
) -> MessageListResponse:
    """多维度筛选消息。

    所有过滤参数 AND 组合，缺省不过滤。``since`` 接受 unix 时间戳或相对/绝对
    时间字符串（``parse_since`` 解析）。``phase`` 按枚举名匹配（大小写不敏感，
    内部 ``Phase[phase.upper()]``）。
    """
    phase_enum: Phase | None = None
    if phase:
        try:
            phase_enum = Phase[phase.upper()]
        except KeyError as exc:
            raise HTTPException(
                status_code=422, detail=f"未知 phase: {phase}"
            ) from exc

    since_ts: int | None = None
    if since is not None:
        try:
            since_ts = _parse_since_or_int(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"无法解析 since 值: {since!r}（支持格式: 24h / 7d / 30m / 2026-06-01 / unix 时间戳）",
            ) from exc

    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)
    matched = store.query_messages(
        since=since_ts, title=title, author=author, platform=platform, phase=phase_enum
    )
    return MessageListResponse(
        messages=[_record_to_out(m) for m in matched],
        count=len(matched),
    )


# ═══════════════════════════════════════════════════════════
# GET /messages/{msg_id}
# ═══════════════════════════════════════════════════════════


@router.get("/messages/{msg_id}", response_model=MessageOut)
async def get_message(
    msg_id: str,
    request: Request,
    _token_name: str = Depends(require_token),
) -> MessageOut:
    """单条消息详情。不存在 → 404 ``{"detail": "message not found"}``。"""
    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)
    rec = store.get_message(msg_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="message not found")
    return _record_to_out(rec)


# ═══════════════════════════════════════════════════════════
# POST /messages/rerun
# ═══════════════════════════════════════════════════════════


@router.post("/messages/rerun", response_model=RerunResponse, status_code=202)
async def rerun_messages(
    body: RerunRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> RerunResponse | JSONResponse:
    """批量重跑指定消息。

    走 ``state.check_running`` 单锁（与 ``/check/run`` 及 Web UI
    ``/messages/batch-reprocess`` 完全对称、互斥），防止 detector 与 rerun
    并发写同一 store。详见 spec §关键不变量 与 plan T3 决策点。

    - ``msg_ids`` 空 → 422
    - ``from_phase`` 非法 → 422
    - 已有 run 在跑 → 409 ``{"status": "already_running", "task_id": ...}``（扁平 shape，
      ``JSONResponse`` 绕开 FastAPI 的 ``{"detail": ...}`` 包装）
    - 全部 msg_id 不存在 → 404
    - 成功 → 202 + ``{"status": "started", "task_id": ..., "reset_count": ...}``
    """
    # min_items 校验（pydantic v2 用 Annotated[list[str], Field(min_length=1)]
    # 但跨版本兼容性差，直接路由层显式校验更可靠）
    if not body.msg_ids:
        raise HTTPException(
            status_code=422, detail="msg_ids 不能为空"
        )

    try:
        target_phase = Phase[body.from_phase.upper()]
    except KeyError as exc:
        raise HTTPException(
            status_code=422, detail=f"未知 phase: {body.from_phase}"
        ) from exc

    state = request.app.state
    # 占锁前检查（与 /check/run 对称，409 走 JSONResponse 扁平 shape）
    if state.check_running:
        existing_task_id = getattr(state, "api_task_id", None)
        return JSONResponse(
            status_code=409,
            content={"status": "already_running", "task_id": existing_task_id},
        )

    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)
    # reset 数量：占锁前先查存在的消息数（reset_specific 在后台 task 内调）
    existing = [m for m in (store.get_message(mid) for mid in body.msg_ids) if m is not None]
    reset_count = len(existing)
    if reset_count == 0:
        raise HTTPException(status_code=404, detail="message not found")

    # ── 占锁 + 初始化 run state（与 /check/run 完全对称）──────────────
    task_id = uuid4().hex
    state.check_running = True
    state.check_processed_count = 0
    state.check_started_at = time.time()
    state.log_history.clear()
    state.api_task_id = task_id  # type: ignore[attr-defined]
    cb = make_log_callback(state)

    async def _rerun() -> None:
        try:
            await PipelineEngine.run_specific_messages(
                msg_ids=body.msg_ids,
                from_phase=target_phase,
                skip_push=body.skip_push,
                config=cfg,
                store=store,
                log_callback=cb,
            )
        except Exception as exc:
            err_item: dict[str, object] = {
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
            # 与 /check/run 对称的清理（finally 保证锁释放）
            state.check_running = False
            state.check_started_at = None
            state.check_task = None
            state.api_task_id = None  # type: ignore[attr-defined]
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_rerun())
    return RerunResponse(
        status="started", task_id=task_id, reset_count=reset_count
    )
