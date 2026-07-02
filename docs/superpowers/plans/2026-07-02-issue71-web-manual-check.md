# Web UI 同步 CLI 按时间范围处理历史消息 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Web UI `/check` 页面支持「按时间范围/标题/作者/平台筛选并重跑历史消息」（CLI 已有 `--since/--title/--author/--reset-phase/--skip-push` 功能的 Web 端对偶），并在 Dashboard 消息列表支持批量多选重跑。

**Architecture:** 复用现有 `POST /check/run` 端点扩展可选筛选参数——有筛选参数时走 `PipelineEngine.run_specific_messages`（手动模式），无参数时走原 `run_check_once`（全量模式）。两种模式共用 `state.check_running` 单锁互斥。手动模式不跑 detector、不调 cleanup（保留超 24h 历史消息）。SSE 日志流复用现有 `_log_callback` 广播机制，需给 `run_specific_messages` 新增 `log_callback` 参数（与 `run_platform` 签名对齐）。批量多选走新端点 `POST /messages/batch-reprocess`。

**Tech Stack:** Python 3.14 / FastAPI / Jinja2 / HTMX / SSE / asyncio / pytest + httpx AsyncClient + ASGITransport

---

## 背景

CLI 端已实现手动检查功能（plan `2026-06-28-manual-content-check.md`），用户可执行 `trawler check --since 7d --title xxx --reset-phase summarized --skip-push` 按条件筛选历史消息重跑流水线。本 issue 把同样的能力暴露到 Web UI，并提供批量多选入口。

## 关键决策（用户已确认，不可更改）

| ID | 决策 | 说明 |
|----|------|------|
| **A1** | 实现路径 A | 复用 `POST /check/run`，接受可选筛选参数（since/title/author/platform/reset_phase/skip_push）。有 since/title/author/reset_phase 筛选参数 → `run_specific_messages`；无参数 → `run_check_once`（原行为不变，platform 仅作为全量模式的平台过滤器）。 |
| **A2** | 批量多选 UI | Dashboard 消息列表加 checkbox 多选 + 「重跑选中 N 条」按钮 → 调 `POST /messages/batch-reprocess`，该端点复用 `run_specific_messages`。 |
| **A3** | 共享单锁 | 手动模式和全量模式共用 `state.check_running` flag，互斥（手动模式跑时全量按钮禁用，反之亦然）。 |
| **A4** | 手动模式隐藏平台卡片 | 手动模式（有筛选参数）时前端隐藏/禁用平台进度卡片，只显示日志流（因手动模式不跑 detector，平台卡片正则匹配不到「开始检查 X 平台」事件，卡片会永远空白，隐藏更清晰）。 |

## 已知约束（来自项目 AGENTS.md + 代码现状）

1. **`run_specific_messages` 当前无 `log_callback` 参数**（`core/engine.py:331-338`），必须新增才能接 SSE。
2. **`parse_since()` 在 `run_check.py:33-55`**，Web 层需 import 复用（避免重复实现）。
3. **CSRF**：所有 POST 需 `X-Requested-With: XMLHttpRequest` 头（HTMX 头），见 `web/app.py:172-185` + `web/auth.py:123`。
4. **SSE 测试模式**：需手动驱动 `app.state.subscribers` queue（见 `tests/test_web_check.py:60-75` 的 producer pattern）。
5. **单 worker 约束**：手动模式 + SSE 必须同 worker 进程内完成（`state.check_running` 是进程内 flag，不跨 worker）。
6. **SSE 协议**：`event: log\ndata: {json}\n\n` / `event: done\ndata: \n\n` / `: heartbeat\n\n`。
7. **`from __future__ import annotations`** 在所有模块文件顶部。
8. **不改 emoji/颜色/error message 文本**（外部接口约定）。

## 文件清单

### 后端修改

| 文件 | 改动 | 说明 |
|------|------|------|
| `core/engine.py` | 改 `run_specific_messages` 签名 | 加 `log_callback` 参数（与 `run_platform:257` 对齐），在 reset/process 前后发日志事件 |
| `web/routes/check.py` | 改 `check_run` | 解析可选表单参数，有筛选 → 走 `run_specific_messages`，无 → 走原 `run_check_once` |
| `web/routes/messages.py` | 加新端点 | `POST /messages/batch-reprocess`（批量多选重跑，调 `run_specific_messages`） |

### 前端修改

| 文件 | 改动 | 说明 |
|------|------|------|
| `web/templates/check.html` | 加筛选表单 + JS | 时间下拉(24h/7d/30d) + title/author 输入 + reset_phase 下拉 + skip_push 复选框；手动模式隐藏平台卡片 |
| `web/templates/dashboard.html` | 加 checkbox 多选 + 批量按钮 | 消息列表每行 checkbox + 表头全选 + 「重跑选中 N 条」按钮（HTMX 调 batch-reprocess） |

### 测试修改

| 文件 | 改动 | 说明 |
|------|------|------|
| `tests/test_manual_check.py` | 扩展 | `run_specific_messages` 的 `log_callback` 参数单测（engine 层） |
| `tests/test_web_check.py` | 扩展 | `/check/run` 带筛选参数的 API 测试（含 SSE 推送验证）+ 手动模式隐藏平台卡片的前端标记 |
| `tests/test_web_messages.py` | 扩展 | `POST /messages/batch-reprocess` 端点测试 |

---

## 任务分解（TDD）

### Task 1: 给 `run_specific_messages` 加 `log_callback` 参数（engine 层）

**Files:**
- Modify: `core/engine.py:331-392`（`run_specific_messages` 方法）
- Test: `tests/test_manual_check.py`（扩展）

- [ ] **Step 1: 写失败测试 — `log_callback` 被调用且事件类型正确**

在 `tests/test_manual_check.py` 末尾追加（在已有的 `test_run_specific_messages_*` 测试块之后）：

```python
# ── run_specific_messages log_callback 参数 (issue #71) ──────────


async def test_run_specific_messages_invokes_log_callback(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """run_specific_messages 应在 reset 前后通过 log_callback 发日志事件。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    events: list[tuple[str, str]] = []

    def cb(event_type: str, message: str) -> None:
        events.append((event_type, message))

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()):
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
            log_callback=cb,
        )
    # 至少触发了 log 事件（reset 开始 / 每条消息 / 完成）
    assert len(events) > 0
    # 所有事件类型应为 "log" 或 "done"
    assert all(et in ("log", "done") for et, _ in events)
    # 完成事件应包含 done 类型
    assert any(et == "done" for et, _ in events)


async def test_run_specific_messages_log_callback_none_default(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """log_callback=None（默认）应不报错（向后兼容现有 CLI 调用）。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()):
        # 不传 log_callback，应正常完成（默认 None）
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
        )


async def test_run_specific_messages_empty_list_with_callback(
    mock_store: MessageStore, tmp_path: Path
) -> None:
    """空 msg_ids 且有 callback：reset_specific 返回 0 时早退，仍发 done 事件。"""
    config = MagicMock()
    config.general.data_dir = str(tmp_path)

    events: list[tuple[str, str]] = []
    cb = lambda et, m: events.append((et, m))  # noqa: E731

    with patch.object(PipelineEngine, "process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_specific_messages(
            msg_ids=[],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
            log_callback=cb,
        )
        assert not mock_proc.called
    # 空列表也应发 done（让前端 SSE 能收到结束信号）
    assert any(et == "done" for et, _ in events)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_manual_check.py::test_run_specific_messages_invokes_log_callback tests/test_manual_check.py::test_run_specific_messages_empty_list_with_callback -v`
Expected: FAIL — `TypeError: run_specific_messages() got an unexpected keyword argument 'log_callback'`

- [ ] **Step 3: 实现 — 修改 `run_specific_messages` 签名 + 加 log_callback 调用**

修改 `core/engine.py:330-392`。把整个方法替换为以下内容（保留原有 docstring，在签名加参数，在 reset/process 前后插 log_callback）：

```python
    @classmethod
    async def run_specific_messages(
        cls,
        msg_ids: list[str],
        from_phase: Phase,
        skip_push: bool,
        config: Config,
        store: MessageStore,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """手动重跑指定消息的流水线（plan 2026-06-28-manual-content-check）。

        与 ``run_platform`` 的区别：
        - 不跑 detector（只对已存在的消息重跑）
        - 不调 cleanup（D6：避免误删超 24h 的历史消息）
        - 支持 skip_push 标志（D4：默认禁止重新推送）
        - 支持 log_callback（issue #71：Web UI SSE 接入，与 run_platform 签名对齐）

        Args:
            msg_ids: 要重跑的消息 ID 列表
            from_phase: 起始阶段（reset 后从这里开始 process）
            skip_push: True 时 push handler 跳过通知
            config: 全局配置
            store: MessageStore 实例（共享调用方创建的）
            log_callback: 可选，``(event_type, message)`` 回调，用于流式日志输出。
                事件类型仅 "log" / "done"。无 callback 时静默（CLI 默认）。

        ⚠️ 并发安全：此方法不持有文件锁。避免与 cron ``run_check_once`` 同时运行，
        否则两个进程的 MessageStore 内存快照会互相覆盖（D10）。

        ⚠️ VIDEO + from_phase=summarized 行为：``process_message`` 的 Bug-3 修复
        会在 ``ctx.downloaded_filepath is None`` 时把 VIDEO 消息回退到 DISCOVERED。
        手动模式每次创建新 ctx，filepath 必为 None（跨进程不可恢复），所以
        ``--reset-phase summarized`` 对 VIDEO 消息实际会从 download 阶段重新跑全流水线。
        这是已知行为，不是 bug。
        """
        if log_callback:
            log_callback("log", f"▶ 手动重跑 {len(msg_ids)} 条消息（from {from_phase.name}, skip_push={skip_push}）")

        count = store.reset_specific(msg_ids, from_phase)
        if count == 0:
            logger.info("⏭ 无消息需要 reset（msg_ids=%s, target=%s）", msg_ids, from_phase.name)
            if log_callback:
                log_callback("log", "⚠️ 没有消息需要重跑")
                log_callback("done", "✅ 手动重跑完成（无操作）")
            return

        if log_callback:
            log_callback("log", f"📋 实际 reset {count} 条消息")

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
                if log_callback:
                    log_callback("log", f"⏭ 跳过 {msg.title[:30]}：类型 {msg.content_type.name} 不支持 {from_phase.name}")
                continue
            if log_callback:
                log_callback("log", f"▶ 处理 {msg.platform}:{msg.msg_id} ({msg.title})")
            # 通过临时属性透传 skip_push 到 PhaseContext（避免污染 MessageRecord schema）
            setattr(msg, "_skip_push", skip_push)
            await cls._safe_process_message(msg, config, store)

        store.save()
        if log_callback:
            log_callback("done", "✅ 手动重跑完成")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_manual_check.py -v`
Expected: PASS（含新加的 3 个 log_callback 测试 + 原有 6 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add core/engine.py tests/test_manual_check.py
git commit -m "feat(engine): add log_callback to run_specific_messages for Web SSE (issue #71)"
```

---

### Task 2: 扩展 `POST /check/run` 接受可选筛选参数

**Files:**
- Modify: `web/routes/check.py:52-122`（`check_run` 方法）
- Test: `tests/test_web_check.py`（扩展）

- [ ] **Step 1: 写失败测试 — 带筛选参数走 run_specific_messages**

在 `tests/test_web_check.py` 的 `TestCheck` 类末尾追加：

```python
    @patch("web.routes.check.PipelineEngine")
    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_with_filter_calls_run_specific_messages(
        self, mock_load, mock_run_once, mock_engine, client: AsyncClient
    ) -> None:
        """带 since/title 等筛选参数时走 run_specific_messages，不走 run_check_once。"""
        mock_load.return_value.general.data_dir = "/tmp"
        # 让 run_specific_messages 是 AsyncMock，避免真实执行
        mock_engine.run_specific_messages = AsyncMock()

        resp = await client.post(
            "/check/run",
            headers=HTMX_HEADERS,
            data={"since": "7d", "title": "测试", "reset_phase": "summarized", "skip_push": "on"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        # run_specific_messages 被调用（可能尚未 await 完成因是 background task，等待一下）
        await asyncio.sleep(0.05)
        mock_engine.run_specific_messages.assert_called_once()
        # run_check_once 不应被调用
        mock_run_once.assert_not_called()
        # 清理 background task 状态
        client._app.state.check_running = False  # type: ignore[attr-defined]

    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_without_filter_calls_run_check_once(
        self, mock_load, mock_run_once, client: AsyncClient
    ) -> None:
        """无筛选参数时走原 run_check_once（行为不变）。"""
        mock_load.return_value.general.data_dir = "/tmp"

        resp = await client.post("/check/run", headers=HTMX_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        await asyncio.sleep(0.05)
        mock_run_once.assert_called_once()
        client._app.state.check_running = False  # type: ignore[attr-defined]

    @patch("web.routes.check.PipelineEngine")
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run_manual_mode_sends_sse(
        self, mock_load, mock_engine, client: AsyncClient
    ) -> None:
        """手动模式（带筛选参数）应通过 SSE 广播日志事件。"""
        # isolate: 清理前序测试残留状态（对齐 test_check_stream_content:85-87）
        app = client._app
        app.state.log_history.clear()
        app.state.check_running = False
        app.state.check_started_at = None

        mock_load.return_value.general.data_dir = "/tmp"

        # 让 run_specific_messages 通过 log_callback 发几条日志后返回。
        # 先 sleep 0.1s 让 SSE 先连上注册 sub_queue，再发 callback（避免 flaky，
        # 对齐现有 test_check_run_sends_sse 的 _slow_run 模式，见 tests/test_web_check.py:128-158）
        async def _fake_run(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0.1)
            cb = kwargs.get("log_callback")
            if cb:
                cb("log", "▶ 手动重跑 1 条消息")
                cb("done", "✅ 手动重跑完成")

        mock_engine.run_specific_messages = _fake_run

        resp = await client.post(
            "/check/run",
            headers=HTMX_HEADERS,
            data={"since": "24h", "reset_phase": "summarized"},
        )
        assert resp.status_code == 200

        # 读 SSE 流，验证能收到日志和 done 事件
        async with client.stream("GET", "/check/stream") as sse:
            assert sse.status_code == 200
            chunks = []
            async for chunk in sse.aiter_bytes():
                chunks.append(chunk)
                if b"event: done" in b"".join(chunks):
                    break

        text = b"".join(chunks).decode("utf-8")
        assert "手动重跑" in text
        assert "event: done" in text
        client._app.state.check_running = False  # type: ignore[attr-defined]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_web_check.py::TestCheck::test_check_run_with_filter_calls_run_specific_messages tests/test_web_check.py::TestCheck::test_check_run_manual_mode_sends_sse -v`
Expected: FAIL — 端点不解析表单参数 / `PipelineEngine` 未在 check.py import

- [ ] **Step 3: 实现 — 重写 `check_run` 方法**

修改 `web/routes/check.py`。**在现有 import 区追加 4~5 行**（不要动现有 3 行 `run_check_once`/`load_config`/`TEMPLATES`，否则会触发 ruff F811 重复 import）。最终顶部 import 块应包含（已有的保持不变，新增的加在合适位置，按 ruff I 规则排序）：

```python
# 已有（保持不动）：
from core.pipeline import run_check_once
from shared.config import load_config
from web.app import TEMPLATES

# 新增 5 行：
from collections.abc import Callable
from core.engine import PipelineEngine
from run_check import parse_since
from shared.message_store import MessageStore
from shared.protocols import Phase
```

> ⚠️ 注意：
> - `run_check_once` / `load_config` / `TEMPLATES` **必须保留**，下方 `check_run` 实现里会用到（全量模式 `await run_check_once(...)`）。
> - 如 check.py 顶部已有 `from collections.abc import Callable`，则只新增 4 行；执行时检查一下。`Callable` 用于下方 `make_log_callback` 工厂函数的返回类型注解（见 Minor 2）。

**子步骤：抽取模块级 `make_log_callback` 工厂**（避免与 Task 3 batch_reprocess 重复，Minor 2）。在 `check.py` 顶部（import 之后、`router = APIRouter()` 之前、`LOG_HISTORY_CAP = 200` 附近）加模块级工厂函数：

```python
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
```

然后 `check_run` 内不再内联 `_log_callback` def，改为 `cb = make_log_callback(state)`，函数体内所有日志广播调用改为 `cb(...)`（见下方 `check_run` 实现已改用工厂）。

然后把 `check_run` 方法（第 52-122 行）整体替换为以下实现（用工厂函数，`_run` 内分流）：

```python
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
            state.check_started_at = None
            state.check_task = None
            # Signal EOF to every active subscriber (broadcast).
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_run())
    return {"status": "started", "mode": "manual" if is_manual else "full"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_web_check.py -v`
Expected: PASS（原有 6 个 + 新增 3 个全过）

- [ ] **Step 5: 提交**

```bash
git add web/routes/check.py tests/test_web_check.py
git commit -m "feat(web): /check/run accepts optional filter params for manual mode (issue #71)"
```

---

### Task 3: 新增 `POST /messages/batch-reprocess` 端点

**Files:**
- Modify: `web/routes/messages.py`（末尾追加）
- Test: `tests/test_web_messages.py`（扩展）

- [ ] **Step 1: 写失败测试 — batch-reprocess 成功路径**

在 `tests/test_web_messages.py` 末尾追加。注意：batch-reprocess 是异步触发（background task），测试需等待 SSE done 或轮询 store 验证 reset 生效。这里用「mock PipelineEngine.run_specific_messages + 验证参数透传」的方式。

先在文件顶部 import 区补 import（如还没有）。检查现有 import：第 15 行已有 `from unittest.mock import AsyncMock, patch`，需确保 `Any`、`asyncio` 可用。在顶部 import 区加：

```python
import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch
```

然后测试函数内的 `import asyncio`（出现在 `test_batch_reprocess_calls_run_specific` 和 `test_batch_reprocess_default_reset_phase` 内的 `await asyncio.sleep(0.1)` 之前）全部删除（顶部已 import）。

在文件末尾追加测试类：

```python
class TestBatchReprocess:
    """POST /messages/batch-reprocess — 批量多选重跑 (issue #71)。

    锁定契约：
    1. 接收 msg_ids 列表 + 可选 reset_phase/skip_push，调 run_specific_messages。
    2. 返回 JSON {"status": "started"}（异步触发，前端轮询或 SSE 监听）。
    3. 无 msg_ids → 400。
    4. reset_phase 缺省 → SUMMARIZED。
    """

    async def test_batch_reprocess_calls_run_specific(
        self, client: tuple[AsyncClient, Path]
    ) -> None:
        c, data_dir = client
        # patch PipelineEngine.run_specific_messages 避免真实执行
        with patch("web.routes.messages.PipelineEngine") as mock_engine:
            mock_engine.run_specific_messages = AsyncMock()

            resp = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={
                    "msg_ids": "bili:BV1test123,xhs:N1",
                    "reset_phase": "summarized",
                    "skip_push": "on",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "started"

            # 等待 background task 执行
            await asyncio.sleep(0.1)
            mock_engine.run_specific_messages.assert_called_once()
            call_kwargs = mock_engine.run_specific_messages.call_args.kwargs
            assert call_kwargs["msg_ids"] == ["bili:BV1test123", "xhs:N1"]
            assert call_kwargs["from_phase"] == Phase.SUMMARIZED
            assert call_kwargs["skip_push"] is True

    async def test_batch_reprocess_default_reset_phase(
        self, client: tuple[AsyncClient, Path]
    ) -> None:
        """不传 reset_phase 时默认 SUMMARIZED。"""
        c, _ = client
        with patch("web.routes.messages.PipelineEngine") as mock_engine:
            mock_engine.run_specific_messages = AsyncMock()

            resp = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"msg_ids": "bili:BV1test123"},
                follow_redirects=False,
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.1)
            call_kwargs = mock_engine.run_specific_messages.call_args.kwargs
            assert call_kwargs["from_phase"] == Phase.SUMMARIZED
            assert call_kwargs["skip_push"] is False  # 未勾选 → False（默认允许重推）

    async def test_batch_reprocess_empty_msg_ids_returns_400(
        self, client: tuple[AsyncClient, Path]
    ) -> None:
        """空 msg_ids 列表 → 400。"""
        c, _ = client
        resp = await c.post(
            "/messages/batch-reprocess",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={"msg_ids": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_batch_reprocess_requires_auth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未登录 → 302 到 /login。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"msg_ids": "bili:BV1"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert "/login" in resp.headers["location"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_web_messages.py::TestBatchReprocess -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 实现 — 在 `web/routes/messages.py` 加端点**

在 `web/routes/messages.py` 文件顶部 import 区（第 24-26 行后）补 import：

```python
from core.engine import PipelineEngine
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import Phase
from web.routes.check import LOG_HISTORY_CAP, make_log_callback
```

（`MessageStore`、`Phase`、`load_config` 已在第 24-26 行 import，只需补 `from core.engine import PipelineEngine` 和 `from web.routes.check import LOG_HISTORY_CAP, make_log_callback`。`messages.py` 和 `check.py` 同包，无循环依赖风险，顶部 import 更清晰。）

> Minor 2：`make_log_callback` 从 check.py 复用，避免与 check_run 的 `_log_callback` 重复。

然后在文件末尾（第 95 行后）追加新端点：

```python
@router.post("/messages/batch-reprocess")
async def batch_reprocess(request: Request) -> dict[str, str]:
    """批量重跑选中消息（issue #71 批量多选入口）。

    接收表单参数：
    - msg_ids: 逗号分隔的消息 ID 列表（如 "bili:BV1,xhs:N1"）
    - reset_phase: 可选，重跑起始阶段（缺省 SUMMARIZED）
    - skip_push: 可选，"on"/"true"/"1" 时禁止重新推送（默认 False，允许重推）。前端 checkbox 默认勾选发送 "on" 与 CLI --skip-push 默认 True 行为一致

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
    target_phase = Phase[reset_phase_str.upper()] if reset_phase_str else Phase.SUMMARIZED
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
```

注意：`messages.py` 顶部需补 `import asyncio` 和 `import time`（现有文件两者都没有）。在文件顶部 import 区（第 19 行 `import logging` 后）加：

```python
import asyncio
import logging
import time
```

> ⚠️ `asyncio.create_task` 和 `time.time()`/`time.strftime()` 都要模块级可用，不要在函数内局部 import（见下方 `make_log_callback` 内部 / `_run` / `except` 块都用到了）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_web_messages.py::TestBatchReprocess -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add web/routes/messages.py tests/test_web_messages.py
git commit -m "feat(web): add POST /messages/batch-reprocess for batch multi-select rerun (issue #71)"
```

---

### Task 4: check.html 加筛选表单 + 手动模式隐藏平台卡片

**Files:**
- Modify: `web/templates/check.html`（加表单 + 改 JS）

**说明：** 这是纯前端改动，无独立测试（前端测试靠 Task 2 的 API 测试 + 手动验证）。先做后端再串调。

- [ ] **Step 1: 在 status panel 下方加筛选表单**

修改 `web/templates/check.html`。在第 33 行（status panel 的 `</div>` 闭合后、第 35 行 `<!-- Platform progress cards -->` 之前）插入筛选表单。

**前置：给平台卡片容器加 id**。现有第 36 行的平台卡片容器 `<div class="grid grid-cols-1 md:grid-cols-3 ...">` 要加 `id="platform-cards"`（避免 JS 用 fragile CSS 选择器 `querySelector('.grid.grid-cols-1.md\\:grid-cols-3')`）。找到该 div，加 `id="platform-cards"` 属性。

插入内容如下：

```html
</div>

<!-- Manual filter form (issue #71) -->
<div id="filter-panel" class="bg-[var(--card-bg)] backdrop-blur-[12px] rounded-[14px] p-5 shadow-card border border-[var(--card-border)] mb-4">
  <details class="group">
    <summary class="cursor-pointer flex items-center gap-2 text-sm font-medium select-none">
      <svg class="w-4 h-4 transition-transform group-open:rotate-90" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>
      手动筛选重跑（按条件筛选历史消息重跑流水线）
    </summary>
    <form id="manual-filter-form" class="mt-4 grid grid-cols-1 md:grid-cols-4 gap-3">
      <div>
        <label class="block text-xs uppercase tracking-wider text-[var(--text-tertiary)] mb-1">时间范围</label>
        <select name="since" class="w-full px-3 py-2 rounded-[8px] bg-[var(--card-bg)] border border-[var(--card-border)] text-sm">
          <option value="">不限</option>
          <option value="24h">最近 24 小时</option>
          <option value="7d">最近 7 天</option>
          <option value="30d">最近 30 天</option>
        </select>
      </div>
      <div>
        <label class="block text-xs uppercase tracking-wider text-[var(--text-tertiary)] mb-1">标题包含</label>
        <input type="text" name="title" placeholder="关键词" class="w-full px-3 py-2 rounded-[8px] bg-[var(--card-bg)] border border-[var(--card-border)] text-sm">
      </div>
      <div>
        <label class="block text-xs uppercase tracking-wider text-[var(--text-tertiary)] mb-1">作者包含</label>
        <input type="text" name="author" placeholder="作者名" class="w-full px-3 py-2 rounded-[8px] bg-[var(--card-bg)] border border-[var(--card-border)] text-sm">
      </div>
      <div>
        <label class="block text-xs uppercase tracking-wider text-[var(--text-tertiary)] mb-1">平台</label>
        <select name="platform" class="w-full px-3 py-2 rounded-[8px] bg-[var(--card-bg)] border border-[var(--card-border)] text-sm">
          <option value="">全部</option>
          <option value="bili">B站</option>
          <option value="xhs">小红书</option>
          <option value="weibo">微博</option>
        </select>
      </div>
      <div>
        <label class="block text-xs uppercase tracking-wider text-[var(--text-tertiary)] mb-1">重跑起始阶段</label>
        <select name="reset_phase" class="w-full px-3 py-2 rounded-[8px] bg-[var(--card-bg)] border border-[var(--card-border)] text-sm">
          <option value="summarized">摘要（重新生成 AI 摘要）</option>
          <option value="downloaded">下载（重新抓取正文）</option>
          <option value="transcribed">转写（视频重新转写）</option>
          <option value="discovered">发现（重跑全流水线）</option>
        </select>
      </div>
      <div class="flex items-end">
        <label class="flex items-center gap-2 text-sm text-[var(--text-secondary)] cursor-pointer">
          <input type="checkbox" name="skip_push" checked class="w-4 h-4 rounded">
          跳过推送
        </label>
      </div>
      <div class="flex items-end md:col-span-2">
        <button type="button" id="manual-run-btn"
          class="px-4 py-2 rounded-[10px] text-sm font-medium bg-[var(--color-primary)]/10 text-[var(--color-primary)] hover:bg-[var(--color-primary)]/20 transition-colors duration-200">
          按筛选条件重跑
        </button>
      </div>
    </form>
  </details>
</div>

<!-- Platform progress cards -->
```

- [ ] **Step 2: 改 `startCheck` 支持手动模式 + 加隐藏平台卡片逻辑**

修改 `web/templates/check.html` 的 `<script>` 块。

**前置：更新 HTML onclick 调用**。现有「立即检查」按钮的 `onclick="startCheck()"` 要改为 `onclick="startCheck(false)"`（显式传 `false` 表示全量模式，与新 `startCheck(isManual)` 签名对齐；`undefined` 也能 work 但显式更清晰）。在 check.html 中找到该按钮（约第 23 行附近），把 `onclick="startCheck()"` 改为 `onclick="startCheck(false)"`。

把现有的 `startCheck` 函数（第 180-194 行）替换为支持两种模式的版本：

```javascript
  function startCheck(isManual) {
    var btn = document.getElementById('run-btn');
    if (btn.disabled) return;
    var body;
    if (isManual) {
      // 手动模式：收集筛选表单
      var form = document.getElementById('manual-filter-form');
      body = new FormData(form);
      // fetch 的 body 直接传 FormData 会自动 multipart/form-data
      // 注意：skip_push checkbox 未勾选时 FormData 不含该字段，后端 default False 正确处理
      // （见 Task 2 修复后的 skip_push 解析逻辑）
    } else {
      body = new FormData();
    }
    fetch('/check/run', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: body })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === 'started') {
          initSSE(isManual);  // 传 mode 决定是否隐藏平台卡片
        }
        else if (data.status === 'already_running') {
          restoreFromStatus().then(function() { connectSSE(); });
        }
        else { showToast('启动失败: ' + (data.status || '未知'), 'error'); }
      })
      .catch(function(err) { showToast('请求失败: ' + err.message, 'error'); });
  }

  /* 手动重跑按钮绑定 */
  document.getElementById('manual-run-btn').addEventListener('click', function() {
    startCheck(true);
  });
```

> ⚠️ **listener 注册位置**：上述 `manual-run-btn` 的 click listener 必须放在 `<script>` 块**末尾**（在 `connectSSE` 函数定义之后、`{% endblock %}` 之前），不能放在 `<script>` 顶部。原因：DOM 元素必须已渲染（脚本在 body 末尾或 DOMContentLoaded 后执行），且 listener 引用的 `startCheck` 函数已定义。若放在顶部会因 DOM 未就绪导致 `getElementById('manual-run-btn')` 返回 null 抛 TypeError。建议直接追加到现有 `<script>` 块最后，与 `restoreFromStatus()` 调用同层。

然后修改 `initSSE` 函数（原第 198-221 行），加 `isManual` 参数控制平台卡片显隐。把现有 `initSSE` 替换为：

```javascript
  var doneHandled = false;
  var currentMode = 'full';  // 'full' | 'manual'
  function initSSE(isManual) {
    if (evtSource) { evtSource.close(); evtSource = null; }
    doneHandled = false;
    currentMode = isManual ? 'manual' : 'full';
    startTime = Date.now();
    processedCount = 0;
    updateStatus('running', isManual ? '手动重跑中' : '运行中');
    setRunButtonState('running');

    // 手动模式隐藏平台进度卡片（不跑 detector，卡片无数据），全量模式恢复显示
    var cardsGrid = document.getElementById('platform-cards');
    if (cardsGrid) {
      cardsGrid.style.display = isManual ? 'none' : '';
    }

    // Reset platform cards (全量模式才需要)
    if (!isManual) {
      PLATFORMS.forEach(function(p) { setPlatformStatus(p, 'idle'); });
    }

    // Reset log
    var logEl = document.getElementById('log-output');
    logEl.innerHTML = '';
    document.getElementById('processed-count').textContent = '0';
    document.querySelector('.live-indicator').classList.remove('hidden');

    // Start elapsed timer
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedTimer = setInterval(updateElapsed, 1000);
    updateElapsed();

    connectSSE();
  }
```

- [ ] **Step 3: 验证前端加载不报错**

Run: `uv run pytest tests/test_web_check.py::TestCheck::test_check_page -v`
Expected: PASS（页面能正常渲染）

手动验证（可选）：`uv run uvicorn web.app:app --reload` 后访问 `/check`，展开「手动筛选重跑」面板，确认表单渲染正常、平台卡片在手动模式隐藏。

- [ ] **Step 4: 提交**

```bash
git add web/templates/check.html
git commit -m "feat(web/check): add manual filter form + hide platform cards in manual mode (issue #71)"
```

---

### Task 5: Dashboard 消息列表加 checkbox 多选 + 批量重跑按钮

**Files:**
- Modify: `web/templates/dashboard.html:214-267`（消息表格区域）
- Test: `tests/test_web_messages.py`（扩展，验证 checkbox 渲染）

- [ ] **Step 1: 写失败测试 — checkbox 渲染在消息表格**

在 `tests/test_web_messages.py` 末尾追加（复用 `TestRetryButtonRenderedInDashboard` 的 patch 模式）：

```python
class TestBatchReprocessCheckboxRendered:
    """Dashboard 消息表格渲染 checkbox 多选 + 批量按钮（issue #71 smoke）。"""

    async def test_dashboard_renders_checkbox_per_row(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        with patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = {"bilibili": [{"uid": 1, "name": "t"}]}
            resp = await c.get("/")
        assert resp.status_code == 200
        # 每行 checkbox name="msg_id" value="{msg_id}"
        assert 'name="msg_id"' in resp.text
        assert 'value="bili:BV1test123"' in resp.text
        # 表头全选 checkbox
        assert 'id="select-all"' in resp.text
        # 批量按钮
        assert '重跑选中' in resp.text
        assert '/messages/batch-reprocess' in resp.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_web_messages.py::TestBatchReprocessCheckboxRendered -v`
Expected: FAIL — checkbox / select-all / 批量按钮未渲染

- [ ] **Step 3: 实现 — 修改 dashboard.html 消息表格**

修改 `web/templates/dashboard.html`。

先在 `<table>` 的 `<thead>` 加一列「选择」（第 222 行 `<tr>` 内最前面）：

把第 221-231 行（thead）替换为：

```html
        <tr class="text-[var(--text-secondary)] text-xs uppercase tracking-wider">
          <th class="text-left px-5 py-3 font-medium w-10">
            <input type="checkbox" id="select-all" class="w-4 h-4 rounded" aria-label="全选">
          </th>
          <th class="text-left px-5 py-3 font-medium">处理时间</th>
          <th class="text-left px-5 py-3 font-medium">发布时间</th>
          <th class="text-left px-5 py-3 font-medium">平台</th>
          <th class="text-left px-5 py-3 font-medium">标题</th>
          <th class="text-left px-5 py-3 font-medium">作者</th>
          <th class="text-left px-5 py-3 font-medium">阶段</th>
          <th class="text-right px-5 py-3 font-medium">详情</th>
        </tr>
```

然后在每行 `<tr>`（第 234 行开始）的第一个 `<td>` 前加 checkbox 单元格。把第 233-235 行替换为：

```html
        {% for msg in recent_messages %}
        <tr class="border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/30 transition-colors">
          <td class="px-5 py-3">
            <input type="checkbox" name="msg_id" value="{{ msg.msg_id }}" class="row-checkbox w-4 h-4 rounded" aria-label="选择 {{ msg.title }}">
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-[var(--text-secondary)]">{{ msg.updated_at | timeago }}</td>
```

（即：在原第一个 `<td>`（处理时间）前插入 checkbox `<td>`，其余列不动。）

最后，在表格容器外（第 214 行的 `<div class="bg-[var(--card-bg)]...">` 内、`<table>` 的父 `<div>` 之后，`{% else %}` 之前）加批量操作栏。把第 257-258 行（`</div>` 闭合表格 overflow div + per-row detail panel 注释）替换为：

```html
    </div>
    {# Batch reprocess bar (issue #71): multi-select + rerun selected #}
    <div class="px-5 py-3 border-t border-gray-100 dark:border-gray-800 flex items-center justify-between gap-3">
      <span class="text-xs text-[var(--text-tertiary)]">
        选中 <span id="selected-count">0</span> 条
      </span>
      <form id="batch-reprocess-form"
            onsubmit="return prepareBatchSubmit(this);">
        <input type="hidden" name="msg_ids" id="batch-msg-ids">
        <label class="flex items-center gap-1.5 text-xs text-[var(--text-secondary)]">
          <span class="text-[var(--text-tertiary)]">起始阶段</span>
          <select name="reset_phase" class="px-2 py-1 rounded-[6px] bg-[var(--card-bg)] border border-[var(--card-border)] text-xs">
            <option value="summarized">摘要</option>
            <option value="downloaded">下载</option>
            <option value="transcribed">转写</option>
            <option value="discovered">发现</option>
          </select>
        </label>
        <label class="flex items-center gap-1.5 text-xs text-[var(--text-secondary)] cursor-pointer">
          <input type="checkbox" name="skip_push" checked class="w-3.5 h-3.5 rounded">
          <span>跳过推送</span>
        </label>
        <button type="submit"
                class="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-[6px] text-xs font-medium bg-[var(--color-primary)]/10 text-[var(--color-primary)] hover:bg-[var(--color-primary)]/20 cursor-pointer transition-colors duration-200"
                aria-label="重跑选中的消息">
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polyline points="23 4 23 10 17 10"/>
            <polyline points="1 20 1 14 7 14"/>
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
          </svg>
          重跑选中
        </button>
      </form>
    </div>
    {# Per-row click-toggled detail panels (rendered once per message, below the table).
        Pure-frontend toggle avoids the table+absolute CSS pitfall and works on touch. #}
```

- [ ] **Step 4: 加 JS 逻辑 — 全选 + 计数 + 提交时收集 msg_ids**

在 `web/templates/dashboard.html` 的 `{% block content %}` 末尾、`{% endblock %}` 之前（第 267 行前）加 `<script>`：

```html
<script>
  /* ── Batch multi-select (issue #71) ── */
  (function() {
    var selectAll = document.getElementById('select-all');
    var checkboxes = document.querySelectorAll('.row-checkbox');
    var countEl = document.getElementById('selected-count');

    function updateCount() {
      var checked = document.querySelectorAll('.row-checkbox:checked');
      countEl.textContent = checked.length;
    }

    if (selectAll) {
      selectAll.addEventListener('change', function() {
        checkboxes.forEach(function(cb) { cb.checked = selectAll.checked; });
        updateCount();
      });
    }
    checkboxes.forEach(function(cb) { cb.addEventListener('change', updateCount); });
  })();

  /* 提交批量表单前，把选中的 msg_id 收集成逗号分隔字符串。
     用 fetch POST 而非默认表单提交，手动处理 JSON 响应（HTMX 对 JSON 不友好，
     原先 hx-post + JSON 端点用户无 UI 反馈）。 */
  function prepareBatchSubmit(form) {
    var checked = document.querySelectorAll('.row-checkbox:checked');
    if (checked.length === 0) {
      showToast('请先选择消息', 'warning');
      return false;
    }
    var ids = Array.prototype.map.call(checked, function(cb) { return cb.value; });
     var msgIds = ids.join(',');
     document.getElementById('batch-msg-ids').value = msgIds;
     // 从表单读取 reset_phase / skip_push（与 check.html 对称，不再硬编码）
     var resetPhase = form.elements.reset_phase.value;
     var skipPush = form.elements.skip_push.checked ? 'on' : '';
     // 用 fetch POST 而非默认表单提交，手动处理 JSON 响应
     fetch('/messages/batch-reprocess', {
       method: 'POST',
       headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/x-www-form-urlencoded' },
       body: 'msg_ids=' + encodeURIComponent(msgIds) +
           '&reset_phase=' + encodeURIComponent(resetPhase) +
           '&skip_push=' + skipPush
     }).then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status === 'started') {
          showToast('批量重跑已启动，跳转查看进度', 'success');
          window.location.href = '/check';
        } else if (data.status === 'already_running') {
          showToast('已有检查任务在运行', 'warning');
        } else {
          showToast('启动失败: ' + (data.status || '未知'), 'error');
        }
      })
      .catch(function(err) { showToast('请求失败: ' + err.message, 'error'); });
    return false;  // 阻止默认表单提交
  }
</script>
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/test_web_messages.py -v`
Expected: PASS（原有 + TestBatchReprocessCheckboxRendered + TestBatchReprocess 全过）

- [ ] **Step 6: 提交**

```bash
git add web/templates/dashboard.html tests/test_web_messages.py
git commit -m "feat(web/dashboard): add multi-select checkbox + batch rerun button (issue #71)"
```

---

### Task 6: 端到端验证 + 全量回归

**Files:** 无新改动，纯验证

- [ ] **Step 1: 全量测试**

Run: `uv run pytest -x`
Expected: PASS（所有测试无回归）

- [ ] **Step 2: Lint + 类型检查**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pyright`
Expected: 无新增 error（注意：pyright 不加 `.` 参数，见 AGENTS.md）

- [ ] **Step 3: 手动冒烟（可选，需真实 cookies）**

```bash
uv run uvicorn web.app:app --reload
```

访问 `/check`：
1. 展开筛选表单，选「最近 7 天」+ 标题填关键词，点「按筛选条件重跑」
2. 确认平台卡片隐藏，日志区显示「匹配 N 条消息」+ 处理过程
3. SSE 流到 `done` 事件后按钮恢复

访问 `/`（Dashboard）：
1. 消息表格每行出现 checkbox
2. 勾选 2 条，点「重跑选中」
3. 确认 toast 提示成功，跳转或 SSE 监听重跑进度

- [ ] **Step 4: 最终提交（如有 lint 修复）**

```bash
git add -A
git commit -m "chore: fix lint after issue #71 web manual check"
```

---

## Self-Review 检查

**1. Spec coverage（issue #71 需求）:**
- ✅ 复用 `POST /check/run` 接受可选筛选参数（Task 2）— 决策 A1
- ✅ 有筛选走 `run_specific_messages`，无参数走原 `run_check_once`（Task 2）— 决策 A1
- ✅ 批量多选 UI（Task 5）+ `batch-reprocess` 端点（Task 3）— 决策 A2
- ✅ 共享单锁 `state.check_running`（Task 2 + Task 3 均检查 `if state.check_running`）— 决策 A3
- ✅ 手动模式隐藏平台卡片（Task 4 Step 2 `initSSE(isManual)`）— 决策 A4
- ✅ `run_specific_messages` 加 `log_callback`（Task 1）— 后端前置
- ✅ 复用 `parse_since`（Task 2 import from run_check）— 避免重复实现
- ✅ 测试覆盖：engine 层（Task 1）+ web check 层含 SSE（Task 2）+ batch 端点（Task 3）+ 前端 smoke（Task 5）

**2. Placeholder scan:** 无 TODO / "implement later" / "similar to Task N"。每个代码块都是完整可执行内容。

**3. Type consistency:**
- `log_callback: Callable[[str, str], None] | None` — engine.py、check.py、messages.py 一致
- `run_specific_messages` 签名：`(msg_ids, from_phase, skip_push, config, store, log_callback=None)` — Task 1 定义，Task 2/3 调用一致
- `Phase` enum：Task 2 用 `Phase[reset_phase_str.upper()]`，与 CLI `_run_manual_check:648` 一致
- SSE 事件类型 `"log"` / `"done"` — Task 1 发出，check.html `appendLog` 消费一致
- `state.check_running` 单锁 — Task 2 `check_run`、Task 3 `batch_reprocess` 都检查并设置
- **跨页交接契约**：batch-reprocess 启动后跳转 /check，`/check` 的 `restoreFromStatus` 必须能处理 `running=True && log_history=[]` 的瞬态（batch-reprocess 启动后 SSE 已在推流，跳转瞬间 log_history 可能尚未填充，依赖 SSE 续流补全）。前端 `restoreFromStatus` 已有 fallback：running 时自动 `connectSSE()` 接收后续事件，因此瞬态安全。

**4. 项目规范检查:**
- ✅ `from __future__ import annotations` — engine.py 已有（第 13 行），check.py 已有（第 1 行），messages.py 已有（第 17 行）
- ✅ Type hint 完整 — 所有新函数签名有类型
- ✅ 新增日志字符串遵循现有 emoji 约定（▶⚠️📋⏭✅），未改动已有 error message 文本
- ✅ 不引入新依赖 — 只用 FastAPI/Jinja2/asyncio 标准库

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-02-issue71-web-manual-check.md`.
