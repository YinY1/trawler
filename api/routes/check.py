"""``/api/v1/check`` 路由 — bot 友好的检查触发与状态查询（T2）。

与 ``web/routes/check.py`` 的 ``/check`` 系列对称，但面向机器调用：
- 全程 JSON（request body / response），不走 form-encoded
- 鉴权走 ``Depends(require_token)``（``Authorization: Bearer``），不依赖 session
- 触发后台 run 后立即返回 ``task_id``（uuid4），客户端轮询 status / 订阅 SSE

**与 Web UI 共享互斥锁**：``state.check_running`` 与 Web UI 的 ``POST /check/run``
共用同一把锁，两边互斥（detector 并发跑两份会重复消息）。API 触发的 run 在
``state.api_task_id`` 上记录 task_id，供 409 / status 引用。

**复用关系**（不复制业务逻辑）：
- ``make_log_callback`` / ``build_sse_response`` 从 ``web.routes.check`` import
- ``parse_since`` 从 ``run_check`` import
- ``run_check_once`` / ``PipelineEngine.run_specific_messages`` 直接 await
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from api.auth import require_token
from api.schemas import CheckRunRequest, CheckRunResponse, CheckStatusResponse
from core.engine import PipelineEngine
from core.pipeline import run_check_once
from run_check import parse_since
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import Phase
from web.routes.check import build_sse_response, make_log_callback

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/check/run", response_model=CheckRunResponse, status_code=202)
async def check_run(
    body: CheckRunRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> CheckRunResponse:
    """触发一次检查（全量 or 手动），后台 ``asyncio.create_task`` 执行。

    - 与 Web UI ``POST /check/run`` 共享 ``state.check_running`` 单锁，互斥
    - 已有 run 在跑 → 409 ``{"status": "already_running", "task_id": ...}``
    - manual 模式必须携带筛选参数（since/title/author/reset_phase）任一，否则 422
    - 参数校验（非法 reset_phase / since）在启动后台 task 前同步返回 422
    """
    state = request.app.state
    if state.check_running:
        existing_task_id = getattr(state, "api_task_id", None)
        return JSONResponse(
            status_code=409,
            content={"status": "already_running", "task_id": existing_task_id},
        )

    is_manual = body.mode == "manual"

    # ── 同步预校验（避免启动后台 task 后才报错）──────────────────────
    target_phase: Phase | None = None
    since_ts: int | None = None
    if is_manual:
        # manual 模式必须有筛选参数（否则无意义：reset 全部消息重跑代价过大且非典型用法）
        has_filter = any([body.since, body.title, body.author, body.reset_phase])
        if not has_filter:
            raise HTTPException(
                status_code=422,
                detail="manual 模式必须提供 since/title/author/reset_phase 中至少一项",
            )
        reset_phase_str = body.reset_phase or "summarized"
        try:
            target_phase = Phase[reset_phase_str.upper()]
        except KeyError as exc:
            raise HTTPException(
                status_code=422, detail=f"未知的重跑阶段: {reset_phase_str}"
            ) from exc
        if body.since:
            try:
                since_ts = parse_since(body.since)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ── 占锁 + 初始化 run state ────────────────────────────────────
    task_id = uuid4().hex
    state.check_running = True
    state.check_processed_count = 0
    state.check_started_at = time.time()
    state.log_history.clear()
    state.api_task_id = task_id  # type: ignore[attr-defined]
    cb = make_log_callback(state)

    async def _run() -> None:
        try:
            config = await load_config()
            if is_manual:
                # manual 模式：parse 参数 → query → run_specific_messages
                assert target_phase is not None  # noqa: S101 — 上面预校验已设值
                platform_filter = None if body.platform in (None, "all", "") else body.platform
                store = MessageStore(config.general.data_dir)
                matched = store.query_messages(
                    since=since_ts,
                    title=body.title,
                    author=body.author,
                    platform=platform_filter,
                )
                if not matched:
                    cb("log", "⚠️ 没有匹配的消息")
                    cb("done", "✅ 手动重跑完成（无匹配）")
                    return
                cb("log", f"📋 匹配 {len(matched)} 条消息，从 {target_phase.name} 重跑")
                msg_ids = [m.msg_id for m in matched]
                await PipelineEngine.run_specific_messages(
                    msg_ids=msg_ids,
                    from_phase=target_phase,
                    skip_push=body.skip_push,
                    config=config,
                    store=store,
                    log_callback=cb,
                )
            else:
                # 全量模式：platform 作为 run_check_once 的过滤器
                await run_check_once(
                    config, platform=body.platform or "all", log_callback=cb
                )
        except Exception as exc:
            err_item: dict[str, object] = {
                "type": "error",
                "message": f"检查失败: {exc}",
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
            # Clear started_at so /check/status 不会把已完成的 run 当成 in-progress。
            # log_history 保留，客户端仍能看到刚跑完的日志。
            state.check_started_at = None
            state.check_task = None
            state.api_task_id = None  # type: ignore[attr-defined]
            # 广播 EOF 到每个 subscriber（每个 generator yield done 后干净退出）
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_run())
    return CheckRunResponse(
        status="started", task_id=task_id, mode="manual" if is_manual else "full"
    )


@router.get("/check/status", response_model=CheckStatusResponse)
async def check_status(
    request: Request,
    _token_name: str = Depends(require_token),
) -> CheckStatusResponse:
    """当前 run 的状态快照。

    ``log_history`` 的 ``_ts`` 内部字段在序列化前 strip 掉（与 SSE replay
    payload 对齐）。无 run 在跑时返回 ``running=False`` + ``started_at=None``。
    """
    state = request.app.state
    clean_history = [
        {k: v for k, v in item.items() if k != "_ts"} for item in state.log_history
    ]
    return CheckStatusResponse(
        running=bool(state.check_running),
        processed_count=int(state.check_processed_count),
        started_at=state.check_started_at,
        log_history=clean_history,
    )


@router.get("/check/stream")
async def check_stream(
    request: Request,
    _token_name: str = Depends(require_token),
) -> StreamingResponse:
    """SSE 日志流，与 Web UI ``GET /check/stream`` 同源（复用 ``state.subscribers``）。

    鉴权走 ``Authorization: Bearer`` header（``Depends(require_token)``），
    无 token → 401 JSON，不是 SSE 流。bot 触发 run 时浏览器开着 Web UI 也会看到
    同一份 SSE 流（特性，非 bug）。
    """
    return build_sse_response(request.app.state)
