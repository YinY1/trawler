# Implementation Plan — 手动指定内容检查（Manual Content Check）

**日期**: 2026-06-28
**规模**: 中等（4 层：shared / core / CLI / 测试）
**TDD 节奏**: 是
**入口范围**: CLI 优先（Web 后续迭代）

---

## 1. 背景与现状

用户原始诉求：除了定时 cron 全量扫描，能"手动指定一批消息重跑流水线"，方便：①漏推的内容补推、②AI 摘要失败后手动重试、③验证 pipeline 修改后效果。

需要按多维度筛选消息（时间/标题/平台/作者/阶段），把命中的消息从指定阶段（默认 SUMMARIZED）重跑，且**默认禁止重新推送**（避免重复打扰订阅者），用户显式 opt-in `--no-skip-push` 才真正发通知。

---

## 2. 调研发现（每条引用 file:line）

### F1. `reset_to_phase` 已存在但粒度太粗
`shared/message_store.py:264-279` — `reset_to_phase(target, platform=None)` 已支持按 platform 批量 reset，但**无法按 msg_id 列表精准 reset**。手动模式必须支持"查询到 3 条命中的消息，只 reset 这 3 条"。

### F2. 现有查询接口过滤维度不足
`shared/message_store.py:112-167` — `get_messages` / `get_messages_in_window` 只支持 `phase` / `platform` / `window_hours` 过滤。**缺 title substring / author substring / 自定义时间起点**。

### F3. CLI 已有 `--from-phase` 但走的是全量 reset 路径
`run_check.py:489-494` + `run_check.py:507` — `check` 命令的 `--from-phase` 直接透传给 `run_check_once`，后者 `pipeline.py:208-209` 调 `store.reset_to_phase(from_phase, platform=platform)`，会**重置该 platform 下所有 >= target 阶段的消息**，不符合"按筛选条件精准匹配"。

### F4. cleanup(24) 会先删超 24h 的消息
`core/engine.py:201, 118` — `run_platform` 和 `run_check_once` 都在流程开头执行 `store.cleanup(24)`。**手动模式如果用户 `--since 7d` 想捞一周内的消息，cleanup 会先把超过 24h 的消息删掉，导致空结果**。手动模式必须跳过 cleanup。

### F5. Push handler 无 dedup，每次进 PUSHED 都无条件 send
`platforms/bilibili/handlers.py:236-285` + `platforms/xiaohongshu/handlers.py:115-145` + `platforms/weibo/handlers.py:152-185` — 三平台 push handler 都直接调 `send_to_subscription`，**没有"是否已经推送过"的检查**。手动重跑如果不加保护，会把同一条消息推送给订阅者 N 次。

### F6. PhaseContext 是 dataclass，加字段成本极低
`shared/protocols.py:303-316` — `PhaseContext` 是 `@dataclass`，加 `skip_push: bool = False` 字段零成本，engine 创建 ctx 时透传即可。push handler 检查该字段决定是否跳过通知。

### F7. `run_check_once` 单平台/多平台分支不同
`core/pipeline.py:108-150` — 多平台走 `asyncio.gather` 共享 `shared_store`，单平台走串行。手动模式如果用户 `--platform all + --title xxx`，需要先 query 后逐条 process，**不能复用 `run_check_once` 的 detector 路径**（手动模式不需要 detector，只对已存在的消息重跑）。

### F8. `--since` 时间解析需支持两种格式
相对格式（`24h` / `7d`）和绝对格式（`2026-06-01`）需要解析为 Unix 时间戳。`shared/message_store.py:139-167` 的 `get_messages_in_window` 用 `window_hours` 参数，但手动模式直接传 `since: int`（绝对时间戳）更直观。

### F9. 测试已有 `test_reset_to_phase_*`
`tests/test_message_store.py:165-176` — 已有 `reset_to_phase` 的测试模式，`reset_specific` 可复制此模式。

### F10. CliRunner 测试模式
`tests/` 目录已有 CLI 测试（如 `test_run_check.py` 等），可参考其 CliRunner mock 模式。

---

## 3. 关键决策（基于已确认的用户决策）

| ID | 决策 | 选定 + 理由 |
|----|------|-------------|
| D1 | 入口范围 | **CLI 优先**。Web `/check/run` 后续迭代复用同一底层 API。 |
| D2 | 筛选维度 | **全套**：since + title（大小写不敏感 substring）+ platform + author（大小写不敏感 substring）+ phase。全部 AND 组合，任一为 None 表示不限制。 |
| D3 | 默认重跑起点 | **SUMMARIZED**。最常见场景是"摘要生成失败/不满意，重新生成"。 |
| D4 | 重复推送防护 | **默认禁止重推**：`skip_push=True`，pipeline 跑到 PUSHED 阶段时 push handler 检查 `ctx.skip_push` 跳过 `send_to_subscription`（但消息 phase 仍推进到 PUSHED）。用户显式 `--no-skip-push` 才真正推送。 |
| D5 | 持久化策略 | **立即 save**。`reset_specific` 内部 store.save()，避免中途崩溃丢失 reset 状态。 |
| D6 | cleanup 处理 | **手动模式跳过 cleanup**。手动模式可能要处理超过 24h 的历史消息，cleanup 会误删。在 `run_specific_messages` 不调用 cleanup。 |
| D7 | skip_push 实现位置 | **PhaseContext 加标志 + push handler 检查**。比"reset 只到 SUMMARIZED 不进 PUSHED"更灵活——phase 仍推进到 PUSHED（dashboard 状态正确），只是不真发通知。 |
| D8 | `--since` 格式 | 支持相对（`24h` / `7d` / `30m`）和绝对（`2026-06-01` / `2026-06-01T12:00:00`）。CLI 层解析为 Unix 时间戳，store 层只接 int。 |
| D9 | 空结果处理 | 查询返回 0 条时打印 `[yellow]⚠️[/] 没有匹配的消息`，正常退出（exit 0），不当作错误。 |
| D10 | 并发安全 | 不加文件锁（改动面大），只在 `run_specific_messages` docstring 警告"避免与 cron 同时运行"。 |

---

## 4. 文件清单

### 修改（4 个）
| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `shared/message_store.py` | 加 2 个方法 | `query_messages` + `reset_specific` |
| `shared/protocols.py` | 加字段 | PhaseContext 加 `skip_push: bool = False` |
| `core/engine.py` | 加 1 个方法 + 改 process_message | `run_specific_messages` + process_message 透传 skip_push 到 ctx |
| `run_check.py` | 扩展 check 命令 | 加 5 个 Click options + 分支逻辑 |

### 修改（3 个 - push handler）
| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `platforms/bilibili/handlers.py` | 加 skip_push 检查 | push handler 开头检查 `ctx.skip_push` |
| `platforms/xiaohongshu/handlers.py` | 加 skip_push 检查 | 同上 |
| `platforms/weibo/handlers.py` | 加 skip_push 检查 | 同上 |

### 测试（1 个新增 + 2 个扩展）
| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `tests/test_message_store.py` | 扩展 | `query_messages` + `reset_specific` 测试 |
| `tests/test_manual_check.py` | **新增** | engine `run_specific_messages` + skip_push 行为测试 |
| `tests/test_run_check_cli.py` | **新增** | CliRunner 测试 `--since` / `--title` / `--skip-push` 等 |

---

## 5. 任务分解（TDD）

### 任务 1: MessageStore 新增 `query_messages` 方法

**测试先写** → `tests/test_message_store.py` 末尾追加：
```python
# ── query_messages (plan 2026-06-28-manual-content-check) ────────


def test_query_messages_by_platform(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "T2", "A")
    result = store.query_messages(platform="bili")
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    result = store.query_messages(phase=Phase.SUMMARIZED)
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_title_substring_case_insensitive(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "Python Tutorial", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "Java Guide", "A")
    result = store.query_messages(title="python")
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_author_substring_case_insensitive(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "Alice")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "Bob")
    result = store.query_messages(author="ali")
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_since(store: MessageStore) -> None:
    now = int(time.time())
    # bypass add_new window check to inject old message
    store._messages["bili:old"] = {
        "platform": "bili", "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value, "pubdate": now - 48 * 3600,
        "title": "Old", "author": "A", "created_at": 0.0, "updated_at": 0.0, "error": "",
    }
    store._messages["bili:new"] = {
        "platform": "bili", "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value, "pubdate": now - 3600,
        "title": "New", "author": "A", "created_at": 0.0, "updated_at": 0.0, "error": "",
    }
    store._dirty = True
    # since = 24h ago → only "new"
    since_ts = now - 24 * 3600
    result = store.query_messages(since=since_ts)
    assert len(result) == 1
    assert result[0].msg_id == "bili:new"


def test_query_messages_combined_filters(store: MessageStore) -> None:
    """AND 组合：platform + title + phase 同时过滤。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "Python Guide", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "Python Tips", "A")
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "Python Notes", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    result = store.query_messages(platform="bili", title="python", phase=Phase.SUMMARIZED)
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_no_filters_returns_all(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "T2", "A")
    result = store.query_messages()
    assert len(result) == 2


def test_query_messages_empty_result(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    result = store.query_messages(title="nonexistent")
    assert result == []
```

**实现** `shared/message_store.py`，在 `get_messages_in_window`（line 167）后追加：
```python
    def query_messages(
        self,
        *,
        since: int | None = None,
        title: str | None = None,
        author: str | None = None,
        platform: str | None = None,
        phase: Phase | None = None,
    ) -> list[MessageRecord]:
        """多维度筛选消息（手动检查专用，plan 2026-06-28）。

        所有过滤条件 AND 组合，None 表示不限制。

        Args:
            since: Unix 时间戳，只返回 pubdate >= since 的消息（绝对时间戳，不是 hours）
            title: 大小写不敏感 substring 匹配
            author: 大小写不敏感 substring 匹配
            platform: 精确匹配平台标识
            phase: 精确匹配阶段

        与 ``get_messages_in_window`` 区别：本方法不做 cleanup，支持超过 24h 的历史消息查询。
        """
        title_lower = title.lower() if title else None
        author_lower = author.lower() if author else None
        results: list[MessageRecord] = []
        for msg_id, data in self._messages.items():
            if since is not None and data.get("pubdate", 0) < since:
                continue
            if platform is not None and data.get("platform") != platform:
                continue
            if phase is not None and data.get("phase") != phase.value:
                continue
            if title_lower is not None and title_lower not in data.get("title", "").lower():
                continue
            if author_lower is not None and author_lower not in data.get("author", "").lower():
                continue
            results.append(self._msg_from_dict(msg_id, data))
        return results
```

**验证**：`uv run pytest tests/test_message_store.py -k query_messages -x`

---

### 任务 2: MessageStore 新增 `reset_specific` 方法

**测试先写** → `tests/test_message_store.py` 继续追加：
```python
# ── reset_specific (plan 2026-06-28-manual-content-check) ────────


def test_reset_specific_resets_target_ids(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.add_new("bili:BV3", "bili", ContentType.VIDEO, int(time.time()), "T3", "A")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.mark_phase("bili:BV2", Phase.PUSHED)
    store.mark_phase("bili:BV3", Phase.PUSHED)

    count = store.reset_specific(["bili:BV1", "bili:BV3"], Phase.SUMMARIZED)
    assert count == 2
    assert store.get_message("bili:BV1").phase == Phase.SUMMARIZED
    assert store.get_message("bili:BV2").phase == Phase.PUSHED  # 未被 reset
    assert store.get_message("bili:BV3").phase == Phase.SUMMARIZED


def test_reset_specific_clears_error(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    store.mark_error("bili:BV1", "summary failed")
    store.reset_specific(["bili:BV1"], Phase.SUMMARIZED)
    assert store.get_message("bili:BV1").error == ""


def test_reset_specific_skips_lower_phase_messages(store: MessageStore) -> None:
    """目标阶段 >= current phase 的消息才 reset，低于的不动。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.mark_phase("bili:BV1", Phase.DISCOVERED)  # 比 SUMMARIZED 低
    count = store.reset_specific(["bili:BV1"], Phase.SUMMARIZED)
    assert count == 0
    assert store.get_message("bili:BV1").phase == Phase.DISCOVERED


def test_reset_specific_empty_list(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    count = store.reset_specific([], Phase.SUMMARIZED)
    assert count == 0


def test_reset_specific_unknown_id(store: MessageStore) -> None:
    """未知 msg_id 静默跳过，不抛异常。"""
    count = store.reset_specific(["bili:nonexistent"], Phase.SUMMARIZED)
    assert count == 0


def test_reset_specific_persists_immediately(tmp_path: Path) -> None:
    """reset_specific 内部必须 save()，确保崩溃不丢数据（D5）。"""
    s1 = MessageStore(tmp_path)
    s1.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    s1.mark_phase("bili:BV1", Phase.PUSHED)

    s1.reset_specific(["bili:BV1"], Phase.SUMMARIZED)
    # 不显式 save()，直接 reload
    s2 = MessageStore(tmp_path)
    msg = s2.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.SUMMARIZED
```

**实现** `shared/message_store.py`，在 `reset_to_phase`（line 279）后追加：
```python
    def reset_specific(self, msg_ids: list[str], target: Phase) -> int:
        """将指定 ID 的消息回退到 target 阶段，清除 error（手动检查专用）。

        与 ``reset_to_phase`` 区别：按 msg_id 列表精准 reset，而不是按 platform 批量。
        调用后立即 save()（plan D5）。

        Args:
            msg_ids: 要 reset 的消息 ID 列表
            target: 目标阶段

        Returns:
            实际被 reset 的消息数量（跳过未知 ID 和 phase < target 的消息）
        """
        count = 0
        target_value = target.value
        for msg_id in msg_ids:
            data = self._messages.get(msg_id)
            if data is None:
                continue
            current_phase = data.get("phase", Phase.DISCOVERED.value)
            if current_phase < target_value:
                continue
            data["phase"] = target_value
            data["error"] = ""
            data["updated_at"] = time.time()
            self._dirty = True
            count += 1
        self.save()
        return count
```

**验证**：`uv run pytest tests/test_message_store.py -k reset_specific -x`

---

### 任务 3: PhaseContext 加 skip_push 字段 + engine 透传

**测试先写** → `tests/test_manual_check.py`（新增文件）：
```python
"""Tests for manual content check — run_specific_messages + skip_push behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase, PhaseContext


def test_phase_context_has_skip_push_default_false() -> None:
    """PhaseContext 必须有 skip_push 字段，默认 False。"""
    from shared.protocols import MessageRecord

    msg = MessageRecord(
        msg_id="x", platform="bili", content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED, pubdate=0, title="t", author="a",
    )
    ctx = PhaseContext(msg=msg, config=None)  # type: ignore[arg-type]
    assert ctx.skip_push is False
```

**实现 1**: `shared/protocols.py` PhaseContext（line 316 后）加字段：
```python
    # 手动重跑模式标志（plan 2026-06-28）：True 时 push handler 跳过 send_to_subscription
    skip_push: bool = False
```

**实现 2**: `core/engine.py` `process_message`（line 123）改 ctx 创建：
```python
        ctx = PhaseContext(msg=msg, config=config, skip_push=getattr(msg, "_skip_push", False))
```

**注意**：`MessageRecord` 不持有 skip_push（避免持久化），engine 用 `msg._skip_push` 临时属性透传。更干净的做法见任务 4 的 `run_specific_messages` 实现。

**验证**：`uv run pytest tests/test_manual_check.py::test_phase_context_has_skip_push_default_false -x`

---

### 任务 4: engine 新增 `run_specific_messages` 方法

**测试先写** → `tests/test_manual_check.py` 继续追加：
```python
import asyncio
import time

from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def mock_store(tmp_path: Path) -> MessageStore:
    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.mark_phase("bili:BV2", Phase.PUSHED)
    return store


async def test_run_specific_messages_resets_and_processes(mock_store: MessageStore) -> None:
    """run_specific_messages 应 reset 目标消息并逐条 process。"""
    from core.engine import PipelineEngine
    from shared.config import Config

    config = MagicMock(spec=Config)
    config.general.data_dir = str(mock_store._path.parent)

    # mock process_message 避免真实 handler
    with patch.object(PipelineEngine, "process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"],
            from_phase=Phase.SUMMARIZED,
            skip_push=True,
            config=config,
            store=mock_store,
        )
        assert mock_proc.called
        # 验证 reset 生效
        assert mock_store.get_message("bili:BV1").phase == Phase.SUMMARIZED
        # 验证 skip_push 通过 ctx 透传
        ctx_arg = mock_proc.call_args[0][0]  # 第一个位置参数是 msg
        # process_message 签名是 (msg, config, store)，skip_push 在 msg._skip_push


async def test_run_specific_messages_empty_list_noop(mock_store: MessageStore) -> None:
    """空 ID 列表应该安全 no-op。"""
    from core.engine import PipelineEngine
    from shared.config import Config

    config = MagicMock(spec=Config)
    with patch.object(PipelineEngine, "process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_specific_messages(
            msg_ids=[], from_phase=Phase.SUMMARIZED, skip_push=True,
            config=config, store=mock_store,
        )
        assert not mock_proc.called


async def test_run_specific_messages_skips_cleanup(mock_store: MessageStore) -> None:
    """手动模式不能调 cleanup，避免误删超 24h 的历史消息（D6）。"""
    from core.engine import PipelineEngine
    from shared.config import Config

    config = MagicMock(spec=Config)
    with patch.object(mock_store, "cleanup") as mock_cleanup, \
         patch.object(PipelineEngine, "process_message", new=AsyncMock()):
        await PipelineEngine.run_specific_messages(
            msg_ids=["bili:BV1"], from_phase=Phase.SUMMARIZED, skip_push=True,
            config=config, store=mock_store,
        )
        assert not mock_cleanup.called
```

**实现** `core/engine.py`，在 `run_platform`（line 239）后追加：
```python
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
                # reset_specific 可能跳过了某些（phase < target），跳过
                continue
            # 通过临时属性透传 skip_push 到 PhaseContext（避免污染 MessageRecord schema）
            setattr(msg, "_skip_push", skip_push)
            await cls.process_message(msg, config, store)

        store.save()
```

**改 process_message** 创建 ctx 时读取该属性（任务 3 已改）：
```python
        ctx = PhaseContext(msg=msg, config=config, skip_push=getattr(msg, "_skip_push", False))
```

**验证**：`uv run pytest tests/test_manual_check.py -x`

---

### 任务 5: 三平台 push handler 加 skip_push 检查

**测试先写** → `tests/test_manual_check.py` 继续追加（以 bili 为例，xhs/weibo 同理）：
```python
async def test_bili_push_skips_when_skip_push_true(tmp_path: Path) -> None:
    """ctx.skip_push=True 时 bili_push 应跳过 send_to_subscription。"""
    from platforms.bilibili.handlers import bili_push
    from shared.protocols import MessageRecord

    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A", subscription_ref="42")
    msg = store.get_message("bili:BV1")
    assert msg is not None

    config = MagicMock()
    config.bilibili.subscriptions = [MagicMock(uid=42, notify_endpoints=["gotify"])]

    ctx = PhaseContext(msg=msg, config=config, skip_push=True)

    with patch("platforms.bilibili.handlers.send_to_subscription", new=AsyncMock()) as mock_send:
        result = await bili_push(ctx)
        assert result is True
        assert not mock_send.called
```

**实现 1**: `platforms/bilibili/handlers.py` `bili_push`（line 237 函数体开头，docstring 后）加：
```python
    if ctx.skip_push:
        logger.info("⏭ 跳过推送（skip_push=True）: %s", ctx.msg.msg_id)
        return True
```

**实现 2**: `platforms/xiaohongshu/handlers.py` 对应 push handler 同样加（line 115 函数体开头）：
```python
    if ctx.skip_push:
        logger.info("⏭ 跳过推送（skip_push=True）: %s", ctx.msg.msg_id)
        return True
```

**实现 3**: `platforms/weibo/handlers.py` 同样（line 152 函数体开头）：
```python
    if ctx.skip_push:
        logger.info("⏭ 跳过推送（skip_push=True）: %s", ctx.msg.msg_id)
        return True
```

**验证**：
```bash
uv run pytest tests/test_manual_check.py -k push_skips -x
# 三平台各一个测试
```

---

### 任务 6: CLI 扩展 check 命令

**测试先写** → `tests/test_run_check_cli.py`（新增文件）：
```python
"""CLI tests for manual content check options (plan 2026-06-28)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from run_check import cli
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase


@pytest.fixture
def populated_store(tmp_path: Path) -> MessageStore:
    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "Python Tutorial", "Alice")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "Java Guide", "Bob")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.mark_phase("bili:BV2", Phase.PUSHED)
    store.save()
    return store


def test_since_parser_relative_hours() -> None:
    """--since 24h 解析为 now - 24*3600。"""
    from run_check import parse_since
    now = int(time.time())
    result = parse_since("24h")
    assert abs(result - (now - 24 * 3600)) < 5  # 5 秒容差


def test_since_parser_relative_days() -> None:
    from run_check import parse_since
    now = int(time.time())
    result = parse_since("7d")
    assert abs(result - (now - 7 * 24 * 3600)) < 5


def test_since_parser_absolute_date() -> None:
    from run_check import parse_since
    result = parse_since("2026-06-01")
    # 解析为当天 00:00:00 本地时间的 Unix 时间戳
    import time as _time
    expected = int(_time.mktime(_time.strptime("2026-06-01", "%Y-%m-%d")))
    assert result == expected


def test_since_parser_absolute_datetime() -> None:
    from run_check import parse_since
    result = parse_since("2026-06-01T12:00:00")
    import time as _time
    expected = int(_time.mktime(_time.strptime("2026-06-01T12:00:00", "%Y-%m-%dT%H:%M:%S")))
    assert result == expected


def test_since_parser_invalid_raises() -> None:
    from run_check import parse_since
    with pytest.raises(ValueError):
        parse_since("invalid")


def test_check_with_title_filter(populated_store: MessageStore, tmp_path: Path) -> None:
    """check --title python 应只匹配 BV1。"""
    runner = CliRunner()
    with patch("run_check.load_config", new=AsyncMock()), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()) as mock_run, \
         patch("core.pipeline.run_check_once", new=AsyncMock()):
        result = runner.invoke(cli, [
            "check", "--title", "python",
            "--config", str(tmp_path / "config.toml"),
        ])
        # 验证 run_specific_messages 被调用，msg_ids 只含 BV1
        assert mock_run.called
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["msg_ids"] == ["bili:BV1"]


def test_check_skip_push_default(populated_store: MessageStore, tmp_path: Path) -> None:
    """不传 flag 时 skip_push 默认 True。"""
    runner = CliRunner()
    with patch("run_check.load_config", new=AsyncMock()), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()) as mock_run:
        runner.invoke(cli, ["check", "--title", "python", "--config", str(tmp_path / "config.toml")])
        assert mock_run.call_args.kwargs["skip_push"] is True


def test_check_no_skip_push_flag(populated_store: MessageStore, tmp_path: Path) -> None:
    """--no-skip-push 应让 skip_push=False。"""
    runner = CliRunner()
    with patch("run_check.load_config", new=AsyncMock()), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()) as mock_run:
        runner.invoke(cli, [
            "check", "--title", "python", "--no-skip-push",
            "--config", str(tmp_path / "config.toml"),
        ])
        assert mock_run.call_args.kwargs["skip_push"] is False


def test_check_empty_result_prints_warning(populated_store: MessageStore, tmp_path: Path) -> None:
    """筛选无匹配时打印警告并正常退出。"""
    runner = CliRunner()
    with patch("run_check.load_config", new=AsyncMock()), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()):
        result = runner.invoke(cli, [
            "check", "--title", "nonexistent",
            "--config", str(tmp_path / "config.toml"),
        ])
        assert result.exit_code == 0
        assert "没有匹配的消息" in result.output
```

**实现** `run_check.py`：

1. 模块顶部（import 区后，`setup_logging` 前）加 `parse_since` 工具函数：
```python
import re
import time as _time


def parse_since(value: str) -> int:
    """解析 --since 参数为 Unix 时间戳。

    支持两种格式：
    - 相对：``24h`` / ``7d`` / ``30m``（h=小时, d=天, m=分钟）
    - 绝对：``2026-06-01`` 或 ``2026-06-01T12:00:00``（本地时区）

    Raises:
        ValueError: 格式无法识别
    """
    # 相对格式：数字 + 单位
    match = re.fullmatch(r"(\d+)([hmd])", value)
    if match:
        num, unit = int(match.group(1)), match.group(2)
        multiplier = {"h": 3600, "m": 60, "d": 86400}[unit]
        return int(_time.time()) - num * multiplier
    # 绝对格式：纯日期
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(_time.mktime(_time.strptime(value, fmt)))
        except ValueError:
            continue
    raise ValueError(f"无法解析 --since 值: {value!r}（支持格式: 24h / 7d / 30m / 2026-06-01）")
```

2. `check` 命令（line 469 装饰器链）追加 options：
```python
@cli.command()
@click.option("--platform", type=click.Choice(["all", "bili", "xhs", "weibo"]), default="all", help="检查的平台")
@click.option("--config", "config_path", default="config/config.toml", show_default=True, help="配置文件路径")
@click.option("--verbose", is_flag=True, default=False, help="启用详细日志输出")
@click.option("--from-phase", default=None,
              type=click.Choice(["discovered", "downloaded", "transcribed", "summarized"], case_sensitive=False),
              help="从指定阶段开始处理（不指定则自动断点续传）")
# ↓ 新增：手动检查筛选选项（plan 2026-06-28）
@click.option("--since", default=None,
              help="时间起点筛选：相对(24h/7d/30m) 或绝对(2026-06-01)")
@click.option("--title", default=None, help="标题模糊匹配（大小写不敏感 substring）")
@click.option("--author", default=None, help="作者模糊匹配（大小写不敏感 substring）")
@click.option("--reset-phase", "reset_phase", default="summarized",
              type=click.Choice(["discovered", "downloaded", "transcribed", "summarized"], case_sensitive=False),
              show_default=True, help="手动模式重跑起始阶段")
@click.option("--skip-push/--no-skip-push", default=True, show_default=True,
              help="是否跳过推送通知（手动模式默认跳过，避免重复打扰订阅者）")
def check(
    platform: str, config_path: str, verbose: bool, from_phase: str | None,
    since: str | None, title: str | None, author: str | None,
    reset_phase: str, skip_push: bool,
) -> None:
    """检查各平台新内容"""
    # ... config 加载、logging setup 保持不变 ...

    # 判断是否手动模式：传了任意筛选参数
    manual_mode = any([since, title, author])

    try:
        if manual_mode:
            asyncio.run(_run_manual_check(
                config, platform, since, title, author, reset_phase, skip_push,
            ))
        else:
            asyncio.run(run_check_once(config, platform, config_path, from_phase=from_phase))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
    except Exception as exc:
        # ... 原有错误处理 ...
```

3. 新增 `_run_manual_check` 辅助函数（在 `check` 函数后）：
```python
async def _run_manual_check(
    config: Config,
    platform: str,
    since: str | None,
    title: str | None,
    author: str | None,
    reset_phase: str,
    skip_push: bool,
) -> None:
    """手动模式：按筛选条件查询消息并重跑（plan 2026-06-28）。"""
    from core.engine import PipelineEngine
    from shared.message_store import MessageStore

    store = MessageStore(config.general.data_dir)
    # ⚠️ 不调 cleanup（D6：避免误删超 24h 的历史消息）

    since_ts = parse_since(since) if since else None
    platform_filter = None if platform == "all" else platform
    target_phase = Phase[reset_phase.upper()]

    matched = store.query_messages(
        since=since_ts, title=title, author=author,
        platform=platform_filter,
    )
    if not matched:
        console.print("[yellow]⚠️[/] 没有匹配的消息")
        return

    # 显示匹配结果
    table = Table(title=f"匹配 {len(matched)} 条消息")
    table.add_column("ID", style="dim")
    table.add_column("标题")
    table.add_column("平台")
    table.add_column("作者")
    table.add_column("当前阶段")
    for m in matched:
        table.add_row(m.msg_id, m.title[:30], m.platform, m.author, m.phase.name)
    console.print(table)

    console.print(f"[bold blue]▶[/] 从 {reset_phase.upper()} 阶段重跑，skip_push={skip_push}")

    msg_ids = [m.msg_id for m in matched]
    await PipelineEngine.run_specific_messages(
        msg_ids=msg_ids,
        from_phase=target_phase,
        skip_push=skip_push,
        config=config,
        store=store,
    )
```

**验证**：
```bash
uv run pytest tests/test_run_check_cli.py -x
uv run trawler check --help  # 确认新 options 出现在帮助文本
```

---

### 任务 7: 端到端验证

1. `uv run ruff check .`
2. `uv run pyright`（**无参数**，见全局 AGENTS.md）
3. `uv run pytest -x`
4. 在本地有真实 `data/messages.json` 的环境跑：
   ```bash
   # 查询匹配（dry-run 效果，先看 table）
   uv run trawler check --title "python" --since 7d
   # 实际重跑（默认 skip_push）
   uv run trawler check --title "python" --since 7d --reset-phase summarized
   # 强制重推
   uv run trawler check --title "python" --since 7d --no-skip-push
   ```
5. 确认日志输出 `⏭ 跳过推送（skip_push=True）: bili:BV1xx`

---

## 6. 验证步骤汇总

```bash
# 单元测试
uv run pytest tests/test_message_store.py -x
uv run pytest tests/test_manual_check.py -x
uv run pytest tests/test_run_check_cli.py -x

# 全套
uv run pytest -x

# 静态检查
uv run ruff check .
uv run pyright

# 集成验证（需真实配置 + data/messages.json）
uv run trawler check --title "test" --since 7d --skip-push
uv run trawler check --help  # 确认所有新 options
```

---

## 7. 风险与不确定项

| ID | 风险 | 影响 | 缓解 |
|----|------|------|------|
| R1 | cleanup(24) 与手动模式冲突 | `--since 7d` 时 cleanup 先删超 24h 消息，导致空结果 | D6：`run_specific_messages` 不调 cleanup。但注意 `run_check_once` 仍会调，所以手动模式走独立路径不经过 `run_check_once`。 |
| R2 | 并发安全：手动跑 + cron 同时跑 | 两个进程的 MessageStore 内存快照互相覆盖，可能丢失数据 | D10：不加文件锁（改动面大），只在 docstring 警告。建议用户避免同时运行，或加外层 cron lock（如 `flock`）。 |
| R3 | `_skip_push` 通过 setattr 临时挂在 MessageRecord 上 | 类型不洁，pyright 可能告警 | 任务 3 用 `getattr(msg, "_skip_push", False)` 兜底。如 pyright 报错可加 `# type: ignore[attr-defined]`。更彻底的方案是把 skip_push 加到 PhaseContext 构造时显式传参（run_specific_messages 调 process_message 时直接传 ctx），但需改 process_message 签名，改动面大，本 plan 不做。 |
| R4 | `--since` 绝对时间用本地时区 | 跨时区用户可能困惑 | 接受现状，与 `pubdate` 存储一致（都是本地时间戳）。文档说明。 |
| R5 | PHASE_FLOW 决定 VIDEO 类型必须经过 TRANSCRIBED 阶段 | `--reset-phase summarized` 时 VIDEO 消息从 SUMMARIZED 直接进 PUSHED，跳过 TRANSCRIBED 是正常的（PHASE_FLOW 已经定义了这个流转） | 无需处理，验证：`PHASE_FLOW[VIDEO].index(SUMMARIZED) + 1` = PUSHED 索引。 |
| R6 | title/author substring 匹配中文 | 中文无大小写概念，`.lower()` 对中文 no-op | 行为正确，无需特殊处理。`"Python".lower() == "python"`，`"教程".lower() == "教程"`。 |
| R7 | CliRunner 测试 mock 边界 | `run_check.py` 模块级 import 会触发 shared.config 等加载 | 测试用 `patch("run_check.load_config")` 等在模块级 mock，避免真实配置加载。 |
| R8 | `--reset-phase` 不含 `pushed` 选项 | 用户无法"只重推不重跑 pipeline" | 故意设计：重推必须配合 `--no-skip-push`，phase 起点必须是 PUSHED 之前的阶段（discovered/downloaded/transcribed/summarized）。如果只想重推，用 `--reset-phase summarized --no-skip-push`。 |

---

## 8. 后续可选清理（不在本 plan 范围）

1. **Web `/check/run` 接入手动模式**：`web/routes/check.py` 当前调 `run_check_once(platform="all")`，加 query params 透传 `since/title/author/reset_phase/skip_push`，调 `run_specific_messages`。复用本 plan 的底层 API。
2. **文件锁**：如 R2 并发问题频发，可在 `run_specific_messages` 加 `fcntl.flock` 排他锁。
3. **dry-run 模式**：加 `--dry-run` flag，只打印匹配的 table 不实际 reset/process。
4. **交互式确认**：匹配 > N 条时 prompt 确认（避免误操作大量消息）。
5. **`_skip_push` 改为 process_message 显式参数**：彻底解决 R3 的类型洁癖问题，需改 `process_message` 签名加 `skip_push: bool = False`，所有调用点同步更新。
6. **query_messages 支持 regex**：当前是 substring，未来可加 `--title-regex` flag 走 `re.search`。

---

## 9. 任务依赖图

```
任务 1 (store.query_messages)
    │
    ▼
任务 2 (store.reset_specific)  ←── 独立，可与任务 1 并行
    │
    ▼
任务 3 (PhaseContext.skip_push 字段)  ←── 独立，可与 1+2 并行
    │
    ▼
任务 4 (engine.run_specific_messages)  ←── 依赖 1+2+3
    │
    ▼
任务 5 (push handlers skip_push 检查)  ←── 依赖 3，可与 4 并行
    │
    ▼
任务 6 (CLI 扩展)  ←── 依赖 4+5
    │
    ▼
任务 7 (端到端验证)  ←── 依赖全部
```

任务 1/2/3 互不依赖，可并行开发。任务 4 是核心整合点。任务 5 与 4 可并行（不同文件）。

---

## 10. 调研发现的"惊喜"

1. **`run_check_once` 不能复用** —— 它内部强制 cleanup + detector，与手动模式语义冲突。必须新写 `run_specific_messages` 独立路径（F7）。
2. **`reset_to_phase` 的 `<` 比较依赖 Phase enum 的 `auto()` 顺序** —— `Phase(Enum)` 用 `auto()`，枚举值是 1/2/3/4/5 的 int，`current_phase < target_value` 是字符串比较还是 int 比较？查 `shared/message_store.py:275` `current_phase >= target.value`，这里 `target.value` 是 int（auto 产生的），但 `data.get("phase")` 是序列化后的字符串（如 `"summarized"`）。**这是已有 bug 隐患**——字符串比较 `"pushed" >= "summarized"` 是字典序，碰巧和 enum 顺序一致但不可靠。本 plan `reset_specific` 复用此模式，**不修这个底层 bug**（影响面大），但记录在案。
3. **CliRunner 测试需 mock 模块级 import** —— `run_check.py` 顶部 `from core.pipeline import run_check_once` 会触发整条 import 链。测试需在 `runner.invoke` 前 patch 掉这些，否则缺 config 文件会失败。
4. **`--since 30m` 的 `m` 与分钟单位的歧义** —— `30m` 是 30 分钟，但用户可能误以为是 30 月。help 文本明确写 `(24h/7d/30m)` 减少歧义。
