# Plan: D 组全局日志页（SSE 实时流）

**分支**: `feat/d-global-logs`

---

## 背景

为 Trawler Web UI 增加一个独立于 check 页的全局日志页面（`/logs`），通过 SSE 实时流展示全应用所有模块的 `logging` 日志，支持级别过滤和自动滚动暂停。

## 范围

| 操作 | 文件 |
|------|------|
| 新增 | `web/logging_bridge.py` — QueueLogHandler + LogBus + setup/teardown |
| 新增 | `web/routes/logs.py` — `/logs` 页面 + `/logs/stream` SSE |
| 新增 | `web/templates/logs.html` — 日志模板（复用 check 页终端样式） |
| 修改 | `web/app.py` — lifespan 集成 + 路由注册 |
| 修改 | `web/templates/base.html` — 侧边栏加入口 |

## 决策摘要

1. **Handler**: 自定义 `logging.Handler` 子类，`emit()` 里调用 `LogBus.publish()`，try/except 防递归
2. **队列结构**: LogBus 维护 `subscribers: list[asyncio.Queue]`（fan-out）+ `history: list`（容量 1000）
3. **fan-out 模式**: 每个 SSE 连接独立 queue，避免 check 页单 queue 多消费者的 bug
4. **logging 接管**: `web/logging_bridge.py` 封装，lifespan 里调 `setup_web_logging()`
5. **路由**: `GET /logs` 页面 + `GET /logs/stream` SSE
6. **UI**: 复用 check 页终端样式，级别过滤器（ALL/DEBUG/INFO/WARN/ERROR），自动滚动暂停，清空按钮
7. **历史日志**: 新 subscriber 先回放 history，再续 live
8. **过滤 uvicorn.access**: 避免 access log 噪音

## 任务清单

---

### Task 1: 新建 `web/logging_bridge.py`

- **文件**: `web/logging_bridge.py` (新)
- **目标**: 封装 QueueLogHandler + LogBus + setup/teardown

**完整代码**:

```python
"""Bridge Python logging to async SSE subscribers via fan-out pattern.

Architecture
------------
    logging module
        │
        ▼
    QueueLogHandler.emit()
        │
        ▼
    LogBus.publish(item)
        │
        ├──▶ history.append(item)          # bounded, for replay
        │
        └──▶ for each subscriber queue:    # fan-out, one queue per connection
                 queue.put_nowait(item)
                 on QueueFull → remove subscriber

This is deliberately decoupled from check.py's single-queue approach.
Every SSE connection gets its own asyncio.Queue via subscribe().
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence

# ── constants ────────────────────────────────────────────────────────────────
LOG_HISTORY_CAP = 1000

# ── data model ───────────────────────────────────────────────────────────────


class LogEntry:
    """A single log record formatted for SSE delivery."""

    __slots__ = ("level", "message", "time")

    def __init__(self, level: str, message: str, time: str) -> None:
        self.level = level
        self.message = message
        self.time = time

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "message": self.message, "time": self.time}


# ── level mapping ────────────────────────────────────────────────────────────

LEVEL_MAP: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

# ── LogBus: fan-out hub ─────────────────────────────────────────────────────


class LogBus:
    """Fan-out hub that distributes log entries to all active subscribers.

    Each subscriber (SSE connection) gets its own asyncio.Queue so that a
    slow consumer does not block others — the slow one is simply evicted.
    """

    def __init__(self, history_cap: int = LOG_HISTORY_CAP) -> None:
        self._subscribers: list[asyncio.Queue[LogEntry | None]] = []
        self._history: list[LogEntry] = []
        self._cap = history_cap
        self._lock = threading.RLock()

    # ── subscriber management ────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[LogEntry | None]:
        """Create a new subscriber queue and register it."""
        q: asyncio.Queue[LogEntry | None] = asyncio.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[LogEntry | None]) -> None:
        """Remove a subscriber queue."""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    @property
    def history(self) -> Sequence[LogEntry]:
        """Read-only snapshot of current history."""
        return list(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ── publishing ───────────────────────────────────────────────────────

    def publish(self, item: LogEntry) -> None:
        """Fan-out a log entry to all subscribers + append to history."""
        with self._lock:
            # 1) Append to history (bounded)
            self._history.append(item)
            if len(self._history) > self._cap:
                self._history[: len(self._history) - self._cap] = []

            # 2) Fan-out to subscribers (iterate over a copy for safe removal)
            dead: list[asyncio.Queue[LogEntry | None]] = []
            for q in list(self._subscribers):
                try:
                    q.put_nowait(item)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)


# ── custom logging Handler ───────────────────────────────────────────────────


class QueueLogHandler(logging.Handler):
    """logging.Handler that feeds a LogBus.

    emit() may be called from any thread (e.g. uvicorn workers). The
    LogBus uses an internal RLock to serialise subscribe/unsubscribe/publish.
    """

    def __init__(self, bus: LogBus) -> None:
        super().__init__()
        self._bus = bus
        # Standard format: "HH:MM:SS [LEVEL] logger_name: message"
        fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        datefmt = "%H:%M:%S"
        self.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and publish to the bus.

        Wrapped in try/except to avoid infinite recursion if logging itself
        triggers another log call.
        """
        try:
            level_name = LEVEL_MAP.get(record.levelno, "info")
            formatted = self.format(record)
            entry = LogEntry(level=level_name, message=formatted, time="")
            self._bus.publish(entry)
        except Exception:
            self.handleError(record)


# ── setup / teardown ─────────────────────────────────────────────────────────

_UVICORN_ACCESS_LOGGER = "uvicorn.access"


def setup_web_logging(bus: LogBus | None = None, level: int = logging.INFO) -> LogBus:
    """Install QueueLogHandler on the root logger.

    Call once during FastAPI lifespan startup. Idempotent-safe: skips if
    a QueueLogHandler is already installed.

    Returns the LogBus instance (for passing to routes).

    Note: uvicorn.access logger is explicitly filtered out to avoid
    flooding the log page with HTTP request lines.
    """
    if bus is None:
        bus = LogBus()

    root = logging.getLogger()
    # Avoid duplicate installation
    for h in root.handlers:
        if isinstance(h, QueueLogHandler):
            return bus  # already installed

    handler = QueueLogHandler(bus)
    handler.setLevel(level)

    # Filter out uvicorn access logs
    class _SkipUvicornAccess(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return record.name != _UVICORN_ACCESS_LOGGER

    handler.addFilter(_SkipUvicornAccess())

    root.addHandler(handler)
    return bus


def teardown_web_logging(bus: LogBus | None = None) -> None:
    """Remove QueueLogHandler from the root logger.

    Call during FastAPI lifespan shutdown.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, QueueLogHandler):
            root.removeHandler(h)
```

---

### Task 2: `web/app.py` — lifespan 集成 + 路由注册

- **文件**: `web/app.py`
- **改动 1**: 在 lifespan 里调用 setup/teardown

**Before** (lifespan function lines 68–86):

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: initialize async resources.

    Refreshed on real startup so each server run gets a fresh queue and a
    clean running flag, independent of any state set at module import time.
    """
    app.state.subscribers = []  # list[asyncio.Queue[dict | None]]
    app.state.log_history = []
    app.state.check_running = False
    app.state.check_task = None
    app.state.check_processed_count = 0
    app.state.check_started_at = None
    yield
    # Cancel any running check on shutdown
    current_task = app.state.check_task
    if current_task is not None and not current_task.done():
        current_task.cancel()
```

**After**:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: initialize async resources.

    Refreshed on real startup so each server run gets a fresh queue and a
    clean running flag, independent of any state set at module import time.
    """
    app.state.subscribers = []  # list[asyncio.Queue[dict | None]]
    app.state.log_history = []
    app.state.check_running = False
    app.state.check_task = None
    app.state.check_processed_count = 0
    app.state.check_started_at = None

    # ── D 组: 全局日志页 ────────────────────────────────────────────
    from web.logging_bridge import setup_web_logging, teardown_web_logging
    log_bus = setup_web_logging()
    app.state.log_bus = log_bus
    # ──────────────────────────────────────────────────────────────────

    yield

    # ── D 组: 清理 ────────────────────────────────────────────────────
    teardown_web_logging(log_bus)
    # ──────────────────────────────────────────────────────────────────

    # Cancel any running check on shutdown
    current_task = app.state.check_task
    if current_task is not None and not current_task.done():
        current_task.cancel()
```

- **改动 2**: `create_app()` 函数里加 `app.state.log_bus` 初始化（和现有 `app.state.log_queue` 在同一块，方便测试不跑 lifespan 时也有值）：

**Before** (lines 88–100):

```python
def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Trawler Web UI", version="0.1.0", lifespan=lifespan)

    # Initialize async resources on app.state so they exist even when the
    # lifespan handler is not executed (e.g. httpx ASGITransport in tests).
    # The lifespan handler re-initializes them on real startup.
    app.state.subscribers = []  # list[asyncio.Queue[dict | None]]
    app.state.log_history = []
    app.state.check_running = False
    app.state.check_task = None
    app.state.check_processed_count = 0
    app.state.check_started_at = None
```

**After**:

```python
def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Trawler Web UI", version="0.1.0", lifespan=lifespan)

    # Initialize async resources on app.state so they exist even when the
    # lifespan handler is not executed (e.g. httpx ASGITransport in tests).
    # The lifespan handler re-initializes them on real startup.
    app.state.subscribers = []  # list[asyncio.Queue[dict | None]]
    app.state.log_history = []
    app.state.check_running = False
    app.state.check_task = None
    app.state.check_processed_count = 0
    app.state.check_started_at = None

    # D 组: 全局日志页 - LogBus (lifespan 里会重新初始化)
    from web.logging_bridge import LogBus
    app.state.log_bus = LogBus()
```

- **改动 3**: 在 `include_router` 块注册 logs_router：

**Before** (lines 107–117):

```python
    from web.routes.auth import router as auth_router
    from web.routes.check import router as check_router
    from web.routes.dashboard import router as dashboard_router
    from web.routes.settings import router as settings_router
    from web.routes.subscriptions import router as subscriptions_router

    app.include_router(dashboard_router)
    app.include_router(subscriptions_router)
    app.include_router(check_router)
    app.include_router(auth_router)
    app.include_router(settings_router)
```

**After**:

```python
    from web.routes.auth import router as auth_router
    from web.routes.check import router as check_router
    from web.routes.dashboard import router as dashboard_router
    from web.routes.logs import router as logs_router
    from web.routes.settings import router as settings_router
    from web.routes.subscriptions import router as subscriptions_router

    app.include_router(dashboard_router)
    app.include_router(subscriptions_router)
    app.include_router(check_router)
    app.include_router(auth_router)
    app.include_router(logs_router)
    app.include_router(settings_router)
```

---

### Task 3: 新建 `web/routes/logs.py`

- **文件**: `web/routes/logs.py` (新)
- **内容**: `/logs` 页面 + `/logs/stream` SSE 端点

**完整代码**:

```python
"""D 组: 全局日志页 — SSE 实时日志流。

完全独立于 check 页的 log_queue/log_history，使用 fan-out 模式
(LogBus.subscribe()) 避免单 queue 多消费者的竞争问题。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from web.app import TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    """Global log page — real-time log output."""
    return TEMPLATES.TemplateResponse(
        request,
        "logs.html",
        {
            "active_nav": "logs",
        },
    )


@router.get("/logs/stream")
async def logs_stream(request: Request) -> StreamingResponse:
    """SSE endpoint: stream all application logs to the browser.

    Uses LogBus.subscribe() to get a dedicated queue (fan-out pattern).
    New subscribers first receive the current history snapshot, then
    continue with live entries.
    """
    bus = request.app.state.log_bus

    # Subscribe before reading history to avoid missing entries between
    # the snapshot and the live loop.
    queue = bus.subscribe()
    history_snapshot = list(bus.history)

    async def event_generator() -> AsyncIterator[bytes]:
        # 1) Replay history
        for entry in history_snapshot:
            data = json.dumps(entry.to_dict(), ensure_ascii=False)
            yield f"event: log\ndata: {data}\n\n".encode("utf-8")

        # 2) Stream live events
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                # None sentinel is not used in the current design, but
                # handle it gracefully for future extensibility.
                if item is None:
                    break
                data = json.dumps(item.to_dict(), ensure_ascii=False)
                yield f"event: log\ndata: {data}\n\n".encode("utf-8")
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

---

### Task 4: 新建 `web/templates/logs.html`

- **文件**: `web/templates/logs.html` (新)
- **内容**: 复用 check 页终端样式，5 档级别过滤 + 自动滚动暂停 + 清空

**完整代码**:

```html
{% extends "base.html" %}
{% block title %}全局日志 · Trawler{% endblock %}

{% block content %}
<h1 class="text-2xl font-semibold tracking-tight mb-1">全局日志</h1>
<p class="text-sm text-[var(--text-secondary)] mb-6">全应用所有模块的实时日志输出</p>

<!-- Terminal log -->
<div class="rounded-[14px] shadow-[0_4px_14px_rgba(0,0,0,0.05)] border border-gray-800 overflow-hidden">
  <!-- Filter bar -->
  <div class="flex items-center justify-between px-4 py-2 bg-[#252526] border-b border-gray-800">
    <div class="flex gap-1">
      <button class="filter-btn px-3 py-1 text-xs rounded-[6px] bg-blue-600 text-white" data-filter="all">ALL</button>
      <button class="filter-btn px-3 py-1 text-xs rounded-[6px] text-gray-400 hover:text-white transition-colors" data-filter="debug">DEBUG</button>
      <button class="filter-btn px-3 py-1 text-xs rounded-[6px] text-gray-400 hover:text-white transition-colors" data-filter="info">INFO</button>
      <button class="filter-btn px-3 py-1 text-xs rounded-[6px] text-gray-400 hover:text-white transition-colors" data-filter="warn">WARN</button>
      <button class="filter-btn px-3 py-1 text-xs rounded-[6px] text-gray-400 hover:text-white transition-colors" data-filter="error">ERROR</button>
    </div>
    <div class="flex items-center gap-3">
      <span id="live-indicator" class="text-xs text-green-500">● LIVE</span>
      <span id="pause-indicator" class="text-xs text-yellow-500 hidden">⏸ 暂停</span>
      <button onclick="document.getElementById('log-output').innerHTML = '<span class=\'text-gray-500\'>日志已清空</span>'" class="text-xs text-gray-500 hover:text-white transition-colors">清空</button>
    </div>
  </div>
  <!-- Log area -->
  <div id="log-output" class="p-4 font-mono text-sm leading-relaxed h-[480px] overflow-y-auto" style="background: #1e1e1e; color: #d4d4d4;">
    <span class="text-gray-500">等待日志接入…</span>
  </div>
</div>

<script>
  /* ── SSE connection ── */
  var evtSource = null;
  var logContainer = document.getElementById('log-output');

  function connectLogSSE() {
    if (evtSource) { evtSource.close(); evtSource = null; }
    evtSource = new EventSource('/logs/stream');

    evtSource.addEventListener('log', function(e) {
      var data;
      try { data = JSON.parse(e.data); } catch(_) { return; }
      appendLog(data);
    });

    evtSource.addEventListener('error', function() {
      if (evtSource && evtSource.readyState === EventSource.CLOSED) {
        // Connection lost — show warning but don't flood user with toasts
      }
    });
  }

  function appendLog(data) {
    var level = (data.level || 'info').toLowerCase();
    var levelUpper = level.toUpperCase();
    var colorClass;
    if (level === 'error' || level === 'err') {
      colorClass = 'text-red-400';
      levelUpper = 'ERR ';
    } else if (level === 'warn' || level === 'warning') {
      colorClass = 'text-orange-400';
      levelUpper = 'WARN';
    } else if (level === 'debug') {
      colorClass = 'text-gray-500';
      levelUpper = 'DEBUG';
    } else {
      colorClass = 'text-gray-400';
      levelUpper = 'INFO';
    }

    var line = document.createElement('div');
    line.className = 'log-line';
    line.setAttribute('data-level', level);

    // Format message — first 19 chars are "HH:MM:SS [LEVEL] ", strip them
    // for a cleaner display since we already show a separate time column.
    // Actually, the formatted message already includes timestamp + level,
    // so we display it as-is.
    var msg = data.message || '';
    line.innerHTML =
      '<span class="' + colorClass + '">' + escapeHtml(msg) + '</span>';
    logContainer.appendChild(line);

    // Auto-scroll: only if user is already near the bottom
    autoScroll();
  }

  /* ── Auto-scroll logic ── */
  var autoScrollEnabled = true;

  function isNearBottom() {
    return (logContainer.scrollHeight - logContainer.scrollTop - logContainer.clientHeight) < 50;
  }

  function autoScroll() {
    if (autoScrollEnabled) {
      logContainer.scrollTop = logContainer.scrollHeight;
    }
  }

  // Detect manual scroll — if user scrolls up, pause auto-scroll
  logContainer.addEventListener('scroll', function() {
    if (isNearBottom()) {
      autoScrollEnabled = true;
      document.getElementById('pause-indicator').classList.add('hidden');
      document.getElementById('live-indicator').classList.remove('hidden');
    } else {
      autoScrollEnabled = false;
      document.getElementById('pause-indicator').classList.remove('hidden');
      document.getElementById('live-indicator').classList.add('hidden');
    }
  });

  /* ── Log filter buttons ── */
  document.querySelectorAll('.filter-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.querySelectorAll('.filter-btn').forEach(function(b) {
        b.className = b.className.replace(/bg-blue-600 text-white/g, '') +
          ' text-gray-400 hover:text-white transition-colors';
      });
      this.className = 'px-3 py-1 text-xs rounded-[6px] bg-blue-600 text-white';
      var filter = this.dataset.filter;
      document.querySelectorAll('.log-line').forEach(function(line) {
        if (filter === 'all') {
          line.style.display = '';
        } else {
          var lvl = line.getAttribute('data-level');
          line.style.display = (lvl === filter) ? '' : 'none';
        }
      });
    });
  });

  function escapeHtml(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  /* ── Lifecycle ── */
  document.addEventListener('DOMContentLoaded', function() {
    connectLogSSE();
  });

  window.addEventListener('beforeunload', function() {
    if (evtSource) { evtSource.close(); evtSource = null; }
  });
</script>
{% endblock %}
```

---

### Task 5: `web/templates/base.html` — 加侧边栏入口

- **文件**: `web/templates/base.html`
- **改动**: 在内容检查 nav 项后面、登录管理前面，插入"全局日志"导航链接

**Before** (lines 57–63):

```html
      <a href="/check" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'check' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        内容检查
      </a>
      <a href="/auth" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'auth' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
```

**After** (插入"全局日志" nav 项):

```html
      <a href="/check" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'check' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        内容检查
      </a>
      <a href="/logs" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'logs' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 6h16M4 12h16M4 18h12"/></svg>
        全局日志
      </a>
      <a href="/auth" class="flex items-center gap-3 px-3 py-2 rounded-[8px] text-sm transition-colors {% if active_nav == 'auth' %}bg-[var(--color-primary)]/10 text-[var(--color-primary)] font-medium{% else %}text-[var(--text-secondary)] hover:bg-black/5 dark:hover:bg-white/5 hover:text-[var(--text-primary)]{% endif %}">
```

---

## 验证清单

- [ ] `uv run ruff check .` — lint 无新增问题
- [ ] `uv run pyright .` — type check 无新增 error（重点检查 `web/logging_bridge.py` 和 `web/routes/logs.py` 的 type hint）
- [ ] `uv run pytest -x` — 已有测试全部通过
- [ ] 启动服务后 `curl http://localhost:8000/logs` 返回 HTTP 200
- [ ] `curl -N http://localhost:8000/logs/stream` 能拿到 SSE 事件（触发一些 `logger.info()` 调用后可看到日志行）
- [ ] 打开浏览器 /logs 页面，确认终端样式渲染正确
- [ ] 验证级别过滤器每个档位都能正确显示/隐藏日志行
- [ ] 验证向上滚动后自动暂停（LIVE 变 ⏸ 暂停），滚回底部后恢复
- [ ] 验证清空按钮只清除前端 DOM，不影响后端队列
- [ ] 验证页面刷新后能回放最近的日志历史

## 风险

1. **QueueFull 异常**: `put_nowait` 在 queue 满时会抛 `asyncio.QueueFull`。已处理：LogBus.publish() 捕获后移除慢消费者。
2. **uvicorn.access 日志噪音**: 所有模块的日志都会进来，包括 uvicorn 自身的 HTTP access log。已处理：QueueLogHandler 通过 Filter 过滤掉 `uvicorn.access` logger。
3. **并发修改 subscribers**: fan-out 时 iterate `list(self._subscribers)`（拷贝迭代），避免在 iterate 中被 unsubscribe 修改。
4. **logging 递归**: emit() 内的格式化或 publish 操作本身可能触发 log。已处理：整个 emit 体包在 try/except 中，异常时调 `self.handleError(record)`。

## Revision History
- 2026-06-16: 修复 oracle review 4 个问题（app.py Before 同步、补 import logging、type→level、LogBus 加锁）
