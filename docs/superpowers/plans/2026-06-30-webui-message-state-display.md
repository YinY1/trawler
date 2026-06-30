# WebUI 消息状态显示补全 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补全 PR #50/#51/#52 数据模型改造后遗留的 4 个 WebUI 显示缺口 (G1/G2/G3/G4),让 dashboard 能正确呈现中文阶段名、区分永久/临时错误、展示重试计数、用 tag 指示 content_type。

**Architecture:** 自底向上三层数据流改造。底层 `MessageRecord` 新增 `permanent_error` 字段 → `MessageStore` 的 `mark_error`/`reset_*` 系列方法同步写入/清零 → `core/engine.py` 三处 `mark_error` 调用透传 `permanent=True` → 顶层 Jinja2 新增 `phase_label` filter + `content_type_tag` macro,改两处模板渲染点。

**Tech Stack:** Python 3.12 dataclass + enum,FastAPI + Jinja2Templates (filter 注册),htmx + Tailwind (前端零新增依赖)。

---

## File Structure

| 文件 | 责任 | 改动类型 |
|---|---|---|
| `shared/protocols.py` | `MessageRecord` 数据模型 | 加 1 个字段 |
| `shared/message_store.py` | JSON 持久化层 | 改 6 个方法 |
| `core/engine.py` | 流水线编排 | 改 3 处 mark_error 调用 |
| `web/app.py` | Jinja2 filter 注册 | 新增 1 个 filter |
| `web/templates/_macros.html` | 通用 badge macro | 新增 1 个 macro |
| `web/templates/dashboard.html` | dashboard 视图 | 改 2 处 phase 渲染 + 详情面板错误区扩充 |
| `tests/test_message_store.py` | store 层测试 | 新增 5 个测试 |
| `tests/test_web_dashboard.py` | dashboard 渲染断言测试 | 新增 4 个测试 |

## 依赖关系图

```
Task 1 (protocols.permanent_error)
   └─> Task 2 (store.mark_error 加 permanent 参数)
          ├─> Task 3 (store.reset_* / mark_retry_reset 同步清零)
          └─> Task 4 (engine 三处调用透传)
                 └─> Task 5 (store 测试一次性补全,覆盖 T1-T4)
                        └─> Task 6 (web/app.py phase_label filter)
                               └─> Task 7 (_macros.html content_type_tag macro)
                                      └─> Task 8 (dashboard.html 三处模板渲染)
                                             └─> Task 9 (web 渲染断言测试)
                                                    └─> Task 10 (全量验证)
```

Task 1-4 串行(数据流自底向上)。Task 5 测试在 store 改完后一次性补。Task 6/7 互相独立但都先于 Task 8(模板需要 filter+macro 都存在)。Task 8 必须先于 Task 9。Task 10 是最后验证关。

---

### Task 1: MessageRecord 加 permanent_error 字段

**Files:**
- Modify: `shared/protocols.py:310` (在 `last_error` 字段后追加)

- [ ] **Step 1: 写测试 (先 TDD)**

在 `tests/test_message_store.py` 文件最末尾(行 590 后)追加一个新测试,验证新字段默认值。新测试紧贴现有 `test_record_has_retry_and_last_error_defaults`(L308-322)的风格。

```python
def test_record_has_permanent_error_default() -> None:
    """MessageRecord 新字段 permanent_error 默认 False（向后兼容）。"""
    from shared.protocols import MessageRecord

    r = MessageRecord(
        msg_id="x",
        platform="bili",
        content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED,
        pubdate=0,
        title="t",
        author="a",
    )
    assert r.permanent_error is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_message_store.py::test_record_has_permanent_error_default -v`
Expected: FAIL with `AttributeError: 'MessageRecord' object has no attribute 'permanent_error'` 或 dataclass 不接受 kwarg 的 TypeError。

- [ ] **Step 3: 加字段**

编辑 `shared/protocols.py`,在第 310 行 `last_error: str = ""` 之后追加新字段:

```python
    # 最近一次可重试失败的错误信息（与 error 字段区分：error 表示永久失败，cron 跳过）
    last_error: str = ""
    # 是否为永久失败（engine mark_error 时透传）：
    # - True: handler 主动标记 (ctx.permanent_error) 或 retry 耗尽，cron 跳过
    # - False: 默认 / 临时失败（仅 last_error）/ reset 后恢复
    # UI 用于区分"永久错误"与"可重试错误"两种视觉状态。
    permanent_error: bool = False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_message_store.py::test_record_has_permanent_error_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add shared/protocols.py tests/test_message_store.py
git commit -m "feat(protocols): add permanent_error field to MessageRecord"
```

---

### Task 2: MessageStore.mark_error 加 permanent 参数

**Files:**
- Modify: `shared/message_store.py:73-106` (`_msg_from_dict` 反序列化补字段)
- Modify: `shared/message_store.py:273-279` (`mark_error` 签名 + 写入)
- Depends on: Task 1

- [ ] **Step 1: 写测试**

在 `tests/test_message_store.py` 末尾追加永久错误相关测试块。先加 `_msg_from_dict` 反序列化和 `mark_error` 默认行为两个测试:

```python
# ── permanent_error (plan 2026-06-30-webui-message-state-display) ──


def test_msg_from_dict_loads_permanent_error(store: MessageStore) -> None:
    """_msg_from_dict 必须把存储中的 permanent_error 反序列化进 MessageRecord。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
        "permanent_error": True,
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is True


def test_msg_from_dict_defaults_permanent_error_when_missing(store: MessageStore) -> None:
    """旧 messages.json 兼容：缺 permanent_error 字段时默认 False。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is False


def test_mark_error_default_not_permanent(store: MessageStore) -> None:
    """mark_error 不传 permanent 时默认 False（保持向后兼容）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "some error")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.error == "some error"
    assert msg.permanent_error is False  # 默认不标永久


def test_mark_error_with_permanent_true(store: MessageStore) -> None:
    """mark_error(permanent=True) 必须同时写 error 和 permanent_error=True。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "handler 标记永久失败", permanent=True)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.error == "handler 标记永久失败"
    assert msg.permanent_error is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_message_store.py -k permanent_error -v`
Expected: 4 个测试中至少 `test_mark_error_with_permanent_true` FAIL(`permanent_error` 字段未被写入,断言 is False ≠ True),`test_msg_from_dict_loads_permanent_error` 也 FAIL。`test_mark_error_default_not_permanent` 可能 PASS(因为字段默认 False)。

- [ ] **Step 3: 改 `_msg_from_dict`**

编辑 `shared/message_store.py` 第 105 行 `last_error=data.get("last_error", ""),` 之后追加一行(在 `)` 闭合之前):

```python
            retry_count=data.get("retry_count", 0),
            last_error=data.get("last_error", ""),
            permanent_error=data.get("permanent_error", False),
        )
```

- [ ] **Step 4: 改 `mark_error` 签名**

编辑 `shared/message_store.py` 第 273-279 行,把整个 `mark_error` 方法替换为:

```python
    def mark_error(self, msg_id: str, error: str, *, permanent: bool = False) -> None:
        """记录消息的错误信息。

        Args:
            msg_id: 消息 ID
            error: 错误文本
            permanent: True 表示永久失败（handler 主动标记或 retry 耗尽），
                cron 将跳过此消息；False（默认）保持向后兼容。
                语义与 ``error`` 字段独立：error 文本始终写入，permanent_error
                仅在 permanent=True 时置位。
        """
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["error"] = error
        if permanent:
            self._messages[msg_id]["permanent_error"] = True
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True
```

注意:用 keyword-only(`*,`)签名,与 D1 决策"加 permanent 参数"一致,且强制调用方用 `permanent=...` 显式传值,避免位置参数误用。`permanent=False` 时不显式写 `permanent_error=False`(避免无谓的 JSON 字段污染;依赖 _msg_from_dict 的 `.get(..., False)` 兜底)。reset 路径(Task 3)才显式写 False 清零。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_message_store.py -k permanent_error -v`
Expected: 4 个测试全部 PASS。

- [ ] **Step 6: 跑全量 store 测试确认无回归**

Run: `uv run pytest tests/test_message_store.py -v`
Expected: 全部 PASS(包含原有 retry_count/reset 测试和新增 permanent_error 测试)。

- [ ] **Step 7: Commit**

```bash
git add shared/message_store.py tests/test_message_store.py
git commit -m "feat(store): mark_error accepts permanent flag, persist permanent_error"
```

---

### Task 3: reset_* / mark_retry_reset 同步清零 permanent_error

**Files:**
- Modify: `shared/message_store.py:335-342` (`mark_retry_reset`)
- Modify: `shared/message_store.py:344-361` (`reset_to_phase`)
- Modify: `shared/message_store.py:363-401` (`reset_specific`)
- Depends on: Task 2

- [ ] **Step 1: 写测试**

在 `tests/test_message_store.py` 末尾追加三个清零测试:

```python
def test_mark_retry_reset_clears_permanent_error(store: MessageStore) -> None:
    """handler 成功后 mark_retry_reset 必须同步清零 permanent_error。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "fail", permanent=True)
    store.mark_retry_reset("bili:BV1")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is False


def test_reset_to_phase_clears_permanent_error(store: MessageStore) -> None:
    """reset_to_phase 必须同步清零 permanent_error（与 retry_count/last_error 一致）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "fail", permanent=True)
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)

    store.reset_to_phase(Phase.DOWNLOADED)

    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is False


def test_reset_specific_clears_permanent_error(store: MessageStore) -> None:
    """reset_specific 必须同步清零 permanent_error（手动重跑清状态）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    store.mark_error("bili:BV1", "summary failed", permanent=True)

    store.reset_specific(["bili:BV1"], Phase.SUMMARIZED)

    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.error == ""
    assert msg.permanent_error is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_message_store.py -k "clears_permanent_error" -v`
Expected: 3 个测试全部 FAIL(`permanent_error` 仍是 True,因为 reset 方法还没写清零逻辑)。

- [ ] **Step 3: 改 `mark_retry_reset`**

编辑 `shared/message_store.py` 第 335-342 行,把整个方法替换为:

```python
    def mark_retry_reset(self, msg_id: str) -> None:
        """handler 成功后重置 retry_count、last_error 和 permanent_error。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["retry_count"] = 0
        self._messages[msg_id]["last_error"] = ""
        self._messages[msg_id]["permanent_error"] = False
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True
```

- [ ] **Step 4: 改 `reset_to_phase`**

编辑 `shared/message_store.py` 第 344-361 行,把整个方法替换为(在 `data["last_error"] = ""` 后加一行):

```python
    def reset_to_phase(self, target: Phase, platform: str | None = None) -> None:
        """将所有阶段 >= target 的消息回退到 target 阶段，清除 error。

        Args:
            target: 目标阶段
            platform: 可选，仅回退指定平台的消息
        """
        for msg_id, data in list(self._messages.items()):
            if platform is not None and data.get("platform") != platform:
                continue
            current_phase = data.get("phase", Phase.DISCOVERED.value)
            if current_phase >= target.value:
                data["phase"] = target.value
                data["error"] = ""
                data["retry_count"] = 0
                data["last_error"] = ""
                data["permanent_error"] = False
                data["updated_at"] = time.time()
                self._dirty = True
```

- [ ] **Step 5: 改 `reset_specific`**

编辑 `shared/message_store.py` 第 384-400 行的循环体,在 `data["last_error"] = ""` 后加一行。改后的 for 循环体(L386-399)应为:

```python
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
            data["retry_count"] = 0
            data["last_error"] = ""
            data["permanent_error"] = False
            data["updated_at"] = time.time()
            self._dirty = True
            count += 1
        self.save()
        return count
```

注意:只改 for 循环体内新增一行 `data["permanent_error"] = False`,方法签名/docstring/return 不动。

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_message_store.py -k "clears_permanent_error or reset" -v`
Expected: 新增 3 个 + 原有 `test_reset_to_phase_*` / `test_reset_specific_*` 全部 PASS。

- [ ] **Step 7: 跑全量 store 测试确认无回归**

Run: `uv run pytest tests/test_message_store.py -v`
Expected: 全部 PASS。

- [ ] **Step 8: Commit**

```bash
git add shared/message_store.py tests/test_message_store.py
git commit -m "feat(store): reset_* and mark_retry_reset clear permanent_error"
```

---

### Task 4: engine 三处 mark_error 调用透传 permanent=True

**Files:**
- Modify: `core/engine.py:155` (missing handler 分支)
- Modify: `core/engine.py:171` (ctx.permanent_error 分支)
- Modify: `core/engine.py:179` (retry 耗尽分支)
- Depends on: Task 2

**关键设计说明:** brief D1 说"engine 调用处 `permanent=ctx.permanent_error` 透传"。但细看 engine 代码,这三个 `mark_error` 调用点都在「永久失败」语义的分支里:

- **L155** (missing handler): 没有 handler = 无法处理 = 永久失败,与 ctx.permanent_error 无关,直接 `permanent=True`。
- **L171** (`if ctx.permanent_error:`): 进入此分支的前提就是 `ctx.permanent_error is True`,所以这里 `permanent=True` 是恒真的(等价于 `permanent=ctx.permanent_error` 但更直白)。
- **L179** (retry_count >= MAX_SUMMARY_RETRIES): 重试耗尽 = 永久失败,`permanent=True`。

三处都用字面量 `permanent=True`,不用 `permanent=ctx.permanent_error`(后者在 L155/L179 语义错误,L171 是恒真啰嗦)。这是对 brief 措辞的精确化,语义与决策一致。

- [ ] **Step 1: 改 L155 (missing handler 分支)**

编辑 `core/engine.py` 第 155 行,把:

```python
                ctx.error = f"missing handler: {msg.platform}/{next_phase.name}"
                store.mark_error(msg.msg_id, ctx.error)
                store.save()
                break
```

改为:

```python
                ctx.error = f"missing handler: {msg.platform}/{next_phase.name}"
                store.mark_error(msg.msg_id, ctx.error, permanent=True)
                store.save()
                break
```

- [ ] **Step 2: 改 L171 (ctx.permanent_error 分支)**

编辑 `core/engine.py` 第 171 行,把:

```python
                if ctx.permanent_error:
                    store.mark_error(msg.msg_id, ctx.error)
                    logger.warning(
                        "⛔ %s:%s 永久失败（handler 标记 permanent_error）: %s（cron 将跳过）",
```

改为:

```python
                if ctx.permanent_error:
                    store.mark_error(msg.msg_id, ctx.error, permanent=True)
                    logger.warning(
                        "⛔ %s:%s 永久失败（handler 标记 permanent_error）: %s（cron 将跳过）",
```

- [ ] **Step 3: 改 L179 (retry 耗尽分支)**

编辑 `core/engine.py` 第 179 行,把:

```python
                elif current_count + 1 >= MAX_SUMMARY_RETRIES:
                    store.mark_error(msg.msg_id, ctx.error)
                    logger.warning(
                        "⛔ %s:%s 连续失败 %d 次达到上限，标记永久错误（cron 将跳过）",
```

改为:

```python
                elif current_count + 1 >= MAX_SUMMARY_RETRIES:
                    store.mark_error(msg.msg_id, ctx.error, permanent=True)
                    logger.warning(
                        "⛔ %s:%s 连续失败 %d 次达到上限，标记永久错误（cron 将跳过）",
```

注意:**不改 logger.warning 的文案**(AGENTS.md: 不改已有 error message 文本)。本次只加 `permanent=True` kwarg。

- [ ] **Step 4: 跑 engine 现有测试确认无回归**

Run: `uv run pytest tests/test_engine.py -v -k "permanent_error or retry"`
Expected: 现有 `test_handler_permanent_error_marks_error_immediately` (L646) 和 `test_bili_download_access_limited_propagates_permanent_error` (L722) 等 retry/permanent 测试全部 PASS(它们断言 `mark_error` 被调用,不断言 permanent kwarg,所以新加 kwarg 不破坏)。

- [ ] **Step 5: 跑全量测试确认无回归**

Run: `uv run pytest -x`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add core/engine.py
git commit -m "feat(engine): mark_error calls pass permanent=True for permanent failures"
```

---

### Task 5: store 层 permanent_error 测试一次性补全验证

**Files:**
- Test: `tests/test_message_store.py` (Task 1-4 已逐步加完测试)

- [ ] **Step 1: 跑 store 全量测试**

Run: `uv run pytest tests/test_message_store.py -v`
Expected: 全部 PASS,包含:
- 原有 ~40 个测试(add_new / mark_phase / mark_error / reset / retry / query_messages / reset_specific 等)
- 新增 `test_record_has_permanent_error_default` (Task 1)
- 新增 4 个 permanent_error 测试 (Task 2: load/default/mark_default/mark_with_permanent)
- 新增 3 个 clears_permanent_error 测试 (Task 3: retry_reset/reset_to_phase/reset_specific)

共 8 个新测试。

- [ ] **Step 2: 跑 lint 和 type check 确认无问题**

Run: `uv run ruff check shared/message_store.py shared/protocols.py core/engine.py tests/test_message_store.py`
Expected: "All checks passed!"

Run: `uv run pyright`
Expected: 0 errors(无参数!见 AGENTS.md)。

- [ ] **Step 3: 如果有任何失败,修复后重新跑 Step 1-2,直到全绿**

无代码改动则跳过 commit。如果有修复,commit message: `test: cover permanent_error round-trip and reset semantics`。

---

### Task 6: web/app.py 新增 phase_label filter

**Files:**
- Modify: `web/app.py:74` (在 `phase_color` filter 注册后并列加 `phase_label`)
- Depends on: 无 (与 Task 7 互相独立,但都先于 Task 8)

- [ ] **Step 1: 加 filter 函数和注册**

编辑 `web/app.py`,在第 74 行 `TEMPLATES.env.filters["phase_color"] = _phase_color` 之后(L75 空行处)追加:

```python


def _phase_label(phase: Any) -> str:
    """Map a Phase enum value (or its .name) to a Chinese display label.

    Companion to ``phase_color``: phase_color picks the badge color, phase_label
    picks the human-readable text. Keeps templates free of hardcoded enum names.
    Returns the raw .name as fallback for unknown phases (forward-compat).
    """
    name = phase.name if hasattr(phase, "name") else str(phase)
    mapping = {
        "DISCOVERED": "已发现",
        "DOWNLOADED": "已下载",
        "TRANSCRIBED": "已转写",
        "SUMMARIZED": "已摘要",
        "PUSHED": "已推送",
    }
    return mapping.get(name, name)


TEMPLATES.env.filters["phase_label"] = _phase_label
```

注意:函数签名 `(phase: Any)` 与现有 `_phase_color` 完全一致(同样用 `hasattr(phase, "name")` 兜底),`Any` 类型从 `typing` 导入(已在 L8 `from typing import Any` 导入,无需新加 import)。fallback 用 `name` 而非 `"未知"` —— 与 `_phase_color` 的 `mapping.get(name, "gray")` 风格一致(优雅降级而非 panic)。

- [ ] **Step 2: 手动验证 filter 注册成功**

Run: `uv run python -c "from web.app import TEMPLATES; print('phase_label' in TEMPLATES.env.filters); print(TEMPLATES.env.filters['phase_label'].__name__)"`
Expected:
```
True
_phase_label
```

- [ ] **Step 3: Commit**

```bash
git add web/app.py
git commit -m "feat(web): add phase_label Jinja2 filter for Chinese phase names"
```

---

### Task 7: _macros.html 新增 content_type_tag macro

**Files:**
- Modify: `web/templates/_macros.html:22` (在 `platform_tag` macro 后并列加 `content_type_tag`)
- Depends on: 无 (与 Task 6 互相独立)

- [ ] **Step 1: 加 macro**

编辑 `web/templates/_macros.html`,在第 22 行 `platform_tag` macro 结束(`{%- endmacro %}`)之后追加 `content_type_tag` macro:

```jinja

{% macro content_type_tag(content_type) -%}
{# Map ContentType enum (.name) to a Chinese label + colored badge.
   Mirrors platform_tag's compact mono-badge style. Used in the detail panel
   meta row to give a visual cue for VIDEO vs TEXT content.
   Caller must pass a ContentType enum instance (uses .name attribute). #}
{%- set label_map = {
  "VIDEO": "视频",
  "TEXT": "图文"
} -%}
{%- set color_map = {
  "VIDEO": "bg-purple-50 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300",
  "TEXT": "bg-teal-50 text-teal-700 dark:bg-teal-900/30 dark:text-teal-300"
} -%}
{%- set ct_name = content_type.name -%}
{%- set label = label_map.get(ct_name, ct_name) -%}
{%- set color = color_map.get(ct_name, "bg-gray-100 text-gray-700 dark:bg-gray-700/40 dark:text-gray-300") -%}
<span class="inline-flex items-center px-2 py-0.5 rounded-[10px] text-xs font-medium {{ color }}">{{ label }}</span>
{%- endmacro %}
```

**实现说明(为什么这样写):**

- **不能用 `hasattr()` 或 `is mapping` 判断:** Jinja2 不支持 Python 内置的 `hasattr()`,也不支持 `is mapping` 这种链式 `is defined` 语法。这些是常见误区,直接写会运行时崩溃。所以本 macro 不做任何防御性的类型分派。
- **直接用 `content_type.name` 是安全的:** dashboard 渲染时 `msg.content_type` 来自 `MessageRecord`,其类型注解是 `ContentType`,经 `MessageStore._msg_from_dict` 反序列化后就是 enum 实例。调用方(见 Task 8 Step 2 `content_type_tag(msg.content_type)`)保证传入 enum,模板内无需也无意义再判类型。
- **fallback 用 `ct_name` 而非中文兜底文案:** 与 `platform_tag` 的 `mapping.get(...)` 风格一致(未知 enum name 原样透传,优雅降级而非 panic),便于后续新增 ContentType 时只改 label_map/color_map 即可生效。

- [ ] **Step 2: Commit**

```bash
git add web/templates/_macros.html
git commit -m "feat(web): add content_type_tag macro for VIDEO/TEXT visual indicator"
```

(此 task 无独立单测;渲染断言在 Task 9 统一覆盖。)

---

### Task 8: dashboard.html 三处模板渲染改造

**Files:**
- Modify: `web/templates/dashboard.html:2` (import 新 macro)
- Modify: `web/templates/dashboard.html:86` (详情面板 meta 行 phase.name → phase_label + 加 content_type_tag)
- Modify: `web/templates/dashboard.html:100-126` (详情面板错误区加 retry/last_error + permanent 区分)
- Modify: `web/templates/dashboard.html:222` (表格 phase 单元格 phase.name → phase_label)
- Depends on: Task 6, Task 7

**注意:** `dashboard.html:47` 的 `phase.name` 在 deprecated `msg_hover_card` macro 内(L30-37 注释明确说"not called anywhere"),**不改**。G3 决策只覆盖活跃渲染点(L86 详情面板 + L222 表格)。

- [ ] **Step 1: 改 import 行**

编辑 `web/templates/dashboard.html` 第 2 行,把:

```jinja
{% from "_macros.html" import stat_card, badge, platform_tag %}
```

改为:

```jinja
{% from "_macros.html" import stat_card, badge, platform_tag, content_type_tag %}
```

- [ ] **Step 2: 改详情面板 meta 行 (L86 区域)**

编辑 `web/templates/dashboard.html` 第 81-87 行。把:

```jinja
      <div class="text-xs text-[var(--text-secondary)] mt-1 flex flex-wrap items-center gap-1.5">
        <span class="font-mono">{{ msg.platform }}</span>
        <span>·</span>
        <span>{{ msg.author }}</span>
        <span>·</span>
        <span>{{ msg.phase.name }}</span>
      </div>
```

改为(加 content_type_tag + phase.name → phase|phase_label):

```jinja
      <div class="text-xs text-[var(--text-secondary)] mt-1 flex flex-wrap items-center gap-1.5">
        <span class="font-mono">{{ msg.platform }}</span>
        {{ content_type_tag(msg.content_type) }}
        <span>·</span>
        <span>{{ msg.author }}</span>
        <span>·</span>
        <span>{{ msg.phase | phase_label }}</span>
      </div>
```

- [ ] **Step 3: 改详情面板错误区 (L100-126)**

编辑 `web/templates/dashboard.html` 第 100-126 行。把整个 `{% if msg.error %}...{% endif %}` 块替换为扩充版本(加 permanent_error 视觉区分 + retry_count/last_error 兄弟分支):

```jinja
  {% if msg.error %}
  <div class="mb-3">
    <div class="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)] mb-1 flex items-center gap-1.5">
      <span>错误</span>
      {% if msg.permanent_error %}
      <span class="inline-flex items-center px-1.5 py-0.5 rounded-[6px] text-[9px] font-medium bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300">永久</span>
      {% else %}
      <span class="inline-flex items-center px-1.5 py-0.5 rounded-[6px] text-[9px] font-medium bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300">可重试</span>
      {% endif %}
    </div>
    <div class="text-xs text-red-500 leading-relaxed whitespace-pre-wrap">{{ msg.error }}</div>
    {# 错误恢复入口 (UX: Try again button + help link)。
       HTMX hx-target="this" + hx-swap="outerHTML" 让返回的 span 原地替换整个按钮容器。
       hx-disabled-elt 防止请求中重复点击。
       base.html 全局 htmx:beforeRequest hook 会自动给按钮加 spin + disabled。 #}
    <div class="mt-2 flex items-center gap-3"
         hx-post="/messages/{{ msg.msg_id | urlencode }}/retry"
         hx-target="this"
         hx-swap="outerHTML"
         hx-disabled-elt="button">
      <button type="button"
              class="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-[6px] text-xs font-medium bg-[var(--color-primary)]/10 text-[var(--color-primary)] hover:bg-[var(--color-primary)]/20 cursor-pointer transition-colors duration-200"
              aria-label="重新处理此消息">
        <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="23 4 23 10 17 10"/>
          <polyline points="1 20 1 14 7 14"/>
          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
        </svg>
        重试此消息
      </button>
      <span class="text-[10px] text-[var(--text-tertiary)]">cron 下次循环将重新处理当前阶段</span>
    </div>
  </div>
  {% elif msg.retry_count > 0 %}
  {# G3: 临时失败（未达永久）展示重试进度。与 {% if msg.error %} 兄弟分支，
       error 为空时才走到这里（cron 仍会重试）。 #}
  <div class="mb-3">
    <div class="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)] mb-1 flex items-center gap-1.5">
      <span>重试中</span>
      <span class="inline-flex items-center px-1.5 py-0.5 rounded-[6px] text-[9px] font-medium bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300">第 {{ msg.retry_count }} 次</span>
    </div>
    <div class="text-xs text-orange-500 leading-relaxed whitespace-pre-wrap">{{ msg.last_error }}</div>
  </div>
  {% endif %}
```

关键设计点:
- `{% if msg.error %}` / `{% elif msg.retry_count > 0 %}` 互斥(error 非空说明已永久失败,不再展示 retry 历史;error 空但 retry_count>0 说明临时失败等下次 cron)。
- "永久"/"可重试"小 tag 紧贴"错误"label,符合 D6"区分 permanent/临时错误 UI"决策。
- retry 块用橙色系(与"可重试" tag 颜色一致),永久错误用红色系(保留原有 `text-red-500`)。
- **保留原重试按钮 + htmx 行为不动**(只在外层包了新 tag,按钮 div 内部 HTMX 属性一字未改)。

- [ ] **Step 4: 改表格 phase 单元格 (L222)**

编辑 `web/templates/dashboard.html` 第 222 行,把:

```jinja
          <td class="px-5 py-3">{{ badge(msg.phase.name, msg.phase | phase_color) }}</td>
```

改为:

```jinja
          <td class="px-5 py-3">{{ badge(msg.phase | phase_label, msg.phase | phase_color) }}</td>
```

注意:`phase_color` 不动(D4 决策:颜色仍只看 phase,不引入 permanent 维度的颜色变化)。仅把 `msg.phase.name` 替换为 `msg.phase | phase_label`,让 badge 文本变中文。

- [ ] **Step 5: 手动启服务目检(可选,有 dev server 时)**

Run: `uv run uvicorn web.app:create_app --factory --port 8000`(另开终端)
访问 `http://localhost:8000`,登录后检查:
- 最近消息表格 phase 列显示中文(已发现/已下载/已转写/已摘要/已推送)
- 点详情按钮,meta 行有"视频"或"图文"紫色/青色 tag,phase 显示中文
- 构造一条 `error != ""` 的消息,详情面板"错误"标签旁应显示"永久"或"可重试"红色/橙色小 tag
- 构造一条 `retry_count > 0 and error == ""` 的消息,详情面板应显示"重试中 第 N 次"块

若无可手动启服务的环境,跳过此步,依赖 Task 9 自动测试覆盖。

- [ ] **Step 6: Commit**

```bash
git add web/templates/dashboard.html
git commit -m "feat(webui): show Chinese phase label, content_type tag, retry/permanent state"
```

---

### Task 9: dashboard 渲染断言测试

**Files:**
- Test: `tests/test_web_dashboard.py` (在现有 `TestDashboard` class 内追加测试方法)

- [ ] **Step 1: 加测试 — phase 中文 label 渲染**

在 `tests/test_web_dashboard.py` 的 `TestDashboard` class 内(L40 之前 class 结束)追加。先加一个 helper 和第一个测试:

```python
    def _seed_message(
        self,
        data_dir: Path,
        *,
        msg_id: str = "bili:BVtest",
        platform: str = "bili",
        content_type: ContentType = ContentType.VIDEO,
        phase: Phase = Phase.SUMMARIZED,
        error: str = "",
        retry_count: int = 0,
        last_error: str = "",
        permanent_error: bool = False,
        title: str = "测试视频",
        author: str = "测试UP",
    ) -> None:
        """在 data_dir 下种入一条可定制状态的消息（绕过 add_new 直接写 store 内部 dict）。"""
        import time as _time

        from shared.message_store import MessageStore

        store = MessageStore(data_dir)
        store._messages[msg_id] = {
            "platform": platform,
            "content_type": content_type.value,
            "phase": phase.value,
            "pubdate": int(_time.time()),
            "title": title,
            "author": author,
            "created_at": 0.0,
            "updated_at": _time.time(),
            "error": error,
            "retry_count": retry_count,
            "last_error": last_error,
            "permanent_error": permanent_error,
        }
        store._dirty = True
        store.save()
```

(放在 class 内首个测试 `test_dashboard_returns_200` 之前作为 method,下划线开头表示 helper 不被 pytest 收集。)

注意:`tests/test_web_dashboard.py` 顶部需补 import。检查现有 import(L1-10):已有 `from pathlib import Path` / `from unittest.mock import AsyncMock, patch` / `pytest` / `AsyncClient` / `create_app` / `set_password`。需要新增:

```python
from shared.protocols import ContentType, Phase
```

加在 L10 `from web.auth import set_password` 之后。

然后加第一个真测试:

```python
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_chinese_phase_label(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G1: 表格 phase 单元格渲染中文阶段名（phase_label filter）。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        # 种入一条 SUMMARIZED 消息
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        self._seed_message(data_dir, phase=Phase.SUMMARIZED)

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        # 中文 phase label 出现，英文 enum name 不再以裸露形式出现在 phase 单元格
        assert "已摘要" in resp.text
```

注意:此测试不用 `client` fixture(因为需要每个测试独立的 data_dir),而是内联 setup。所有新测试沿用此模式。

- [ ] **Step 2: 跑测试确认通过**

Run: `uv run pytest tests/test_web_dashboard.py::TestDashboard::test_dashboard_renders_chinese_phase_label -v`
Expected: PASS(`已摘要` 出现在表格 phase 单元格 + 详情面板 meta 行)。

如果 FAIL("已摘要" 不在 resp.text),检查 Task 6/8 是否完成、`phase_label` filter 是否注册。

- [ ] **Step 3: 加测试 — content_type tag 渲染**

继续在 class 内追加:

```python
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_content_type_tag(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G4: 详情面板 meta 行渲染 content_type tag (视频/图文)。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # VIDEO 和 TEXT 各一条
        self._seed_message(
            data_dir, msg_id="bili:V1", content_type=ContentType.VIDEO, phase=Phase.PUSHED
        )
        self._seed_message(
            data_dir, msg_id="xhs:N1", platform="xhs", content_type=ContentType.TEXT, phase=Phase.PUSHED
        )

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        assert "视频" in resp.text
        assert "图文" in resp.text
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_web_dashboard.py::TestDashboard::test_dashboard_renders_content_type_tag -v`
Expected: PASS。

- [ ] **Step 5: 加测试 — 永久错误 tag 渲染**

继续追加:

```python
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_permanent_error_tag(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G2: 详情面板错误区显示 永久/可重试 tag (区分 permanent_error)。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # 永久失败 + 可重试失败 各一条
        self._seed_message(
            data_dir,
            msg_id="bili:perm",
            phase=Phase.SUMMARIZED,
            error="transcribe 模型超时",
            permanent_error=True,
            title="永久失败",
        )
        self._seed_message(
            data_dir,
            msg_id="bili:temp",
            phase=Phase.SUMMARIZED,
            error="临时网络错误",
            permanent_error=False,
            title="临时失败",
        )

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        assert "永久" in resp.text
        assert "可重试" in resp.text
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_web_dashboard.py::TestDashboard::test_dashboard_renders_permanent_error_tag -v`
Expected: PASS。

- [ ] **Step 7: 加测试 — retry_count/last_error 渲染**

继续追加:

```python
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_retry_progress(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G3: 临时失败 (error 空, retry_count>0) 显示重试进度块。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        self._seed_message(
            data_dir,
            msg_id="bili:retry",
            phase=Phase.SUMMARIZED,
            error="",  # 关键：未永久失败
            retry_count=2,
            last_error="API timeout",
            permanent_error=False,
            title="重试中",
        )

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        assert "重试中" in resp.text  # 既是 title 也匹配 label
        assert "第 2 次" in resp.text
        assert "API timeout" in resp.text
```

注意:`"重试中"` 这个字符串既是种入消息的 title 也会命中 label,断言可能因为 title 命中而误绿。为加强特异性,加 `"第 2 次"` 和 `"API timeout"` 两个独有字符串作为辅助断言。

- [ ] **Step 8: 跑新增的全部 4 个测试**

Run: `uv run pytest tests/test_web_dashboard.py -v`
Expected: 5 个测试(原 1 个 + 新增 4 个)全部 PASS。

- [ ] **Step 9: Commit**

```bash
git add tests/test_web_dashboard.py
git commit -m "test(web): assert dashboard renders phase_label, content_type tag, error state"
```

---

### Task 10: 全量验证

**Files:** 无(纯验证)

- [ ] **Step 1: 跑全量测试**

Run: `uv run pytest -x`
Expected: 全部 PASS,无 fail无 error。

- [ ] **Step 2: 跑 lint**

Run: `uv run ruff check .`
Expected: "All checks passed!"

- [ ] **Step 3: 跑 format check(确认无未格式化代码)**

Run: `uv run ruff format --check .`
Expected: 所有文件已格式化。若有 diff,跑 `uv run ruff format .` 修复后再 commit。

- [ ] **Step 4: 跑 type check**

Run: `uv run pyright`
Expected: 0 errors(无参数!)。

- [ ] **Step 5: 如有 format 修复,补一个 commit**

```bash
git add -A
git commit -m "style: ruff format"
```

若无修复,跳过此步。

---

## Self-Review

### Spec 覆盖率核对

| 缺口 / 决策 | 覆盖任务 | 状态 |
|---|---|---|
| G1 阶段名英文裸露 | T6 (filter) + T8 Step 2/4 (模板 L86/L222) | ✓ |
| G2 permanent_error 持久化 | T1 (字段) + T2 (mark_error 写入) + T3 (reset 清零) + T4 (engine 透传) + T8 Step 3 (UI tag) | ✓ |
| G3 retry_count/last_error UI | T8 Step 3 (elif retry_count>0 分支) | ✓ |
| G4 content_type 视觉指示 | T7 (macro) + T8 Step 2 (详情面板 meta 行) | ✓ |
| D1=A mark_error 加 permanent 参数 | T2 | ✓ |
| D2=A reset_*/mark_retry_reset 清零 | T3 | ✓ |
| D3=A phase_label filter | T6 | ✓ |
| D4=A phase_color 不动 | T8 Step 4 注释明确不动 | ✓ |
| D5=A 仅详情面板加 content_type_tag | T8 Step 2 (表格不动) | ✓ |
| D6=A retry_count 兄弟分支 | T8 Step 3 (elif 分支) | ✓ |
| G5 transcript 跳过 | 不涉及(用户决定不做) | ✓ N/A |

全部决策有任务对应,无遗漏。

### Placeholder 扫描

- 所有"Write the failing test"步骤都有完整可运行的 Python 代码,无 `...` / `TODO` / `implement here`。
- 所有"改 XX 行"都有 before/after 代码块,无"add appropriate handling"。
- 模板改造有完整 jinja 片段,无"similar to Task N"。
- 命令都有 `Run:` + `Expected:`,无"run the tests"(给出具体 pytest 路径)。

### 类型 / 签名一致性

- `permanent_error: bool = False` (T1) ↔ `data.get("permanent_error", False)` (T2 Step 3) ↔ `permanent: bool = False` kwarg (T2 Step 4) ↔ `permanent=True` (T4) — 一致 ✓
- `_phase_label(phase: Any) -> str` (T6) ↔ `msg.phase | phase_label` (T8) — 一致 ✓
- `content_type_tag(content_type)` (T7) ↔ `content_type_tag(msg.content_type)` (T8 Step 2) — 一致 ✓
- `mark_error(self, msg_id, error, *, permanent=False)` (T2) ↔ `store.mark_error(msg.msg_id, ctx.error, permanent=True)` (T4) — 一致 ✓ (kwarg 用 keyword-only `*,` 强制)
- T3 三处 reset 都写 `permanent_error = False`,与 T2 默认不写 False 的策略不冲突(reset 是显式清零语义) ✓

### 已知边界 / 风险

1. **engine.py L155** "missing handler" 分支用 `permanent=True`,brief D1 措辞是 `permanent=ctx.permanent_error`,但 ctx 在该路径无 permanent_error 语义(根本没跑 handler)。我用 `permanent=True` 是对的(missing handler 本质永久失败),已在 Task 4 设计说明里讲清。
2. **dashboard.html L47** deprecated macro 不改,已在 Task 8 注意里说明。
3. **content_type_tag macro** 不做防御性类型分派(不使用 Jinja2 不支持的 `hasattr()`),直接用 `content_type.name`,安全性靠调用方传 enum 实例保证。详见 Task 7 Step 1 的实现说明。
4. **Task 9 测试** 不复用 `client` fixture(它没传 data_dir),改用内联 setup + `_seed_message` helper。helper 用 `_` 前缀避免 pytest 收集。

---

## 执行建议

每个 task 独立 commit,共 ~10 个 commits。Task 1-5 是数据层 + 测试,可由一个 fixer 串行做;Task 6-9 是 web 层,可由另一个 fixer 并行做(但 Task 8 依赖 Task 6+7 完成)。Task 10 最后统一验证。

推荐用 subagent-driven-development:每个 task 派一个 fresh subagent,两阶段 review(实现 → 验证)。
