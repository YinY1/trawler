from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from core.engine import PipelineEngine
from core.pipeline import run_check_once
from run_check import parse_since
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import Phase
from web.app import TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter()

# Cap for in-memory log history replayed to reconnecting clients.
LOG_HISTORY_CAP = 500


def make_log_callback(state: Any) -> Callable[[str, str], None]:
    """构造 SSE 日志广播 callback（issue #71，check_run 与 batch_reprocess 共用）。

    将日志事件追加到 state.log_history（有界），并 fan-out 到所有 SSE 订阅者。
    """

    def _cb(event_type: str, message: str) -> None:
        now = time.time()
        item = {"type": event_type, "message": message, "time": time.strftime("%H:%M:%S"), "_ts": now}
        state.log_history.append(item)
        if len(state.log_history) > LOG_HISTORY_CAP:
            del state.log_history[: len(state.log_history) - LOG_HISTORY_CAP]
        state.check_processed_count += 1
        for sub in list(state.subscribers):
            try:
                sub.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full, dropping event")

    return _cb


@router.get("/check", response_class=HTMLResponse)
async def check_page(request: Request) -> HTMLResponse:
    """Check page — trigger button + log output."""
    state = request.app.state
    return TEMPLATES.TemplateResponse(
        request,
        "check.html",
        {
            "active_nav": "check",
            "running": state.check_running,
            "processed_count": state.check_processed_count,
            "started_at": state.check_started_at,
            "log_history": list(state.log_history),
        },
    )


@router.get("/check/status")
async def check_status(request: Request) -> dict[str, object]:
    """Live status snapshot — used by client to reconnect after navigation."""
    state = request.app.state
    return {
        "running": state.check_running,
        "processed_count": state.check_processed_count,
        "started_at": state.check_started_at,
        "log_history": list(state.log_history),
    }


@router.post("/check/run")
async def check_run(request: Request) -> dict[str, str]:
    """Trigger a check run in the background.

    支持两种模式（issue #71）：
    - 全量模式（无筛选参数）：走 run_check_once（原行为，跑 detector + cleanup）
    - 手动模式（带 since/title/author/reset_phase 任一）：
      走 PipelineEngine.run_specific_messages（不跑 detector，不调 cleanup）

    两种模式共用 state.check_running 单锁互斥。
    """
    state = request.app.state
    if state.check_running:
        return {"status": "already_running"}

    # 解析表单参数（全量模式不传任何字段）
    form = await request.form()
    since = (form.get("since") or "").strip() or None
    title = (form.get("title") or "").strip() or None
    author = (form.get("author") or "").strip() or None
    platform = (form.get("platform") or "").strip() or None
    reset_phase_str = (form.get("reset_phase") or "").strip() or None
    skip_push = form.get("skip_push") in ("on", "true", "1")  # default False（允许重推）

    # 判定模式：有 since/title/author/reset_phase 任一筛选参数则进手动模式
    # （platform 不参与判定，仅作为过滤器；CLI `trawler check --platform bili` 走全量检测）
    is_manual = any([since, title, author, reset_phase_str])

    # 同步预校验手动模式参数：非法 phase/since 立即返回 status=error，
    # 避免启动 background task 后才在日志里报错（用户看到伪装的 "started"）。
    if is_manual:
        if reset_phase_str:
            try:
                Phase[reset_phase_str.upper()]
            except KeyError:
                return {"status": "error", "message": f"未知的重跑阶段: {reset_phase_str}"}
        if since:
            try:
                parse_since(since)
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}

    # Fresh run: reset state
    state.check_running = True
    state.check_processed_count = 0
    state.check_started_at = time.time()
    state.log_history.clear()

    cb = make_log_callback(state)

    async def _run() -> None:
        try:
            config = await load_config()
            if is_manual:
                # 手动模式：parse 参数 → query → run_specific_messages
                if reset_phase_str:
                    target_phase = Phase[reset_phase_str.upper()]
                else:
                    target_phase = Phase.SUMMARIZED  # 默认重跑摘要阶段（与 CLI 默认一致）

                # since 解析（复用 CLI 的 parse_since，支持 24h/7d/2026-06-01 等格式）
                since_ts = parse_since(since) if since else None
                platform_filter = None if platform in (None, "all", "") else platform

                store = MessageStore(config.general.data_dir)
                matched = store.query_messages(
                    since=since_ts,
                    title=title,
                    author=author,
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
                    skip_push=skip_push,
                    config=config,
                    store=store,
                    log_callback=cb,
                )
            else:
                # 全量模式：原行为（platform 作为 run_check_once 的过滤器，前端可选平台）
                await run_check_once(config, platform=platform or "all", log_callback=cb)
        except Exception as exc:
            err_item = {
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
            # Clear started_at so a later /check/status snapshot (e.g. page
            # reload, "再次运行" reconnect) does not mistake a finished run
            # for an in-progress one and wrongly restart the elapsed timer.
            # log_history is intentionally kept so the client can still show
            # the just-finished run's logs.
            state.check_started_at = None
            state.check_task = None
            # Signal EOF to every active subscriber (broadcast). Each
            # subscriber's generator yields a `done` event and exits cleanly.
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_run())
    return {"status": "started", "mode": "manual" if is_manual else "full"}


@router.get("/check/stream")
async def check_stream(request: Request) -> StreamingResponse:
    """SSE endpoint: stream log events to the browser.

    Each connection owns a private asyncio.Queue registered in
    ``state.subscribers``; ``_log_callback`` broadcasts every event to all
    subscribers. This avoids the single-consumer race where two concurrent
    SSE connections would split events between them and one would hang on
    EOF.

    Connect-time replay: snapshot ``log_history`` at entry, yield it as
    ``log`` events, then continue with the live queue. The final ``None``
    sentinel is translated into a ``done`` event and the connection closes.
    """
    state = request.app.state
    # Per-connection queue; bounded so a slow client cannot grow memory.
    sub_queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(maxsize=2000)
    state.subscribers.append(sub_queue)
    # Snapshot history at connect time, but ONLY items from the current run.
    # If a new run just cleared history but the SSE connect races ahead of
    # the POST response, we could snapshot stale items from the PREVIOUS run.
    # Filtering by check_started_at timestamp ensures we only replay what
    # belongs to this run. time field is "%H:%M:%S" so we compare by
    # sequence: items appended after check_started_at belong to this run.
    current_run_started = state.check_started_at
    history_snapshot = [
        item for item in state.log_history if not current_run_started or item.get("_ts", 0) >= current_run_started
    ]

    async def event_generator() -> AsyncIterator[bytes]:
        try:
            # 1) Replay history captured at connect time (current run only)
            for item in history_snapshot:
                clean = {k: v for k, v in item.items() if k != "_ts"}
                data = json.dumps(clean, ensure_ascii=False)
                yield f"event: log\ndata: {data}\n\n".encode("utf-8")
            # 2) If a finished run's history was already present at connect
            #    time, emit done immediately so the client UI settles. We key
            #    off history_snapshot rather than check_running to avoid a
            #    race: a client that connects in the window between SSE open
            #    and POST /check/run would otherwise be told "done" before the
            #    run even starts. An empty history at connect time means no
            #    run has produced output yet — fall through to the live loop.
            if not state.check_running and history_snapshot:
                yield b"event: done\ndata: \n\n"
                return
            # 3) Stream live events from our private queue
            while True:
                try:
                    item = await asyncio.wait_for(sub_queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"  # SSE keepalive comment
                    continue
                if item is None:
                    yield b"event: done\ndata: \n\n"
                    return
                # Strip internal _ts (used only for history filtering) so the
                # browser-facing payload stays clean — matches history replay.
                clean = {k: v for k, v in item.items() if k != "_ts"}
                data = json.dumps(clean, ensure_ascii=False)
                yield f"event: log\ndata: {data}\n\n".encode("utf-8")
        finally:
            # Always deregister so producer never queues into a dead queue.
            try:
                state.subscribers.remove(sub_queue)
            except ValueError:
                pass

    return StreamingResponse(event_generator(), media_type="text/event-stream")
