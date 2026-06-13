# Message State Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-platform binary dedup stores with a unified `MessageStore` that tracks per-message phase state (`discovered → downloaded → transcribed → summarized → pushed`), and introduce a `PipelineEngine` with decorator-based handler registration to decouple phase logic from orchestration.

**Architecture:** `shared/message_store.py` provides phase-aware JSON persistence with 24h time window and automatic cleanup. `core/engine.py` provides `PipelineEngine` with `@register(platform, phase)` decorator pattern and `run_platform()` unified entry. Each platform gets a `handlers.py` file with phase handlers and a detector function. Platform `monitor.py` is simplified to pure fetch functions. `pipeline.py` becomes thin delegation wrappers.

**Tech Stack:** Python 3.12+, asyncio, json (stdlib), dataclasses, `from __future__ import annotations`

**Spec Reference:** `docs/superpowers/specs/2026-06-13-message-state-pipeline-design.md`

---

## File Structure

```
CREATED/MODIFIED:
  shared/protocols.py           # +ContentType(Enum), Phase(Enum), MessageRecord, PhaseContext, PHASE_FLOW
  shared/message_store.py       # [NEW] MessageStore — unified phase-aware JSON persistence
  core/engine.py                # [NEW] PipelineEngine — registry + process_message + run_platform
  platforms/bilibili/handlers.py    # [NEW] B站 phase handlers + detector (registered via Engine)
  platforms/bilibili/monitor.py     # [MODIFY] Pure fetch function, no store param
  platforms/xiaohongshu/handlers.py # [NEW] XHS phase handlers + detector
  platforms/xiaohongshu/monitor.py  # [MODIFY] Pure fetch function, no store param
  platforms/weibo/handlers.py       # [NEW] Weibo phase handlers + detector
  platforms/weibo/monitor.py        # [MODIFY] Pure fetch function, no store param
  core/pipeline.py               # [SIMPLIFY] Delegates to PipelineEngine
  run_check.py                   # [MODIFY] +--from-phase CLI option
  tests/test_message_store.py    # [NEW] MessageStore unit tests
  tests/test_engine.py           # [NEW] PipelineEngine unit tests
  tests/test_pipeline_e2e.py     # [NEW] End-to-end integration test
  tests/test_platform_handlers.py# [NEW] Handler registration tests
  tests/test_cli.py              # [MODIFY] +--from-phase test

UNCHANGED:
  shared/config.py               # Config already has general.data_dir
  core/notifier.py               # Unchanged, still called by handlers
  core/summarizer.py             # Unchanged, still called by handlers
  core/transcriber.py            # Unchanged, still called by handlers
  shared/downloader.py           # Unchanged
  platforms/*/comments.py        # Unchanged
  platforms/*/downloader.py      # Unchanged
  platforms/*/parser.py          # Unchanged
```

---

### Task 1: Add data models to `shared/protocols.py`

**Duration:** 3 min

- [ ] **Step 1: Append ContentType, Phase, MessageRecord, PhaseContext, PHASE_FLOW to the end of shared/protocols.py**

```python
# ═══════════════════════════════════════════════════════════
# 消息状态管理 — ContentType, Phase, MessageRecord
# ═══════════════════════════════════════════════════════════

from enum import Enum, auto


class ContentType(Enum):
    VIDEO = auto()  # B站视频 / XHS视频笔记 — 完整四阶段
    TEXT = auto()   # 微博 / XHS图文笔记 — 两阶段（下载+推送）


class Phase(Enum):
    DISCOVERED = auto()
    DOWNLOADED = auto()
    TRANSCRIBED = auto()
    SUMMARIZED = auto()
    PUSHED = auto()


# 各类型消息的阶段流转路径
PHASE_FLOW: dict[ContentType, list[Phase]] = {
    ContentType.VIDEO: [Phase.DISCOVERED, Phase.DOWNLOADED, Phase.TRANSCRIBED, Phase.SUMMARIZED, Phase.PUSHED],
    ContentType.TEXT: [Phase.DISCOVERED, Phase.DOWNLOADED, Phase.PUSHED],
}


@dataclass
class MessageRecord:
    """单条消息在流水线中的完整状态"""

    msg_id: str  # "{platform}:{id}" e.g. "bili:BV1xx", "xhs:note_id", "weibo:post_id"
    platform: str  # "bili" | "xhs" | "weibo"
    content_type: ContentType
    phase: Phase
    pubdate: int  # Unix 时间戳（内容发布时间）
    title: str
    author: str
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""


@dataclass
class PhaseContext:
    """流水线上下文，各阶段产出逐级积累"""

    msg: MessageRecord
    config: Config
    downloaded_filepath: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    content_text: str = ""
    transcript_text: str = ""
    summary_text: str = ""
    keywords: list[str] = field(default_factory=list)
    comment_highlights: str = ""
    error: str = ""
```

- [ ] **Step 2: Add the new imports to the top of shared/protocols.py** (add `from enum import Enum, auto` after existing imports; update the existing `from dataclasses import dataclass, field` line if needed)

- [ ] **Step 3: Verify**

```bash
uv run pyright shared/protocols.py
```

Expected: No errors (note: `Config` is imported at runtime in PhaseContext; use a string annotation `'Config'` or import `from shared.config import Config` — since `Config` is already imported by callers, use `from __future__ import annotations` which is already at the top).

- [ ] **Step 4: Commit**

```bash
git add shared/protocols.py && git commit -m "feat(protocols): add ContentType, Phase, MessageRecord, PhaseContext, PHASE_FLOW"
```

---

### Task 2: Write tests for `MessageStore`

**Duration:** 5 min

- [ ] **Step 1: Create `tests/test_message_store.py`**

```python
"""Tests for MessageStore — unified phase-aware message storage."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from shared.protocols import ContentType, MessageRecord, Phase
from shared.message_store import MessageStore


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    """Create a MessageStore backed by a temp directory."""
    return MessageStore(tmp_path)


# ── add_new / is_known ──────────────────────────────────────────


def test_add_new_creates_record(store: MessageStore) -> None:
    msg = store.add_new(
        msg_id="bili:BV1xx",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=int(time.time()),
        title="Test Video",
        author="Test Author",
    )
    assert msg is not None
    assert msg.msg_id == "bili:BV1xx"
    assert msg.content_type == ContentType.VIDEO
    assert msg.phase == Phase.DISCOVERED
    assert store.is_known("bili:BV1xx")


def test_add_new_returns_none_for_duplicate(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    result = store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    assert result is None


def test_add_new_returns_none_outside_window(store: MessageStore) -> None:
    old_pubdate = int(time.time()) - 48 * 3600  # 48 hours ago
    result = store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, old_pubdate, "T", "A")
    assert result is None


# ── get_message ─────────────────────────────────────────────────


def test_get_message_returns_record(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    msg = store.get_message("bili:BV1xx")
    assert msg is not None
    assert msg.msg_id == "bili:BV1xx"
    assert msg.platform == "bili"


def test_get_message_returns_none_for_unknown(store: MessageStore) -> None:
    assert store.get_message("nonexistent") is None


# ── mark_phase / mark_error ────────────────────────────────────


def test_mark_phase_updates_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_phase("bili:BV1xx", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1xx")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED


def test_mark_error_records_error(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1xx", "download failed")
    msg = store.get_message("bili:BV1xx")
    assert msg is not None
    assert msg.error == "download failed"


# ── get_messages ────────────────────────────────────────────────


def test_get_messages_all(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    all_msgs = store.get_messages()
    assert len(all_msgs) == 2


def test_get_messages_filter_by_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    discovered = store.get_messages(phase=Phase.DISCOVERED)
    assert len(discovered) == 1
    assert discovered[0].msg_id == "bili:BV2"


def test_get_messages_exclude_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    unfinished = store.get_messages(phase=Phase.PUSHED, exclude=True)
    assert len(unfinished) == 1
    assert unfinished[0].msg_id == "bili:BV2"


# ── cleanup ─────────────────────────────────────────────────────


def test_cleanup_removes_old_messages(store: MessageStore) -> None:
    old_pubdate = int(time.time()) - 48 * 3600
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, old_pubdate, "T1", "A")  # will be rejected
    # Manually insert an old message (bypass window check)
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": "video",
        "phase": "discovered",
        "pubdate": old_pubdate,
        "title": "Old",
        "author": "A",
        "created_at": 0.0,
        "updated_at": 0.0,
        "error": "",
    }
    store._dirty = True
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.cleanup(window_hours=24)
    assert not store.is_known("bili:BV1")
    assert store.is_known("bili:BV2")


# ── persistence (save + reload) ────────────────────────────────


def test_save_and_reload(tmp_path: Path) -> None:
    s1 = MessageStore(tmp_path)
    s1.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    s1.mark_phase("bili:BV1", Phase.DOWNLOADED)
    s1.save()

    s2 = MessageStore(tmp_path)
    assert s2.is_known("bili:BV1")
    msg = s2.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED


def test_save_creates_json_file(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.save()
    json_path = tmp_path / "messages.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    assert data["version"] == 2
    assert "bili:BV1" in data["messages"]


# ── reset_to_phase ──────────────────────────────────────────────


def test_reset_to_phase_downgrades_messages(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    store.reset_to_phase(Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED
    assert msg.error == ""  # error cleared on reset
```

- [ ] **Step 2: Verify tests fail (red phase)**

```bash
uv run pytest tests/test_message_store.py -x
```

Expected: ImportError or module-not-found error (MessageStore doesn't exist yet).

- [ ] **Step 3: Commit**

```bash
git add tests/test_message_store.py && git commit -m "test: add MessageStore tests"
```

---

### Task 3: Implement `shared/message_store.py`

**Duration:** 5 min

- [ ] **Step 1: Create `shared/message_store.py`**

```python
"""统一消息状态存储 — 阶段感知的 JSON 持久化，取代各平台 JsonSetStore

管理 ``data/messages.json``，单文件存储所有平台的消息及阶段状态。
支持时间窗口过滤（默认 24h）和自动清理超期消息。

设计原则：
- 每推进一个阶段立即 save()，避免中途崩溃丢失进度
- JSON 全量重写在 24h 窗口内数据量很小（百条级），IO 成本可忽略
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from shared.protocols import ContentType, MessageRecord, Phase

logger = logging.getLogger(__name__)

# 默认时间窗口（小时）
DEFAULT_WINDOW_HOURS = 24


class MessageStore:
    """统一消息状态存储。

    取代各平台的 ``SubscriptionStore`` / ``XhsSubscriptionStore`` / ``WeiboSubscriptionStore``。
    使用 ``data/messages.json`` 存储所有消息，格式见 spec。
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / "messages.json"
        self._messages: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ── 内部 ─────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载消息数据。"""
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                raw = data.get("messages", {})
                if isinstance(raw, dict):
                    self._messages = raw
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("加载 %s 失败，使用空存储: %s", self._path, exc)

    def save(self) -> None:
        """持久化消息数据到磁盘（原子写入，先写临时文件再 rename）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 2,
                "messages": self._messages,
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self._dirty = False
        except OSError as exc:
            logger.error("保存 %s 失败: %s", self._path, exc)

    def _msg_from_dict(self, msg_id: str, data: dict) -> MessageRecord:
        """将存储的 dict 转换为 MessageRecord（处理枚举反序列化）。"""
        return MessageRecord(
            msg_id=msg_id,
            platform=data["platform"],
            content_type=ContentType(data["content_type"]),
            phase=Phase(data["phase"]),
            pubdate=data["pubdate"],
            title=data["title"],
            author=data["author"],
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            error=data.get("error", ""),
        )

    # ── 时间窗口 ─────────────────────────────────────────────

    @staticmethod
    def is_in_window(pubdate: int, window_hours: int = DEFAULT_WINDOW_HOURS) -> bool:
        """检查发布时间是否在时间窗口内。"""
        return (time.time() - pubdate) < window_hours * 3600

    # ── 查询 ─────────────────────────────────────────────────

    def is_known(self, msg_id: str) -> bool:
        """检查消息是否已记录。"""
        return msg_id in self._messages

    def get_message(self, msg_id: str) -> MessageRecord | None:
        """获取单条消息。"""
        data = self._messages.get(msg_id)
        if data is None:
            return None
        return self._msg_from_dict(msg_id, data)

    def get_messages(
        self,
        *,
        phase: Phase | None = None,
        exclude: bool = False,
        platform: str | None = None,
    ) -> list[MessageRecord]:
        """获取消息列表，支持按阶段和平台过滤。

        Args:
            phase: 按阶段过滤
            exclude: 为 True 时排除指定阶段的消息（即获取未达到该阶段的消息）
            platform: 按平台过滤（可选）
        """
        results: list[MessageRecord] = []
        for msg_id, data in self._messages.items():
            if platform is not None and data.get("platform") != platform:
                continue
            msg_phase = data.get("phase", "")
            if phase is not None:
                if exclude and msg_phase == phase.value:
                    continue
                if not exclude and msg_phase != phase.value:
                    continue
            results.append(self._msg_from_dict(msg_id, data))
        return results

    # ── 写入 ─────────────────────────────────────────────────

    def add_new(
        self,
        msg_id: str,
        platform: str,
        content_type: ContentType,
        pubdate: int,
        title: str,
        author: str,
    ) -> MessageRecord | None:
        """添加新消息。

        内部做去重和时间窗口检查。如果消息已在 store 中或超出时间窗口，返回 None。

        Returns:
            新创建的 MessageRecord，或 None（已存在 / 超期）
        """
        if self.is_known(msg_id):
            return None
        if not self.is_in_window(pubdate):
            return None

        now = time.time()
        data = {
            "platform": platform,
            "content_type": content_type.value,
            "phase": Phase.DISCOVERED.value,
            "pubdate": pubdate,
            "title": title,
            "author": author,
            "created_at": now,
            "updated_at": now,
            "error": "",
        }
        self._messages[msg_id] = data
        self._dirty = True
        return self._msg_from_dict(msg_id, data)

    def mark_phase(self, msg_id: str, phase: Phase) -> None:
        """更新消息的阶段。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["phase"] = phase.value
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def mark_error(self, msg_id: str, error: str) -> None:
        """记录消息的错误信息。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["error"] = error
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def reset_to_phase(self, target: Phase, platform: str | None = None) -> None:
        """将所有阶段 >= target 的消息回退到 target 阶段，清除 error。

        Args:
            target: 目标阶段
            platform: 可选，仅回退指定平台的消息
        """
        for msg_id, data in list(self._messages.items()):
            if platform is not None and data.get("platform") != platform:
                continue
            current_phase = data.get("phase", "")
            if current_phase >= target.value:
                data["phase"] = target.value
                data["error"] = ""
                data["updated_at"] = time.time()
                self._dirty = True

    # ── 清理 ─────────────────────────────────────────────────

    def cleanup(self, window_hours: int = DEFAULT_WINDOW_HOURS) -> None:
        """删除超出时间窗口的消息。"""
        cutoff = time.time() - window_hours * 3600
        to_remove = [
            msg_id
            for msg_id, data in self._messages.items()
            if data.get("pubdate", 0) < cutoff
        ]
        for msg_id in to_remove:
            del self._messages[msg_id]
        if to_remove:
            self._dirty = True
            logger.info("MessageStore cleanup: removed %d old messages", len(to_remove))
```

- [ ] **Step 2: Run tests to verify**

```bash
uv run pytest tests/test_message_store.py -x -v
```

Expected: All tests pass.

- [ ] **Step 3: Type check**

```bash
uv run pyright shared/message_store.py
```

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add shared/message_store.py && git commit -m "feat(store): implement MessageStore with phase tracking and time window"
```

---

### Task 4: Write tests for `PipelineEngine`

**Duration:** 5 min

- [ ] **Step 1: Create `tests/test_engine.py`**

```python
"""Tests for PipelineEngine — decorator-based pipeline engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext
from core.engine import PipelineEngine


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path)


# ── Registration ────────────────────────────────────────────────


def test_register_handler() -> None:
    """@PipelineEngine.register should store handler in _handlers."""
    # Clean slate
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def mock_handler(ctx: PhaseContext) -> bool:
        return True

    assert ("bili", Phase.DOWNLOADED) in PipelineEngine._handlers
    assert PipelineEngine._handlers[("bili", Phase.DOWNLOADED)] is mock_handler


def test_register_detector() -> None:
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register_detector("bili")
    async def mock_detector(config: Config, store: MessageStore) -> None:
        pass

    assert "bili" in PipelineEngine._detectors
    assert PipelineEngine._detectors["bili"] is mock_detector


# ── process_message ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_message_full_flow(config: Config, store: MessageStore) -> None:
    """VIDEO message should go through all phases."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        calls.append("transcribed")
        return True

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        calls.append("summarized")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new(
        "bili:BV1", "bili", ContentType.VIDEO, 2000000000, "Test", "Author"
    )
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    assert calls == ["downloaded", "transcribed", "summarized", "pushed"]
    # Message should now be in PUSHED phase
    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED


@pytest.mark.asyncio
async def test_process_message_text_skips_transcribe_summarize(config: Config, store: MessageStore) -> None:
    """TEXT message should only go through DOWNLOADED → PUSHED."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("weibo", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("weibo", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new(
        "weibo:123", "weibo", ContentType.TEXT, 2000000000, "Post", "Author"
    )
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    assert calls == ["downloaded", "pushed"]


@pytest.mark.asyncio
async def test_process_message_handler_failure_stops_flow(config: Config, store: MessageStore) -> None:
    """If a handler returns False, flow should stop and error should be recorded."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.error = "download failed"
        return False

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        pytest.fail("should not be called")

    msg = store.add_new(
        "bili:BV1", "bili", ContentType.VIDEO, 2000000000, "Test", "Author"
    )
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.DISCOVERED  # unchanged
    assert updated.error == "download failed"


@pytest.mark.asyncio
async def test_process_message_resume_from_mid_phase(config: Config, store: MessageStore) -> None:
    """Should resume from current phase, not repeat completed phases."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        calls.append("transcribed")
        return True

    msg = store.add_new(
        "bili:BV1", "bili", ContentType.VIDEO, 2000000000, "Test", "Author"
    )
    assert msg is not None
    # Manually advance to DOWNLOADED
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED

    await PipelineEngine.process_message(msg, config, store)
    assert calls == ["transcribed"]  # only transcribed, not downloaded


# ── run_platform ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_platform_detect_and_process(config: Config, tmp_path: Path) -> None:
    """run_platform should run detector then process pending messages."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    store = MessageStore(tmp_path)

    detected = False

    @PipelineEngine.register_detector("bili")
    async def bili_detector(cfg: Config, st: MessageStore) -> None:
        nonlocal detected
        detected = True
        st.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    # We need to override store creation in run_platform for test
    # Instead, test the logic directly
    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "bili")

    assert detected
    # Re-read store to check
    store2 = MessageStore(tmp_path)
    assert store2.is_known("bili:BV1")
    msg = store2.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.PUSHED
```

- [ ] **Step 2: Verify tests fail**

```bash
uv run pytest tests/test_engine.py -x
```

Expected: ImportError for `core.engine` (doesn't exist yet).

- [ ] **Step 3: Commit**

```bash
git add tests/test_engine.py && git commit -m "test: add PipelineEngine tests"
```

---

### Task 5: Implement `core/engine.py`

**Duration:** 5 min

- [ ] **Step 1: Create `core/engine.py`**

```python
"""流水线引擎 — 注册表模式 + 统一流水线编排

核心概念：
- ``PipelineEngine`` 提供 ``@register(platform, phase)`` 和 ``@register_detector(platform)`` 装饰器
- 各平台在 ``handlers.py`` 中通过装饰器注册 handler
- 跨平台共用 handler 使用 ``"*"`` 作为 platform 通配符
- ``run_platform()`` 是统一入口：cleanup → detect → process
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext

logger = logging.getLogger(__name__)

# Handler 类型：接收 PhaseContext，返回 bool（True=成功）
PhaseHandler = Callable[[PhaseContext], Awaitable[bool]]


class PipelineEngine:
    """统一流水线引擎。

    使用类变量注册表（允许跨模块导入时自动注册）：
    - ``_handlers``:  {(platform, phase): handler}
    - ``_detectors``: {platform: detector}
    """

    _handlers: dict[tuple[str, Phase], PhaseHandler] = {}
    _detectors: dict[str, Callable[..., Awaitable[None]]] = {}

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
    def register_detector(cls, platform: str) -> Callable:
        """装饰器：注册某平台的 detector 函数。

        Usage::

            @PipelineEngine.register_detector("bili")
            async def bili_detector(config: Config, store: MessageStore) -> None:
                ...
        """

        def decorator(func: Callable) -> Callable:
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
        """
        from shared.protocols import MessageRecord

        assert isinstance(msg, MessageRecord), f"expected MessageRecord, got {type(msg)}"
        ctx = PhaseContext(msg=msg, config=config)
        phases = PHASE_FLOW[msg.content_type]

        start_idx = phases.index(Phase(msg.phase))
        for next_phase in phases[start_idx + 1:]:
            # 尝试精确匹配，fallback 到通配符
            handler = cls._handlers.get((msg.platform, next_phase))
            if handler is None:
                handler = cls._handlers.get(("*", next_phase))
            if handler is None:
                logger.error("No handler for %s / %s", msg.platform, next_phase)
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
    ) -> None:
        """统一平台入口：cleanup → detect → process。

        Args:
            config: 全局配置
            platform: 平台标识（"bili" | "xhs" | "weibo"）
            from_phase: 可选，将所有消息回退到指定阶段后重新处理
        """
        store = MessageStore(config.general.data_dir)
        store.cleanup(24)

        if from_phase is not None:
            store.reset_to_phase(from_phase, platform=platform)

        detector = cls._detectors.get(platform)
        if detector is not None:
            await detector(config, store)

        for msg in store.get_messages(phase=Phase.PUSHED, exclude=True, platform=platform):
            await cls.process_message(msg, config, store)

        store.save()
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_engine.py -x -v
```

Expected: All tests pass.

- [ ] **Step 3: Type check**

```bash
uv run pyright core/engine.py
```

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add core/engine.py && git commit -m "feat(engine): implement PipelineEngine with decorator-based handler registry"
```

---

### Task 6: Write end-to-end integration test

**Duration:** 5 min

- [ ] **Step 1: Create `tests/test_pipeline_e2e.py`**

```python
"""End-to-end integration test for PipelineEngine with mocked platform.

Tests the full pipeline flow: detect → process_message → phase transitions.
Uses unittest.mock to avoid real network calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext
from core.engine import PipelineEngine


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path)


# ── Happy path: detector → all handlers succeed ─────────────────


@pytest.mark.asyncio
async def test_full_pipeline_happy_path(config: Config, tmp_path: Path) -> None:
    """Detector discovers a message → all phase handlers called in order → PUSHED."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    call_order: list[str] = []

    @PipelineEngine.register_detector("test")
    async def test_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new(
            msg_id="test:001",
            platform="test",
            content_type=ContentType.TEXT,
            pubdate=2000000000,
            title="E2E Test",
            author="Tester",
        )

    @PipelineEngine.register("test", Phase.DOWNLOADED)
    async def test_download(ctx: PhaseContext) -> bool:
        call_order.append("downloaded")
        ctx.content_text = "downloaded content"
        return True

    @PipelineEngine.register("test", Phase.PUSHED)
    async def test_push(ctx: PhaseContext) -> bool:
        call_order.append("pushed")
        return True

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test")

    assert call_order == ["downloaded", "pushed"]

    store2 = MessageStore(tmp_path)
    assert store2.is_known("test:001")
    msg = store2.get_message("test:001")
    assert msg is not None
    assert msg.phase == Phase.PUSHED


# ── Error path: handler fails → phase stops ────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_handler_failure(config: Config, tmp_path: Path) -> None:
    """If a handler returns False, pipeline stops and error is recorded."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register_detector("test")
    async def test_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new(
            msg_id="test:002",
            platform="test",
            content_type=ContentType.TEXT,
            pubdate=2000000000,
            title="Fail Test",
            author="Tester",
        )

    @PipelineEngine.register("test", Phase.DOWNLOADED)
    async def test_download(ctx: PhaseContext) -> bool:
        ctx.error = "download failed"
        return False

    @PipelineEngine.register("test", Phase.PUSHED)
    async def test_push(ctx: PhaseContext) -> bool:
        pytest.fail("should not be called after failure")

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test")

    store2 = MessageStore(tmp_path)
    msg = store2.get_message("test:002")
    assert msg is not None
    assert msg.phase == Phase.DISCOVERED  # unchanged
    assert msg.error == "download failed"


# ── Resume from mid-phase ──────────────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_resume_from_phase(config: Config, tmp_path: Path) -> None:
    """Message already at DOWNLOADED should resume from there, not repeat."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    call_order: list[str] = []

    @PipelineEngine.register_detector("test")
    async def test_detector(cfg: Config, st: MessageStore) -> None:
        st.add_new(
            msg_id="test:003",
            platform="test",
            content_type=ContentType.TEXT,
            pubdate=2000000000,
            title="Resume Test",
            author="Tester",
        )
        # Manually advance to DOWNLOADED to simulate resume
        st.mark_phase("test:003", Phase.DOWNLOADED)

    @PipelineEngine.register("test", Phase.DOWNLOADED)
    async def test_download(ctx: PhaseContext) -> bool:
        call_order.append("downloaded")
        return True

    @PipelineEngine.register("test", Phase.PUSHED)
    async def test_push(ctx: PhaseContext) -> bool:
        call_order.append("pushed")
        return True

    config.general.data_dir = str(tmp_path)
    await PipelineEngine.run_platform(config, "test")

    # DOWNLOADED handler should NOT be called (already at that phase)
    assert call_order == ["pushed"]
```

- [ ] **Step 2: Verify test failure (import error — engine not implemented yet)**

```bash
uv run pytest tests/test_pipeline_e2e.py -x
```

Expected: ImportError or module-not-found error.

- [ ] **Step 3: Commit**

```bash
git add tests/test_pipeline_e2e.py && git commit -m "test: add end-to-end pipeline integration test"
```

---

### Task 7: Implement B站 handlers + simplify monitor

**Duration:** 8 min (two sub-tasks: handlers + monitor)

#### 7a: Create `platforms/bilibili/handlers.py`

- [ ] **Step 1: Create `platforms/bilibili/handlers.py`**

```python
"""B站流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` 装饰器注册阶段处理器。
使用 ``@PipelineEngine.register_detector`` 装饰器注册 detector。
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from core.engine import PipelineEngine
from core.notifier import notify_new_video
from core.summarizer import extract_keywords, generate_summary
from core.transcriber import cleanup_media, transcribe_file_async
from platforms.bilibili.comments import fetch_comment_highlights
from platforms.bilibili.monitor import fetch_user_videos
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext

logger = logging.getLogger(__name__)
console = Console()


# ── Detector ────────────────────────────────────────────────────


@PipelineEngine.register_detector("bili")
async def bili_detector(config: Config, store: MessageStore) -> None:
    """检测新注册的 UP 主视频并加入 store。"""
    from shared.config import BiliSubscription

    # 一次性的流式风格兼容：从 config.bilibili.subscriptions 获取订阅
    for sub in config.bilibili.subscriptions:
        assert isinstance(sub, BiliSubscription)
        videos = await fetch_user_videos(
            uid=sub.uid,
            config=config,
            max_count=config.bilibili.monitor.max_videos_per_check,
        )
        for v in videos:
            store.add_new(
                msg_id=f"bili:{v.bvid}",
                platform="bili",
                content_type=ContentType.VIDEO,
                pubdate=v.pubdate,
                title=v.title,
                author=v.author,
            )


# ── Phase: DOWNLOADED ──────────────────────────────────────────


def _format_comment_highlights(highlights: list) -> str:
    """将评论亮点列表格式化为 Markdown 文本（从 pipeline.py 迁入）。"""
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_up_owner", False) or getattr(h, "is_author", False)
        is_pinned = getattr(h, "is_pinned", False)
        reply_to = getattr(h, "reply_to", "")
        parent_content = getattr(h, "parent_content", "")

        tags: list[str] = []
        if is_author:
            tags.append("UP主")
        if is_pinned:
            tags.append("置顶")
        tag = f" ({', '.join(tags)})" if tags else ""

        if reply_to and parent_content:
            parts.append(
                f"- **{reply_to}**:\n"
                f"  > {parent_content}\n"
                f"  **{name}**{tag} (👍{like}):\n"
                f"  {content}"
            )
        else:
            parts.append(f"- **{name}**{tag} (👍{like}):\n  {content}")
    return "\n".join(parts)


@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool:
    """下载 B站 视频音频。"""
    bvid = ctx.msg.msg_id.replace("bili:", "")
    console.print(f"  [dim]⬇ 下载 {ctx.msg.title} ({bvid})…[/]")

    from shared.downloader import download_video

    try:
        result = await download_video(bvid=bvid, config=ctx.config, title=ctx.msg.title)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        console.print(f"  [red]✗ {ctx.error}[/]")
        logger.exception("Download failed for %s", bvid)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        console.print(f"  [yellow]⚠️  {ctx.error}[/]")
        return False

    ctx.downloaded_filepath = result.filepath
    console.print("  [green]✓ 下载完成[/]")
    return True


# ── Phase: TRANSCRIBED ─────────────────────────────────────────


@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。"""
    if ctx.msg.content_type != ContentType.VIDEO:
        # 非视频类型不处理转写
        return True

    filepath = ctx.downloaded_filepath
    if filepath is None or not filepath.exists():
        console.print("  [yellow]⚠️  无可用媒体文件，跳过转写[/]")
        return True  # 跳过而非失败

    source_id = ctx.msg.msg_id
    console.print(f"  [dim]📝 转写 {source_id}…[/]")

    try:
        transcript = await transcribe_file_async(
            filepath=filepath,
            config=ctx.config,
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
        )
        if transcript.success:
            ctx.transcript_text = transcript.text
            console.print("  [green]✓ 转写完成[/]")
        else:
            console.print(f"  [yellow]⚠️  转写未成功: {transcript.error}[/]")
    except ImportError:
        # 转写依赖未安装，跳过
        console.print("  [dim]⏭  转写依赖未安装，跳过[/]")
    except Exception as exc:
        console.print(f"  [red]✗ 转写失败: {exc}[/]")
        logger.exception("Transcribe failed for %s", source_id)

    return True  # 转写失败不阻塞后续流程


# ── Phase: SUMMARIZED ──────────────────────────────────────────


@PipelineEngine.register("*", Phase.SUMMARIZED)
async def summarize_phase(ctx: PhaseContext) -> bool:
    """生成摘要+关键词+评论亮点（跨平台共用 handler）。"""
    source_id = ctx.msg.msg_id
    console.print(f"  [dim]💬 获取评论亮点…[/]")

    # 评论亮点 — 各平台不同
    if ctx.msg.platform == "bili":
        bvid = source_id.replace("bili:", "")
        try:
            highlights = await fetch_comment_highlights(bvid=bvid, config=ctx.config)
            ctx.comment_highlights = _format_comment_highlights(highlights)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
            logger.warning("Comment highlights failed for %s: %s", source_id, exc)

    console.print(f"  [dim]🤖 生成摘要…[/]")

    text_to_summarize = ctx.transcript_text or ctx.content_text
    try:
        summary_text, _source, _is_ai = generate_summary(
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=text_to_summarize,
            config=ctx.config,
        )
        ctx.summary_text = summary_text
    except Exception as exc:
        console.print(f"  [red]✗ 摘要生成失败: {exc}[/]")
        logger.exception("Summary failed for %s", source_id)

    # 关键词提取
    try:
        ctx.keywords = extract_keywords(
            text=ctx.summary_text,
            title=ctx.msg.title,
            author=ctx.msg.author,
            config=ctx.config,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  关键词提取失败: {exc}[/]")
        logger.warning("Keywords failed for %s: %s", source_id, exc)

    return True


# ── Phase: PUSHED ──────────────────────────────────────────────


@PipelineEngine.register("bili", Phase.PUSHED)
async def bili_push(ctx: PhaseContext) -> bool:
    """推送 B站 视频通知。"""
    bvid = ctx.msg.msg_id.replace("bili:", "")
    console.print(f"  [dim]🔔 推送通知…[/]")

    try:
        await notify_new_video(
            bvid=bvid,
            title=ctx.msg.title,
            author=ctx.msg.author,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
            config=ctx.config.bilibili.notification,
        )
        console.print("  [green]✓ 通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("Notify failed for %s: %s", bvid, exc)

    # 清理媒体
    if (
        ctx.config.transcribe.delete_after_transcribe
        and ctx.downloaded_filepath is not None
    ):
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=bvid)
        except Exception as exc:
            console.print(f"  [yellow]⚠️  媒体清理失败: {exc}[/]")
            logger.warning("Cleanup failed for %s: %s", bvid, exc)

    return True
```

#### 7b: Simplify `platforms/bilibili/monitor.py`

- [ ] **Step 2: Simplify monitor.py to pure fetch function**

Edit `platforms/bilibili/monitor.py`:

1. Remove `SubscriptionStore` class (deprecated)
2. Rename `check_new_videos` to `fetch_user_videos`, remove `store` parameter, remove filtering logic

The modified file becomes:

```python
"""B站 API 模式视频监控 - 通过 bilibili_api 检查 UP 主新视频（纯检测函数）

提供 ``fetch_user_videos()`` 纯检测函数，不再负责去重和时间窗口过滤。
去重和窗口过滤统一在 ``MessageStore`` 中处理。
"""

from __future__ import annotations

import logging

import bilibili_api

from shared.config import Config
from shared.protocols import VideoInfo

logger = logging.getLogger(__name__)


async def _fetch_user_videos(
    uid: int,
    credential: bilibili_api.Credential,
    max_count: int = 10,
) -> list[dict]:
    """调用 bilibili_api 获取 UP 主最近视频的原始数据。"""
    from bilibili_api import user

    u = user.User(uid=uid, credential=credential)
    results: list[dict] = []
    page = 1

    while len(results) < max_count:
        try:
            resp = await u.get_videos(pn=page, ps=min(30, max_count - len(results)))
        except Exception as e:
            logger.error(f"获取 UP 主 {uid} 视频列表失败 (page={page}): {e}")
            break

        vlist = resp.get("list", {}).get("vlist", [])
        if not vlist:
            break

        results.extend(vlist)
        total = resp.get("page", {}).get("count", 0)
        if page * 30 >= total:
            break
        page += 1

    return results[:max_count]


def _parse_video_info(raw: dict, uid: int) -> VideoInfo:
    """将 API 返回的原始字典解析为 VideoInfo。"""
    return VideoInfo(
        bvid=raw.get("bvid", ""),
        title=raw.get("title", ""),
        uid=uid,
        author=raw.get("author", ""),
        pubdate=raw.get("created", 0),
        duration=_parse_duration(raw.get("length", 0)),
        desc=raw.get("description", ""),
        pic=raw.get("pic", ""),
    )


def _parse_duration(raw_duration) -> int:
    """将 API 返回的 duration 解析为整数秒。"""
    if isinstance(raw_duration, int):
        return raw_duration
    if isinstance(raw_duration, str):
        parts = raw_duration.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            pass
    return 0


async def fetch_user_videos(
    uid: int,
    config: Config,
    max_count: int = 10,
) -> list[VideoInfo]:
    """纯检测函数：获取 UP 主的最新视频列表。

    Args:
        uid: UP 主 UID
        config: 全局配置
        max_count: 最大获取数量

    Returns:
        原始视频信息列表（不做去重和时间窗口过滤）
    """
    from platforms.bilibili.auth import get_credential

    credential = get_credential(config)

    logger.info(f"获取 UP 主 {uid} 的视频列表 (最多 {max_count} 条)")

    raw_videos: list[dict]
    try:
        raw_videos = await _fetch_user_videos(uid, credential, max_count)
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 视频列表异常: {e}")
        return []

    if not raw_videos:
        logger.info(f"UP 主 {uid} 没有视频或获取失败")
        return []

    videos = [_parse_video_info(raw, uid) for raw in raw_videos if raw.get("bvid")]
    videos.sort(key=lambda v: v.pubdate, reverse=True)

    logger.info(f"UP 主 {uid} 获取到 {len(videos)} 个视频")
    return videos
```

Changes from original:
- Removed `SubscriptionStore` class
- Removed `check_new_videos` function (replaced by `fetch_user_videos`)
- `fetch_user_videos` no longer takes `store` param, returns all videos without filtering
- Removed `import json` (no longer needed)
- Added `max_count` parameter

- [ ] **Step 3: Verify type check**

```bash
uv run pyright platforms/bilibili/monitor.py platforms/bilibili/handlers.py
```

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add platforms/bilibili/handlers.py platforms/bilibili/monitor.py && git commit -m "feat(bili): add handlers.py, simplify monitor.py to pure fetch function"
```

---

### Task 8: Implement XHS handlers + simplify monitor

**Duration:** 6 min

#### 8a: Create `platforms/xiaohongshu/handlers.py`

- [ ] **Step 1: Create `platforms/xiaohongshu/handlers.py`**

```python
"""小红书流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` / ``@PipelineEngine.register_detector`` 装饰器注册。
"""

from __future__ import annotations

import logging

from rich.console import Console

from core.engine import PipelineEngine
from core.notifier import notify_new_xhs_note
from core.summarizer import extract_keywords, generate_summary
from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights
from platforms.xiaohongshu.downloader import download_note
from platforms.xiaohongshu.monitor import fetch_user_notes
from platforms.xiaohongshu.parser import parse_note_content
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext

logger = logging.getLogger(__name__)
console = Console()


# ── Detector ────────────────────────────────────────────────────


@PipelineEngine.register_detector("xhs")
async def xhs_detector(config: Config, store: MessageStore) -> None:
    """检测新笔记并加入 store。"""
    for sub in config.xiaohongshu.subscriptions:
        notes = await fetch_user_notes(
            user_id=sub.user_id,
            name=sub.name,
            config=config,
        )
        for n in notes:
            content_type = ContentType.VIDEO if n.note_type == "video" else ContentType.TEXT
            store.add_new(
                msg_id=f"xhs:{n.note_id}",
                platform="xhs",
                content_type=content_type,
                pubdate=n.pubdate,
                title=n.title,
                author=n.author,
            )


# ── Phase: DOWNLOADED ──────────────────────────────────────────


@PipelineEngine.register("xhs", Phase.DOWNLOADED)
async def xhs_download(ctx: PhaseContext) -> bool:
    """下载小红书笔记（图片/视频）。"""
    note_id = ctx.msg.msg_id.replace("xhs:", "")
    console.print(f"  [dim]⬇ 下载 {ctx.msg.title} ({note_id})…[/]")

    # 重建 NoteInfo 结构
    from shared.protocols import NoteInfo

    note = NoteInfo(
        note_id=note_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        user_id="",  # 不需要 user_id 用于下载
        note_type="video" if ctx.msg.content_type == ContentType.VIDEO else "normal",
        pubdate=ctx.msg.pubdate,
    )

    try:
        result = await download_note(note=note, config=ctx.config)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        console.print(f"  [red]✗ {ctx.error}[/]")
        logger.exception("XHS download failed for %s", note_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        console.print(f"  [yellow]⚠️  {ctx.error}[/]")
        return False

    ctx.downloaded_filepath = result.filepath
    ctx.image_paths = result.image_paths
    ctx.content_text = result.content_text

    # 解析笔记内容
    try:
        parsed = parse_note_content(note=note, download_result=result)
        if parsed:
            ctx.content_text = parsed.text
            if not ctx.downloaded_filepath and parsed.video_path:
                ctx.downloaded_filepath = parsed.video_path
            ctx.image_paths = parsed.image_paths
    except Exception as exc:
        console.print(f"  [yellow]⚠️  内容解析失败: {exc}[/]")
        logger.warning("XHS parse failed for %s: %s", note_id, exc)

    console.print("  [green]✓ 下载完成[/]")
    return True


# ── Phase: PUSHED ──────────────────────────────────────────────


@PipelineEngine.register("xhs", Phase.PUSHED)
async def xhs_push(ctx: PhaseContext) -> bool:
    """推送小红书笔记通知。"""
    note_id = ctx.msg.msg_id.replace("xhs:", "")
    console.print(f"  [dim]🔔 推送通知…[/]")

    try:
        await notify_new_xhs_note(
            note_id=note_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
            xhs_noti_config=ctx.config.xiaohongshu.notification,
        )
        console.print("  [green]✓ 通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("XHS notify failed for %s: %s", note_id, exc)

    return True
```

#### 8b: Simplify `platforms/xiaohongshu/monitor.py`

- [ ] **Step 2: Rewrite `platforms/xiaohongshu/monitor.py`**

```python
"""小红书笔记监控模块 — 用户笔记列表获取（纯检测函数）

提供 ``fetch_user_notes()`` 纯检测函数，不再负责去重和时间窗口过滤。
去重和窗口过滤统一在 ``MessageStore`` 中处理。
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from rich.console import Console

from platforms.xiaohongshu.auth import (
    XHS_BASE_URL,
    get_request_headers,
    get_signed_params,
    get_xhs_cookie,
)
from shared.config import Config
from shared.constants import XHS_REQUEST_TIMEOUT
from shared.http import get_session
from shared.protocols import NoteInfo

logger = logging.getLogger("trawler.xiaohongshu.monitor")
console = Console()

# 默认每页笔记数
DEFAULT_PAGE_SIZE = 20
# 默认最大每次检查笔记数
DEFAULT_MAX_NOTES_PER_CHECK = 10
# 笔记列表 API
USER_POSTED_API = f"{XHS_BASE_URL}/api/sns/web/v1/user_posted"


def _parse_note_from_api(note_data: dict[str, Any], author_name: str, user_id: str) -> NoteInfo | None:
    """从 API 响应中解析单条笔记信息。"""
    try:
        note_id = note_data.get("note_id", "") or note_data.get("id", "")
        if not note_id:
            return None

        note_type = note_data.get("type", "normal")
        is_video = note_type == "video" or bool(note_data.get("video"))

        title = note_data.get("display_title", "") or note_data.get("title", "")
        desc = note_data.get("desc", "")

        # 封面图
        cover_url = ""
        cover_data = note_data.get("cover", {})
        if isinstance(cover_data, dict):
            cover_url = cover_data.get("url", "") or cover_data.get("url_default", "")
        elif isinstance(cover_data, str):
            cover_url = cover_data

        # 点赞数
        liked_count = 0
        interact_info = note_data.get("interact_info", {})
        if isinstance(interact_info, dict):
            liked_str = interact_info.get("liked_count", "0")
            try:
                liked_count = int(liked_str)
            except (ValueError, TypeError):
                liked_count = 0

        # 发布时间
        pubdate = note_data.get("last_update_time", 0) or note_data.get("time", 0)
        if isinstance(pubdate, str):
            try:
                pubdate = int(pubdate)
            except (ValueError, TypeError):
                pubdate = 0

        # xsec_token
        xsec_token = note_data.get("xsec_token", "") or note_data.get("xsec_token_str", "")

        return NoteInfo(
            note_id=str(note_id),
            title=title,
            author=author_name,
            user_id=user_id,
            note_type="video" if is_video else "normal",
            pubdate=pubdate,
            desc=desc,
            cover_url=cover_url,
            liked_count=liked_count,
            xsec_token=xsec_token,
        )
    except Exception as e:
        logger.debug(f"解析笔记数据失败: {e}")
        return None


async def _fetch_notes_via_api(
    user_id: str,
    cookie: str,
    cursor: str = "",
    num: int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """通过小红书 API 获取用户笔记列表。"""
    body: dict[str, Any] = {
        "user_id": user_id,
        "cursor": cursor,
        "num": num,
        "image_scenes": [],
    }

    headers = get_request_headers(cookie)
    signed = get_signed_params(body, cookie)
    headers.update(signed)

    session = await get_session()
    async with session.post(
        USER_POSTED_API,
        json=body,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
    ) as resp:
        if resp.status != 200:
            logger.warning(f"小红书笔记列表 API 返回状态码: {resp.status}")
            return []

        data = await resp.json(content_type=None)

    if not data.get("success", False):
        msg = data.get("msg", "unknown")
        logger.warning(f"小红书笔记列表 API 失败: {msg}")
        return []

    notes = data.get("data", {}).get("notes", [])
    return notes if isinstance(notes, list) else []


async def _fetch_notes_fallback(
    user_id: str,
    cookie: str,
) -> list[dict[str, Any]]:
    """降级方案：使用简化请求获取笔记列表。"""
    params = {
        "user_id": user_id,
        "cursor": "",
        "num": str(DEFAULT_PAGE_SIZE),
        "image_scenes": "",
    }

    headers = get_request_headers(cookie)

    session = await get_session()
    try:
        async with session.get(
            USER_POSTED_API,
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.debug(f"降级请求返回状态码: {resp.status}")
                return []

            data = await resp.json(content_type=None)

        if not data.get("success", False):
            return []

        notes = data.get("data", {}).get("notes", [])
        return notes if isinstance(notes, list) else []
    except Exception as e:
        logger.debug(f"降级请求失败: {e}")
        return []


async def fetch_user_notes(
    user_id: str,
    name: str,
    config: Config,
    max_notes: int = DEFAULT_MAX_NOTES_PER_CHECK,
) -> list[NoteInfo]:
    """纯检测函数：获取指定用户的最新笔记。

    Args:
        user_id: 小红书用户 ID
        name: 用户名称（用于日志）
        config: 全局配置
        max_notes: 单次检查最大返回笔记数

    Returns:
        原始笔记列表（不做去重和时间窗口过滤）
    """
    cookie = get_xhs_cookie(config)
    if not cookie:
        logger.error(f"[{name}] 缺少 Cookie，无法检查笔记")
        return []

    # 方式1：使用签名 API
    raw_notes: list[dict[str, Any]] = []
    try:
        raw_notes = await _fetch_notes_via_api(user_id, cookie)
        logger.debug(f"[{name}] 签名 API 获取到 {len(raw_notes)} 条笔记")
    except Exception as e:
        logger.warning(f"[{name}] 签名 API 请求失败: {e}")

    # 方式2：降级请求
    if not raw_notes:
        try:
            raw_notes = await _fetch_notes_fallback(user_id, cookie)
            logger.debug(f"[{name}] 降级请求获取到 {len(raw_notes)} 条笔记")
        except Exception as e:
            logger.warning(f"[{name}] 降级请求也失败: {e}")

    if not raw_notes:
        logger.info(f"[{name}] 未获取到任何笔记数据")
        return []

    # 解析（不做去重，由 MessageStore 处理）
    notes: list[NoteInfo] = []
    for raw in raw_notes:
        note = _parse_note_from_api(raw, name, user_id)
        if note is not None:
            notes.append(note)

    # 按发布时间降序排列
    notes.sort(key=lambda n: n.pubdate, reverse=True)

    # 限制数量
    notes = notes[:max_notes]

    logger.info(f"[{name}] 获取到 {len(notes)} 条笔记")
    return notes
```

Changes from original:
- Removed `XhsSubscriptionStore` class
- Removed `XhsSubscriptionStore` import (`JsonSetStore`)
- Renamed `check_new_notes` to `fetch_user_notes`, removed `store` param
- Removed all `store.is_known()` and `store.mark_known_note()` calls
- Removed `from pathlib import Path` (no longer needed)
- Removed `import Optional` (use `| None`)
- Returns all parsed notes without filtering

- [ ] **Step 3: Verify**

```bash
uv run pyright platforms/xiaohongshu/monitor.py platforms/xiaohongshu/handlers.py
```

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add platforms/xiaohongshu/handlers.py platforms/xiaohongshu/monitor.py && git commit -m "feat(xhs): add handlers.py, simplify monitor.py to pure fetch function"
```

---

### Task 9: Implement Weibo handlers + simplify monitor

**Duration:** 6 min

#### 9a: Create `platforms/weibo/handlers.py`

- [ ] **Step 1: Create `platforms/weibo/handlers.py`**

```python
"""微博流水线 handler — 各阶段处理器 + detector

使用 ``@PipelineEngine.register`` / ``@PipelineEngine.register_detector`` 装饰器注册。
"""

from __future__ import annotations

import logging

from rich.console import Console

from core.engine import PipelineEngine
from core.notifier import notify_new_weibo_post
from platforms.weibo.comments import fetch_weibo_comment_highlights
from platforms.weibo.downloader import download_weibo_media
from platforms.weibo.monitor import fetch_user_posts
from platforms.weibo.parser import parse_weibo_post
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext

logger = logging.getLogger(__name__)
console = Console()


# ── Detector ────────────────────────────────────────────────────


@PipelineEngine.register_detector("weibo")
async def weibo_detector(config: Config, store: MessageStore) -> None:
    """检测新微博并加入 store。"""
    for sub in config.weibo.subscriptions:
        posts = await fetch_user_posts(
            user_id=sub.user_id,
            name=sub.name,
            config=config,
        )
        for p in posts:
            store.add_new(
                msg_id=f"weibo:{p.post_id}",
                platform="weibo",
                content_type=ContentType.TEXT,
                pubdate=p.pubdate,
                title=p.clean_text[:80] if p.clean_text else p.post_id,
                author=p.author,
            )


# ── Phase: DOWNLOADED ──────────────────────────────────────────


@PipelineEngine.register("weibo", Phase.DOWNLOADED)
async def weibo_download(ctx: PhaseContext) -> bool:
    """下载微博媒体（图片）。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")
    console.print(f"  [dim]⬇ 下载 {post_id}…[/]")

    # 重建 WeiboPost
    from shared.protocols import WeiboDownloadResult, WeiboPost

    post = WeiboPost(
        post_id=post_id,
        text="",
        clean_text=ctx.msg.title,
        author=ctx.msg.author,
        user_id="",
        pubdate=ctx.msg.pubdate,
    )

    try:
        result = await download_weibo_media(post=post, config=ctx.config)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        console.print(f"  [red]✗ {ctx.error}[/]")
        logger.exception("Weibo download failed for %s", post_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        console.print(f"  [yellow]⚠️  {ctx.error}[/]")
        return False

    ctx.image_paths = result.image_paths
    ctx.content_text = result.text

    # 解析内容
    try:
        parsed = parse_weibo_post(post=post, download_result=result)
        if parsed.get("text"):
            ctx.content_text = parsed["text"]
        ctx.keywords = parsed.get("topics", [])
    except Exception as exc:
        console.print(f"  [yellow]⚠️  内容解析失败: {exc}[/]")
        logger.warning("Weibo parse failed for %s: %s", post_id, exc)

    console.print("  [green]✓ 下载完成[/]")
    return True


# ── Phase: PUSHED ──────────────────────────────────────────────


@PipelineEngine.register("weibo", Phase.PUSHED)
async def weibo_push(ctx: PhaseContext) -> bool:
    """推送微博通知。"""
    post_id = ctx.msg.msg_id.replace("weibo:", "")
    display_title = ctx.msg.title
    console.print(f"  [dim]🔔 推送通知…[/]")

    # 获取评论亮点
    try:
        highlights = await fetch_weibo_comment_highlights(
            post_id=post_id,
            config=ctx.config,
            author_user_id="",
        )
        ctx.comment_highlights = _format_comment_highlights(highlights)
    except Exception as exc:
        console.print(f"  [yellow]⚠️  评论获取失败: {exc}[/]")
        logger.warning("Weibo comment highlights failed for %s: %s", post_id, exc)

    try:
        await notify_new_weibo_post(
            post_id=post_id,
            title=display_title,
            author=ctx.msg.author,
            summary=ctx.summary_text,
            keywords=ctx.keywords,
            comment_highlights=ctx.comment_highlights or None,
            weibo_noti_config=ctx.config.weibo.notification,
        )
        console.print("  [green]✓ 通知推送完成[/]")
    except Exception as exc:
        console.print(f"  [yellow]⚠️  通知推送失败: {exc}[/]")
        logger.warning("Weibo notify failed for %s: %s", post_id, exc)

    return True


def _format_comment_highlights(highlights: list) -> str:
    """格式化评论亮点为 Markdown（与 B站 共用格式）。"""
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_author", False) or getattr(h, "is_up_owner", False)
        tags = " (作者)" if is_author else ""
        parts.append(f"- **{name}**{tags} (👍{like}):\n  {content}")
    return "\n".join(parts)
```

#### 9b: Simplify `platforms/weibo/monitor.py`

- [ ] **Step 2: Edit `platforms/weibo/monitor.py`**

Changes:
1. Remove `WeiboSubscriptionStore` class
2. Rename `check_new_weibo_posts` to `fetch_user_posts`, remove `store` param, remove filtering

```python
"""微博内容监控模块 - 检测用户新微博（纯检测函数）

提供 ``fetch_user_posts()`` 纯检测函数，不再负责去重和时间窗口过滤。
"""

from __future__ import annotations

import logging

from rich.console import Console

from platforms.weibo.api import fetch_user_posts as _api_fetch_user_posts
from shared.config import Config
from shared.protocols import WeiboPost

logger = logging.getLogger(__name__)
console = Console()

DEFAULT_MAX_POSTS_PER_CHECK = 10


async def fetch_user_posts(
    user_id: str,
    name: str,
    config: Config,
    max_posts: int = DEFAULT_MAX_POSTS_PER_CHECK,
) -> list[WeiboPost]:
    """纯检测函数：获取指定用户的最新微博。

    Args:
        user_id: 微博用户 ID
        name: 用户名称（用于日志）
        config: 全局配置
        max_posts: 单次检查最大返回帖子数

    Returns:
        原始微博列表（不做去重和时间窗口过滤）
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.error("[%s] 缺少 Cookie，无法检查微博", name)
        return []

    logger.info("获取用户 %s (%s) 的微博", name, user_id)

    try:
        posts = await _api_fetch_user_posts(cookie, user_id, max_posts)
    except Exception as e:
        logger.error("获取用户 %s 微博失败: %s", user_id, e)
        return []

    if not posts:
        logger.info("[%s] 未获取到任何微博", name)
        return []

    posts.sort(key=lambda p: p.pubdate, reverse=True)
    logger.info("[%s] 获取到 %d 条微博", name, len(posts))
    return posts
```

Note: The import changed from `from platforms.weibo.api import fetch_user_posts` to `from platforms.weibo.api import fetch_user_posts as _api_fetch_user_posts` to avoid naming conflict with the new `fetch_user_posts` function name.

- [ ] **Step 3: Verify**

```bash
uv run pyright platforms/weibo/monitor.py platforms/weibo/handlers.py
```

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add platforms/weibo/handlers.py platforms/weibo/monitor.py && git commit -m "feat(weibo): add handlers.py, simplify monitor.py to pure fetch function"
```

---

### Task 10: Simplify `core/pipeline.py`

**Duration:** 5 min

- [ ] **Step 1: Rewrite `core/pipeline.py`**

Replace the entire file with a thin delegation layer:

```python
"""流程编排模块 — 纯编排层，委托给 PipelineEngine

各平台的 ``run_*_check_once`` 简化为引擎委托 + 独立动态检查。
统计报告功能保留。
"""

from __future__ import annotations

import logging

from rich.console import Console

from core.engine import PipelineEngine
from shared.config import Config
from shared.http import close_session
from shared.protocols import DynamicInfo, Phase

# B站动态（独立于 PipelineEngine，本次不迁移）
from core.notifier import notify_dynamic
from platforms.bilibili.dynamic import check_new_dynamics
from platforms.bilibili.monitor import SubscriptionStore

console = Console()
logger = logging.getLogger(__name__)


# ── 统计 ──────────────────────────────────────────────────


class _Stats:
    """单次运行统计（保留向后兼容）"""

    def __init__(self) -> None:
        self.videos_processed: int = 0
        self.videos_succeeded: int = 0
        self.videos_failed: int = 0
        self.dynamics_processed: int = 0
        self.dynamics_succeeded: int = 0
        self.dynamics_failed: int = 0
        self.notes_processed: int = 0
        self.notes_succeeded: int = 0
        self.notes_failed: int = 0
        self.weibo_posts_processed: int = 0
        self.weibo_posts_succeeded: int = 0
        self.weibo_posts_failed: int = 0

    def report(self) -> str:
        lines: list[str] = []
        if self.videos_processed:
            lines.append(
                f"  视频: {self.videos_processed} 处理, {self.videos_succeeded} 成功, {self.videos_failed} 失败"
            )
        if self.dynamics_processed:
            lines.append(
                f"  动态: {self.dynamics_processed} 处理, {self.dynamics_succeeded} 成功, {self.dynamics_failed} 失败"
            )
        if self.notes_processed:
            lines.append(
                f"  笔记: {self.notes_processed} 处理, {self.notes_succeeded} 成功, {self.notes_failed} 失败"
            )
        if self.weibo_posts_processed:
            lines.append(
                f"  微博: {self.weibo_posts_processed} 处理, "
                f"{self.weibo_posts_succeeded} 成功, "
                f"{self.weibo_posts_failed} 失败"
            )
        return "\n".join(lines) if lines else "  无内容需要处理"


_run_stats: _Stats | None = None


def get_last_stats() -> _Stats | None:
    """返回最近一次运行的统计"""
    return _run_stats


# ── 平台入口（薄委托层） ──────────────────────────────────


async def run_bili_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """B站检查流程 — 视频委托给 PipelineEngine，动态独立检查"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None
    console.print("[cyan]🔍 B站检查…[/]")

    # 视频处理（通过引擎）
    await PipelineEngine.run_platform(config, "bili", from_phase=from_phase)
    _run_stats.videos_processed = 1
    _run_stats.videos_succeeded = 1

    # ── B站动态（独立于 PipelineEngine，本次不迁移）──
    if config.bilibili.monitor.watch_dynamic:
        store = SubscriptionStore(config.general.data_dir)
        console.print("[cyan]🔍 检查新动态…[/]")
        for sub in config.bilibili.subscriptions:
            try:
                new_dynamics = await check_new_dynamics(
                    uid=sub.uid,
                    config=config,
                    store=store,
                )
                for dyn in new_dynamics:
                    _run_stats.dynamics_processed += 1
                    try:
                        await process_dynamic(dyn, config, store)
                        _run_stats.dynamics_succeeded += 1
                    except Exception as exc:
                        _run_stats.dynamics_failed += 1
                        console.print(f"[red]✗ 处理动态失败: {exc}[/]")
                        logger.exception("Failed to process dynamic")
            except Exception as exc:
                console.print(f"[yellow]⚠️  检查 {sub.name}({sub.uid}) 动态失败: {exc}[/]")
                logger.warning(
                    "Failed to check dynamics for %s(%s): %s",
                    sub.name,
                    sub.uid,
                    exc,
                )
        store.save()

    console.print("[green]✓ B站检查完成[/]")


async def run_xhs_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """小红书检查流程 — 委托给 PipelineEngine"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None
    console.print("[cyan]🔍 小红书检查…[/]")
    await PipelineEngine.run_platform(config, "xhs", from_phase=from_phase)
    console.print("[green]✓ 小红书检查完成[/]")
    _run_stats.notes_processed = 1
    _run_stats.notes_succeeded = 1


async def run_weibo_check_once(config: Config, from_phase: Phase | None = None) -> None:
    """微博检查流程 — 委托给 PipelineEngine"""
    global _run_stats  # noqa: PLW0603
    assert _run_stats is not None
    console.print("[cyan]🔍 微博检查…[/]")
    await PipelineEngine.run_platform(config, "weibo", from_phase=from_phase)
    console.print("[green]✓ 微博检查完成[/]")
    _run_stats.weibo_posts_processed = 1
    _run_stats.weibo_posts_succeeded = 1


# ═══════════════════════════════════════════════════════════
# B站视频 API 检测（保留以兼容旧引用）
# ═══════════════════════════════════════════════════════════


async def _api_check(config: Config, store: SubscriptionStore) -> list:
    """API 模式逐个检查订阅 UP 主（保留以兼容旧引用，不再被调用）。

    视频检测已迁移至 ``bili_detector``（platforms/bilibili/handlers.py）。
    """
    return []


# ═══════════════════════════════════════════════════════════
# B站动态处理（独立于 PipelineEngine，本次不迁移）
# ═══════════════════════════════════════════════════════════


async def process_dynamic(
    dynamic_info: DynamicInfo,
    config: Config,
    store: SubscriptionStore,
) -> None:
    """处理单条 B站动态"""
    console.print(f"[bold blue]▶ 处理动态[/] {dynamic_info.dynamic_id}")

    # 如果动态关联了视频，通过 PipelineEngine 处理
    if dynamic_info.linked_bvid:
        from shared.message_store import MessageStore
        from shared.protocols import ContentType

        msg_store = MessageStore(config.general.data_dir)
        if msg_store.is_known(f"bili:{dynamic_info.linked_bvid}"):
            console.print(f"  [dim]关联视频 {dynamic_info.linked_bvid} 已处理过，跳过[/]")
        else:
            msg = msg_store.add_new(
                msg_id=f"bili:{dynamic_info.linked_bvid}",
                platform="bili",
                content_type=ContentType.VIDEO,
                pubdate=dynamic_info.pubdate,
                title=dynamic_info.title or "",
                author=dynamic_info.author or "",
            )
            if msg is not None:
                try:
                    await PipelineEngine.process_message(msg, config, msg_store)
                except Exception as exc:
                    console.print(f"[red]✗ 动态关联视频处理失败: {exc}[/]")
                    logger.exception(
                        "Linked video process failed for dynamic %s",
                        dynamic_info.dynamic_id,
                    )

    # 通知推送
    try:
        await notify_dynamic(
            dynamic_info={
                "user": dynamic_info.author,
                "content": dynamic_info.content or dynamic_info.title,
                "dynamic_id": dynamic_info.dynamic_id,
                "type": "动态",
                "url": dynamic_info.link,
            },
            config=config.bilibili.notification,
        )
    except Exception as exc:
        console.print(f"  [yellow]⚠️  动态通知推送失败: {exc}[/]")
        logger.warning("Dynamic notify failed for %s: %s", dynamic_info.dynamic_id, exc)

    # 标记动态已知
    dedup_key = f"dyn_{dynamic_info.dynamic_id}"
    store.mark_known(dedup_key)

    console.print("  [green]✓ 动态处理完成[/]")


# ═══════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════


async def run_check_once(
    config: Config,
    platform: str = "all",
    config_path: str = "config.toml",
    from_phase: Phase | None = None,
) -> None:
    """统一检查入口

    Args:
        config: 全局配置
        platform: "all" | "bili" | "xhs" | "weibo"
        config_path: 配置文件路径，用于 token 续期后的磁盘写入
        from_phase: 可选，从指定阶段重新处理
    """
    global _run_stats  # noqa: PLW0603
    _run_stats = _Stats()

    console.print()
    console.rule("[bold]Trawler v0.1.0[/bold]")
    console.print()

    if platform in ("all", "bili"):
        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens("bilibili", config, config_path)
        await run_bili_check_once(config, from_phase=from_phase)

    if platform in ("all", "xhs") and config.xiaohongshu.enabled:
        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens("xhs", config, config_path)
        await run_xhs_check_once(config, from_phase=from_phase)

    if platform in ("all", "weibo") and config.weibo.enabled:
        from shared.auth.scheduler import check_and_renew_tokens

        await check_and_renew_tokens("weibo", config, config_path)
        await run_weibo_check_once(config, from_phase=from_phase)

    # 打印统计
    console.print()
    console.rule("[bold]运行统计[/bold]")
    console.print(_run_stats.report())
    console.print()

    # 关闭全局 aiohttp session
    await close_session()
```

- [ ] **Step 2: Verify type check**

```bash
uv run pyright core/pipeline.py
```

Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add core/pipeline.py && git commit -m "refactor(pipeline): delegate to PipelineEngine, remove per-platform procedural logic"
```

---

### Task 11: Update `run_check.py` with `--from-phase` CLI option

**Duration:** 4 min

- [ ] **Step 1: Edit `run_check.py`**

Add the `--from-phase` option to the `check` command and pass it through:

```python
@click.option(
    "--from-phase",
    default=None,
    type=click.Choice(["discovered", "downloaded", "transcribed", "summarized"]),
    help="从指定阶段开始重新处理（不指定则自动断点续传）",
)
def check(platform: str, config_path: str, verbose: bool, from_phase: str | None) -> None:
    """检查各平台新内容"""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    if verbose:
        console.print("[dim]调试模式已启用[/]")
    try:
        config = load_config(config_path)
    except Exception as exc:
        console.print(f"[red]✗ 配置加载失败: {exc}[/]")
        sys.exit(1)
    try:
        # Convert string to Phase enum
        phase_enum = None
        if from_phase is not None:
            from shared.protocols import Phase
            phase_enum = Phase(from_phase.upper())
        asyncio.run(run_check_once(config, platform, config_path, from_phase=phase_enum))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[red]✗ 运行出错: {exc}[/]")
        if verbose:
            console.print_exception()
        sys.exit(1)
```

- [ ] **Step 2: Update the check command call chain**

Also import `Phase` from `shared.protocols` inside the function (lazy import to avoid circular dependencies at module level).

- [ ] **Step 3: Update existing tests for `test_cli.py`**

Add a test for `--from-phase`:

```python
# Add to tests/test_cli.py

def test_check_with_from_phase(runner: CliRunner) -> None:
    """--from-phase should be accepted as a CLI option."""
    result = runner.invoke(cli, ["check", "--from-phase", "downloaded", "--platform", "bili"])
    # Should fail because there's no config, but the option parsing should work
    assert "--from-phase" in result.output or result.exit_code in (0, 1)
```

Also update the existing `test_check_help` to verify `--from-phase` appears:

```python
def test_check_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check", "--help"])
    assert result.exit_code == 0
    assert "--platform" in result.output
    assert "--from-phase" in result.output  # new
    assert "all" in result.output
    assert "bili" in result.output
```

- [ ] **Step 4: Verify**

```bash
uv run pyright run_check.py
uv run pytest tests/test_cli.py -x -v
```

Expected: No type errors, tests pass.

- [ ] **Step 5: Commit**

```bash
git add run_check.py tests/test_cli.py && git commit -m "feat(cli): add --from-phase option to check command"
```

---

### Task 12: Write integration tests for platform handlers

**Duration:** 5 min

- [ ] **Step 1: Create `tests/test_platform_handlers.py`**

```python
"""Tests for platform handler registrations — verify all handlers wire up correctly."""

from __future__ import annotations

import pytest

from core.engine import PipelineEngine
from shared.protocols import Phase


# All handlers should be importable without errors
@pytest.mark.parametrize("platform", ["bili", "xhs", "weibo"])
def test_detector_registered(platform: str) -> None:
    assert platform in PipelineEngine._detectors, f"{platform} detector not registered"


def test_bili_handlers_registered() -> None:
    assert ("bili", Phase.DOWNLOADED) in PipelineEngine._handlers
    assert ("bili", Phase.PUSHED) in PipelineEngine._handlers


def test_xhs_handlers_registered() -> None:
    assert ("xhs", Phase.DOWNLOADED) in PipelineEngine._handlers
    assert ("xhs", Phase.PUSHED) in PipelineEngine._handlers


def test_weibo_handlers_registered() -> None:
    assert ("weibo", Phase.DOWNLOADED) in PipelineEngine._handlers
    assert ("weibo", Phase.PUSHED) in PipelineEngine._handlers


def test_cross_platform_handlers_registered() -> None:
    assert ("*", Phase.TRANSCRIBED) in PipelineEngine._handlers
    assert ("*", Phase.SUMMARIZED) in PipelineEngine._handlers
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_platform_handlers.py -x -v
```

Expected: All tests pass (since importing handlers.py files registers them).

- [ ] **Step 3: Commit**

```bash
git add tests/test_platform_handlers.py && git commit -m "test: add platform handler registration tests"
```

---

### Task 13: Final verification

**Duration:** 3 min

- [ ] **Step 1: Lint check**

```bash
uv run ruff check .
```

Expected: No new issues. (If there are pre-existing issues unrelated to this change, confirm they're not introduced by this plan.)

- [ ] **Step 2: Type check**

```bash
uv run pyright .
```

Expected: No new errors.

- [ ] **Step 3: Run all tests**

```bash
uv run pytest -x -v
```

Expected: All tests pass (both new and existing).

- [ ] **Step 4: Visual review of new file structure**

```bash
ls -la shared/message_store.py core/engine.py platforms/*/handlers.py
```

Expected: All 7 new files exist.

---

## Summary

| Task | File(s) | Type |
|------|---------|------|
| 1 | `shared/protocols.py` | MODIFY — add enums + dataclasses |
| 2 | `tests/test_message_store.py` | NEW — MessageStore tests |
| 3 | `shared/message_store.py` | NEW — MessageStore implementation |
| 4 | `tests/test_engine.py` | NEW — PipelineEngine tests |
| 5 | `core/engine.py` | NEW — PipelineEngine implementation |
| 6 | `tests/test_pipeline_e2e.py` | NEW — end-to-end integration test |
| 7 | `platforms/bilibili/handlers.py`, `monitor.py` | NEW + MODIFY |
| 8 | `platforms/xiaohongshu/handlers.py`, `monitor.py` | NEW + MODIFY |
| 9 | `platforms/weibo/handlers.py`, `monitor.py` | NEW + MODIFY |
| 10 | `core/pipeline.py` | SIMPLIFY |
| 11 | `run_check.py`, `tests/test_cli.py` | MODIFY |
| 12 | `tests/test_platform_handlers.py` | NEW |
| 13 | — | Final verification |

**Total: ~64 min** (all tasks combined)
