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

    emit() must be called from the event loop thread (single-worker uvicorn);
    asyncio.Queue is not thread-safe. The LogBus uses an internal RLock only to
    serialise subscribe/unsubscribe/publish against concurrent *synchronous*
    callers within the same loop iteration — it does NOT make cross-thread
    put_nowait safe.
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

    # Ensure root logger level matches handler level; otherwise INFO records
    # are dropped before reaching the handler (root defaults to WARNING).
    root.setLevel(level)
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
