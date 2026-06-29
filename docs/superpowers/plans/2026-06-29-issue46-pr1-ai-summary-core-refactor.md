# Issue #46 PR-1: AI 摘要触发条件核心架构改造

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 ContentType.DYNAMIC,把 bili 动态按是否含视频归类到 TEXT/VIDEO,简化 PHASE_FLOW 为 2 条路径

**Architecture:** PHASE_FLOW 用 content_type 字段单一驱动,bili_dynamic_detector 负责在检测时区分动态类型(纯文字→TEXT,含视频→反查 bvid 后注册为 VIDEO)。所有 DYNAMIC 类型特判从 transcribe_phase / summarize_phase / bili_push 移除。

**Tech Stack:** Python 3.12, asyncio, pytest, trawler 现有架构

**关联:**
- Spec: `docs/superpowers/specs/2026-06-29-ai-summary-trigger-refactor-design.md`
- Issue #46
- 当前 PHASE_FLOW: `shared/protocols.py:258-276`
- 当前 detector: `platforms/bilibili/handlers.py:53-96`

**涉及文件清单:**

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `shared/protocols.py` | Modify | 删 ContentType.DYNAMIC + 简化 PHASE_FLOW;DynamicInfo 加 `has_video` 字段 |
| `platforms/bilibili/handlers.py` | Modify | detector 分流(case 1/2/3);`bili_download` 加 bili_dyn: 前缀特判(Task 5);移除 transcribe_phase / bili_push 的 content_type 特判 |
| `platforms/bilibili/dynamic.py` | Modify | `_parse_dynamic` 返回时设 `has_video = bool(linked_bvid)` |
| `tests/test_engine.py` | Modify | Task 2: 4 处 DYNAMIC fixture → TEXT + handler phase 改 DOWNLOADED;Task 5: 新增 `test_bili_download_handles_dynamic_text_prefix`;Task 7: 新增 `test_text_message_never_reaches_transcribe_phase` |
| `tests/test_bili_dynamic_parse.py` | Create | Task 3: `_parse_dynamic` 的 has_video 字段测试(AV/WORD/DRAW 三种动态) |
| `tests/test_bili_dynamic_detector.py` | Create | Task 4: 纯文字动态注册 TEXT + mark_body;Task 6: 视频型动态 case 1/2/3 共 4 个测试 |
| `tests/test_bili_push.py` | Create | Task 8: bili_dyn push 走 dynamic URL / bili video push 走 video URL |

---

## 关键设计决策(本 plan 固化)

| # | 决策 | 理由 |
|---|---|---|
| D1 | `ContentType` 保持 `Enum` + `auto()`(spec §1 建议改 StrEnum 但项目当前是 int Enum,本 PR **不动序列化格式**,保持最小改动) | 改 StrEnum 涉及 store 序列化/反序列化、migration,超出本 PR 范围。删 DYNAMIC 后旧 messages.json 的 `content_type: 3` 反序列化会失败 → 由 D2 处理 |
| D2 | **数据重置**:用户升级 PR-1 前必须删除 `data/messages.json`(spec §7 已确认)。Release notes / PR description 必须显著标注 | 旧 DYNAMIC 消息反序列化失败,无自动迁移(spec §7 明确不做) |
| D3 | **纯文字动态走 TEXT flow**:`[DISCOVERED, DOWNLOADED, PUSHED]`。但 bili 没有合适的 download handler(现有 `bili_download` 调 video downloader 拿 mp4,对纯文字动态不适用)。**方案**:detector 注册 `bili_dyn:{id}` 消息时同步调 `store.mark_body()` 写入动态正文;`bili_download` handler 加 `bili_dyn:` 前缀特判,no-op return True | TEXT flow 必须有 DOWNLOADED 阶段。detector 已有 dyn.content,直接 mark_body 最省 DOWNLOADED handler 的重新获取成本。前缀特判比新增平台/detector 简单 |
| D4 | **视频型动态的 linked_bvid 由 `_parse_dynamic` 从 `major.archive.bvid` 直接提取**(plan Task 3 已固化),**无网络反查**。理论边缘(has_video=True 但 linked_bvid 空)由 detector 内 `if not dyn.linked_bvid: continue` 防御性跳过 | linked_bvid 已在 dyn 上,无需额外 API 调用。spec §2 case 2 描述的"反查 bvid"步骤在当前实现下天然不需要 |
| D5 | **`bili_push` 改造**:用 `msg_id.startswith("bili_dyn:")` 替代 `is_dynamic` 判断,保留动态 URL 渲染(`t.bilibili.com/{id}`)和 type 字段(`"dynamic"` 用于通知模板) | 改造后 `bili_dyn:` 前缀的消息只可能是 TEXT 类型(纯文字/图文动态)。前缀判断与 content_type 解耦,URL/通知 type 渲染仍需按前缀区分 |
| D6 | **媒体清理条件改为 `downloaded_filepath is not None`**(原 `not is_dynamic`) | TEXT 类型无视频文件(`downloaded_filepath` 始终 None),条件自然成立。语义不变,与 content_type 解耦 |
| D7 | **`dynamic_text` 字段保留**(spec §1):仍用于「视频型动态 linked_bvid 已注册」场景,把 UP 主补充文字追加到对应 VIDEO 消息 | 现有逻辑(spec §2 case 1),不变 |
| D8 | **`has_video` 字段加在 `DynamicInfo` dataclass**(spec §2 末段),不在 `_parse_dynamic` 返回 dict | `_parse_dynamic` 已返回 `DynamicInfo` dataclass,直接加字段 |
| D9 | **反查 bvid 不新增 API 模块**(spec 涉及文件清单列了 `api.py` 但项目无此文件):新增函数放在 `platforms/bilibili/dynamic.py` 顶部,命名 `resolve_bvid_from_dynamic` | 项目结构无 `platforms/bilibili/api.py`,bili API 调用分散在 monitor.py / dynamic.py / comments.py。dynamic 详情 API 与 dynamic 模块内聚 |
| D10 | **测试 fixture `ContentType.DYNAMIC` → `ContentType.TEXT`**(不是 VIDEO) | test_engine.py 现有 4 处 DYNAMIC fixture 都用于验证 phase 推进语义(TEXT flow 不含 TRANSCRIBED,符合测试原意)。VIDEO 会触发 Bug-3 rewind 逻辑,改变测试语义 |

---

## Task 1: 探索 + 现状基线

**目标**:跑现有测试套件记录基线,审计所有 DYNAMIC 引用点,生成清单供后续 task 对照。

### Step 1.1: 跑基线测试

- [ ] 运行 `uv run pytest -x -q tests/ 2>&1 | tail -20`,记录通过测试数和失败数(应全过)

```bash
uv run pytest -x -q tests/ 2>&1 | tail -20
```

预期:全部通过。把"N passed in X s"记下来,作为后续 task 的对比基线。

### Step 1.2: 审计 DYNAMIC 引用清单

- [ ] 用 grep 列出所有 `DYNAMIC` / `ContentType.DYNAMIC` / `is_dynamic` 引用:

```bash
rg -n "DYNAMIC|is_dynamic" --type py -g '!docs/**' -g '!**/plans/**'
```

预期输出(本 plan 写作时已确认):
- `shared/protocols.py:244` — `DYNAMIC = auto()` 定义
- `shared/protocols.py:271-275` — `PHASE_FLOW[ContentType.DYNAMIC]` 路径定义
- `platforms/bilibili/handlers.py:60, 87` — 注释引用
- `platforms/bilibili/handlers.py:91` — detector 注册为 DYNAMIC
- `platforms/bilibili/handlers.py:256` — `is_dynamic = ctx.msg.content_type == ContentType.DYNAMIC`
- `tests/test_engine.py:174, 192, 605, 611, 633, 648, 707` — 4 处 fixture + 注释
- `platforms/bilibili/dynamic.py:17-21, 64-65, 80` — `_DYNAMIC_TYPE_MAP` 和 `dynamic_type` 变量(这些是动态 API 的 type 字段,**不是** ContentType.DYNAMIC,**不动**)

### Step 1.3: 提交基线

无代码改动,**不提交**。仅记录基线数据供后续 task 验证。

---

## Task 2: 删除 ContentType.DYNAMIC,简化 PHASE_FLOW

**目标**:删除枚举值和 PHASE_FLOW 路径,先做最小改动让 fixture 显式失败,从而暴露所有需要更新的位置。

### Step 2.1: 写测试 — 改 test_engine.py 的 4 处 DYNAMIC fixture 为 TEXT

- [ ] 修改 `/home/zyw10/proj/trawler/tests/test_engine.py`

把以下 4 处 `ContentType.DYNAMIC` 改为 `ContentType.TEXT`(根据 D10 决策):

**改动 1** — `test_process_message_resume_from_mid_phase`(L192 附近):

```python
# 改前(L192):
    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "Test", "Author")

# 改后:
    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "Test", "Author")
```

同时更新 L194 的 `store.mark_phase(..., Phase.SUMMARIZED)`:TEXT flow `[DISCOVERED, DOWNLOADED, PUSHED]` 不含 SUMMARIZED,resume 测试要换成 `Phase.DOWNLOADED`。完整改后函数体:

```python
@pytest.mark.asyncio
async def test_process_message_resume_from_mid_phase(config: Config, store: MessageStore) -> None:
    """Should resume from current phase, not repeat completed phases.

    Uses TEXT content: TEXT phase flow excludes TRANSCRIBED/SUMMARIZED,
    so the Bug-3 VIDEO-only rewind gate never fires here and this test keeps
    verifying the pure resume semantics."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "Test", "Author")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED

    await PipelineEngine.process_message(msg, config, store)
    assert calls == ["pushed"]  # only pushed, downloaded is not repeated
```

注意:原 docstring 注释也要更新(DYNAMIC → TEXT)。

**改动 2** — `test_handler_failure_increments_retry_count`(L605 附近):

```python
# 改前(L605):
    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "T", "A")

# 改后:
    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
```

但这个测试是验证 `SUMMARIZED` handler 失败 → retry_count 增加。TEXT flow 不含 SUMMARIZED!所以需要把注册的 handler phase 改为 `Phase.DOWNLOADED`(TEXT flow 含 DOWNLOADED)。同时更新 L611-612 的注释和断言。完整改后函数体:

```python
@pytest.mark.asyncio
async def test_handler_failure_increments_retry_count(
    config: Config, store: MessageStore
) -> None:
    """handler 返回 False 且 retry_count < MAX 时:retry_count += 1，不写 error，cron 仍重试。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.error = "download 失败"
        return False

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        pytest.fail("PUSHED 不应被调用")

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    # TEXT flow=[DISCOVERED, DOWNLOADED, PUSHED]：handler 失败时 next_phase=DOWNLOADED 未推进
    assert updated.phase == Phase.DISCOVERED
    assert updated.retry_count == 1
    assert updated.last_error == "download 失败"
    assert updated.error == ""  # 关键：未达上限，不写 error
```

**改动 3** — `test_handler_failure_after_max_retries_marks_error`(L633 附近):

```python
# 改前(L633):
    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "T", "A")

# 改后:
    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
```

同样,这个测试注册的 handler 是 `Phase.SUMMARIZED`,TEXT flow 不含。改为 `Phase.DOWNLOADED`。完整改后函数体(注意 L628 的 register 和 L648 的断言也要改):

```python
@pytest.mark.asyncio
async def test_handler_failure_after_max_retries_marks_error(
    config: Config, store: MessageStore
) -> None:
    """retry_count 达到 MAX_SUMMARY_RETRIES 后：写 error，cron 永久跳过。"""
    from shared.constants import MAX_SUMMARY_RETRIES

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.error = "download 失败"
        return False

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    # 预置 retry_count = MAX - 1，下一次失败应触发 mark_error
    for _ in range(MAX_SUMMARY_RETRIES - 1):
        store.mark_retry_failure("bili:BV1", "prev fail")
    pre = store.get_message("bili:BV1")
    assert pre is not None
    assert pre.retry_count == MAX_SUMMARY_RETRIES - 1

    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.DISCOVERED  # TEXT flow 中 DOWNLOADED 未推进
    assert updated.error != ""  # 关键：达到上限，写 error
    assert "download 失败" in updated.error
    # 注：mark_error 不增加 retry_count（mark_error 只写 error 字段）。
    # engine 的「达到上限」检查用 current_count+1 >= MAX，触发后直接 mark_error，
    # 所以 retry_count 仍为预置的 MAX-1（最后一次失败的计数未写入）。
    assert updated.retry_count == MAX_SUMMARY_RETRIES - 1
```

**改动 4** — `test_handler_success_resets_retry_count`(L707 附近):

```python
# 改前(L707):
    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "T", "A")

# 改后:
    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
```

同样,这个测试注册的 handler 是 `Phase.SUMMARIZED` 和 `Phase.PUSHED`,TEXT flow 不含 SUMMARIZED。改为 `Phase.DOWNLOADED`:

```python
@pytest.mark.asyncio
async def test_handler_success_resets_retry_count(
    config: Config, store: MessageStore
) -> None:
    """handler 成功后 retry_count 必须重置为 0（之前失败过的消息恢复后清状态）。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.content_text = "成功正文"
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    store.mark_retry_failure("bili:BV1", "prev fail")
    store.mark_retry_failure("bili:BV1", "prev fail")
    pre = store.get_message("bili:BV1")
    assert pre is not None
    assert pre.retry_count == 2

    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED
    assert updated.retry_count == 0  # 重置
    assert updated.last_error == ""
```

### Step 2.2: 跑测试 — 应失败(TEXT flow 不匹配 / DYNAMIC 仍存在)

- [ ] 运行:

```bash
uv run pytest -x tests/test_engine.py -v 2>&1 | tail -40
```

预期:上述 4 个改后的测试**可能仍过**(因为它们用了 TEXT 且 handler 注册在 DOWNLOADED/PUSHED),但 `shared/protocols.py` 还有 DYNAMIC,后续 handler 测试会受影响。**关键是这一步不会比基线更差**。

### Step 2.3: 实现 — 删 DYNAMIC,简化 PHASE_FLOW

- [ ] 修改 `/home/zyw10/proj/trawler/shared/protocols.py`

```python
# 改前(L239-244):
class ContentType(Enum):
    """内容类型"""

    VIDEO = auto()  # B站视频 / XHS视频笔记 — 完整五阶段
    TEXT = auto()  # 微博 / XHS图文笔记 — 三阶段（下载+推送）
    DYNAMIC = auto()  # B站动态 — 三阶段（摘要+推送，无下载/转写）

# 改后:
class ContentType(Enum):
    """内容类型"""

    VIDEO = auto()  # 任何含视频附件的内容 — 完整五阶段
    TEXT = auto()  # 纯文字/图片 — 三阶段（下载+推送）
```

```python
# 改前(L258-276):
PHASE_FLOW: dict[ContentType, list[Phase]] = {
    ContentType.VIDEO: [
        Phase.DISCOVERED,
        Phase.DOWNLOADED,
        Phase.TRANSCRIBED,
        Phase.SUMMARIZED,
        Phase.PUSHED,
    ],
    ContentType.TEXT: [
        Phase.DISCOVERED,
        Phase.DOWNLOADED,
        Phase.PUSHED,
    ],
    ContentType.DYNAMIC: [
        Phase.DISCOVERED,
        Phase.SUMMARIZED,
        Phase.PUSHED,
    ],
}

# 改后:
PHASE_FLOW: dict[ContentType, list[Phase]] = {
    ContentType.VIDEO: [
        Phase.DISCOVERED,
        Phase.DOWNLOADED,
        Phase.TRANSCRIBED,
        Phase.SUMMARIZED,
        Phase.PUSHED,
    ],
    ContentType.TEXT: [
        Phase.DISCOVERED,
        Phase.DOWNLOADED,
        Phase.PUSHED,
    ],
}
```

### Step 2.4: 跑测试 — 暴露剩余 DYNAMIC 引用错误

- [ ] 运行:

```bash
uv run pytest -x tests/ 2>&1 | tail -30
```

预期失败:`platforms/bilibili/handlers.py:91` 的 `ContentType.DYNAMIC` 引用导致 `AttributeError: DYNAMIC`。pyright 也会报。这就是我们要的「红灯」。

### Step 2.5: 临时让 detector 不阻塞 — 把 DYNAMIC 改 TEXT 让测试通过

为了让后续 task 能基于「测试可跑」的状态推进,临时把 detector 里的 `ContentType.DYNAMIC` 改为 `ContentType.TEXT`(Task 4/6 会重写整个分支):

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/handlers.py` L88-96:

```python
# 改前:
                # 罕见：动态先于视频被发现（视频超出时间窗口或抓取失败）
                # 仍保留 linked_bvid 信息但不阻塞，按独立 DYNAMIC 注册
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.DYNAMIC,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )

# 改后(临时,Task 4/6 会重写):
                # 罕见：动态先于视频被发现（视频超出时间窗口或抓取失败）
                # PR-1 临时：未反查 bvid 前先按 TEXT 注册,Task 6 会重写此分支
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.TEXT,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )
```

### Step 2.6: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/ 2>&1 | tail -10
uv run pyright 2>&1 | tail -10
```

预期:测试全过,pyright 0 error(注意 pyright **不加 `.`** 参数,见 AGENTS.md)。

### Step 2.7: 提交

- [ ] 提交:

```bash
git add shared/protocols.py platforms/bilibili/handlers.py tests/test_engine.py
git commit -m "refactor(proto): 删除 ContentType.DYNAMIC, 简化 PHASE_FLOW 为 VIDEO/TEXT 两条路径

- shared/protocols.py: 删 DYNAMIC 枚举值和 PHASE_FLOW 路径
- platforms/bilibili/handlers.py: bili_dynamic_detector 临时把独立动态注册为 TEXT(Task 4/6 重写)
- tests/test_engine.py: 4 处 DYNAMIC fixture 改 TEXT, phase 注册从 SUMMARIZED 改 DOWNLOADED

Refs: issue #46, spec docs/superpowers/specs/2026-06-29-ai-summary-trigger-refactor-design.md §1"
```

---

## Task 3: DynamicInfo 暴露 has_video 字段

**目标**:在 `DynamicInfo` dataclass 加 `has_video: bool` 字段,`_parse_dynamic` 根据 type 设置(spec §2 末段 + D8)。

**依据**:bili 动态 API 返回的 type 字段中,**只有 type 8(DYNAMIC_TYPE_AV,视频投屏)和 type 1 转发原视频时**会带 linked_bvid。代码现状(`dynamic.py:95-127`):type 8 设 linked_bvid,type 1(转发)若 orig 是视频也设 linked_bvid。其余 type(4 文字 / 2 图文)linked_bvid 永远为空字符串。

**决策**:`has_video = bool(linked_bvid)` 即可,不需要按 type 数字判断。这覆盖了 type 8 和 type 1 转发视频两种情况,且与 detector 反查 bvid 逻辑天然一致(spec §2 case 1/2 都基于 linked_bvid)。

### Step 3.1: 写测试 — has_video 字段

- [ ] 创建 `/home/zyw10/proj/trawler/tests/test_bili_dynamic_parse.py`:

```python
"""Tests for platforms.bilibili.dynamic._parse_dynamic — has_video 字段。

Covers spec §2: DynamicInfo 暴露 has_video 让 detector 区分视频型/纯文字动态。
"""

from __future__ import annotations

from platforms.bilibili.dynamic import _parse_dynamic


def _make_item(dynamic_type_str: str, dynamic_id: str = "123") -> dict:
    """构造一条动态 API 原始 dict。"""
    return {
        "id_str": dynamic_id,
        "type": dynamic_type_str,
        "modules": {
            "module_author": {"name": "tester", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": "desc text",
                "major": {
                    "archive": {
                        "bvid": "BV1xx9999",
                        "title": "video title",
                    }
                },
            },
        },
    }


def test_parse_dynamic_type_av_has_video_true() -> None:
    """DYNAMIC_TYPE_AV (type 8) 是视频投屏动态,has_video 必为 True。"""
    item = _make_item("DYNAMIC_TYPE_AV")
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.has_video is True
    assert dyn.linked_bvid == "BV1xx9999"


def test_parse_dynamic_type_word_has_video_false() -> None:
    """DYNAMIC_TYPE_WORD (type 4) 是纯文字动态,has_video 必为 False。

    需要清空 major.archive(纯文字动态 API 不返回 archive 字段)。
    """
    item = _make_item("DYNAMIC_TYPE_WORD")
    item["modules"]["module_dynamic"]["major"] = {}  # 纯文字无 major.archive
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.has_video is False
    assert dyn.linked_bvid == ""


def test_parse_dynamic_type_draw_has_video_false() -> None:
    """DYNAMIC_TYPE_DRAW (type 2) 是图文动态,has_video 必为 False。"""
    item = _make_item("DYNAMIC_TYPE_DRAW")
    item["modules"]["module_dynamic"]["major"] = {
        "draw": {
            "title": "draw title",
            "items": [{"src": "https://example.com/1.jpg"}],
        }
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.has_video is False
    assert dyn.linked_bvid == ""
```

### Step 3.2: 跑测试 — 应失败(AttributeError,字段不存在)

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_dynamic_parse.py -v 2>&1 | tail -20
```

预期:`AttributeError: 'DynamicInfo' object has no attribute 'has_video'`。

### Step 3.3: 实现 — 加字段 + 设置

- [ ] 修改 `/home/zyw10/proj/trawler/shared/protocols.py` `DynamicInfo` dataclass(L46-58):

```python
# 改前:
@dataclass
class DynamicInfo:
    """B站动态信息"""

    dynamic_id: str
    title: str
    author: str
    uid: int
    pubdate: int  # Unix 时间戳
    link: str
    content: str = ""
    image_urls: list[str] = field(default_factory=list)
    linked_bvid: str = ""

# 改后:
@dataclass
class DynamicInfo:
    """B站动态信息"""

    dynamic_id: str
    title: str
    author: str
    uid: int
    pubdate: int  # Unix 时间戳
    link: str
    content: str = ""
    image_urls: list[str] = field(default_factory=list)
    linked_bvid: str = ""
    # 是否为视频型动态(type 8 视频投屏 / type 1 转发原视频)。
    # detector 用此字段决定注册为 VIDEO 还是 TEXT(spec §2)。
    # 实现:has_video = bool(linked_bvid)
    has_video: bool = False
```

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/dynamic.py` `_parse_dynamic` 返回值(L141-151):

```python
# 改前(L141-151):
    return DynamicInfo(
        dynamic_id=dynamic_id,
        title=title,
        author=author,
        uid=uid,
        pubdate=timestamp,
        link=link,
        content=content,
        image_urls=image_urls,
        linked_bvid=linked_bvid,
    )

# 改后:
    return DynamicInfo(
        dynamic_id=dynamic_id,
        title=title,
        author=author,
        uid=uid,
        pubdate=timestamp,
        link=link,
        content=content,
        image_urls=image_urls,
        linked_bvid=linked_bvid,
        has_video=bool(linked_bvid),
    )
```

### Step 3.4: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_dynamic_parse.py -v 2>&1 | tail -10
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
```

预期:新测试过,其他测试无回归,pyright 0 error。

### Step 3.5: 提交

- [ ] 提交:

```bash
git add shared/protocols.py platforms/bilibili/dynamic.py tests/test_bili_dynamic_parse.py
git commit -m "feat(proto): DynamicInfo 暴露 has_video 字段

- shared/protocols.py: DynamicInfo 加 has_video: bool = False
- platforms/bilibili/dynamic.py: _parse_dynamic 返回时设置 has_video = bool(linked_bvid)
- tests/test_bili_dynamic_parse.py: 覆盖 type AV/WORD/DRAW 三种动态

Refs: issue #46, spec §2"
```

---

## Task 4: bili_dynamic_detector 纯文字/图文动态 → 注册为 TEXT

**目标**:detector 走 spec §2 case 3 分支——`has_video=False` 的动态注册为 `bili_dyn:{id}` TEXT 消息,detector 同步调 `store.mark_body()` 写入动态正文(见 D3)。

### Step 4.1: 写测试 — detector 注册纯文字动态为 TEXT

- [ ] 创建 `/home/zyw10/proj/trawler/tests/test_bili_dynamic_detector.py`:

```python
"""Tests for bili_dynamic_detector — 纯文字/图文动态注册为 TEXT。

Covers spec §2 case 3 + plan D3: detector 注册消息时同步 mark_body 写入动态正文。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.engine import PipelineEngine
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, DynamicInfo, Phase


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.bilibili.monitor.watch_dynamic = True
    return cfg


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path)


def _make_text_dynamic(dynamic_id: str = "dyn1") -> DynamicInfo:
    """纯文字动态(has_video=False)。"""
    return DynamicInfo(
        dynamic_id=dynamic_id,
        title="文字动态标题",
        author="UP1",
        uid=100,
        pubdate=2000000000,
        link=f"https://t.bilibili.com/{dynamic_id}",
        content="这是动态正文内容",
        image_urls=[],
        linked_bvid="",
        has_video=False,
    )


@pytest.mark.asyncio
async def test_text_dynamic_registered_as_text_with_body(
    config: Config, store: MessageStore
) -> None:
    """纯文字动态:detector 注册为 bili_dyn:{id},content_type=TEXT,body 写入 dyn.content。"""
    # 准备一个订阅,fetch_new_dynamics 返回 1 条纯文字动态
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]
    dyn = _make_text_dynamic()

    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        # 触发 detector 注册
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 断言:消息已注册,类型 TEXT,phase DISCOVERED
    msg = store.get_message("bili_dyn:dyn1")
    assert msg is not None
    assert msg.content_type == ContentType.TEXT
    assert msg.phase == Phase.DISCOVERED
    assert msg.title == "文字动态标题"
    assert msg.author == "UP1"
    # 关键:body 已在 detector 阶段写入动态正文(plan D3)
    assert msg.body == "这是动态正文内容"
```

### Step 4.2: 跑测试 — 应失败(body 仍为空字符串)

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_dynamic_detector.py -v 2>&1 | tail -20
```

预期:`AssertionError: assert '' == '这是动态正文内容'`(body 字段未写)。

### Step 4.3: 实现 — detector 纯文字分支注册 TEXT + mark_body

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/handlers.py` L70-96 整个 `for dyn in dynamics` 循环:

```python
# 改前(L70-96):
    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            if dyn.linked_bvid:
                # 视频型动态：检查对应视频是否已被 bili_detector 注册
                video_msg_id = f"bili:{dyn.linked_bvid}"
                if store.is_known(video_msg_id):
                    # 视频已注册，跳过动态；如有附加文字则追加到视频消息
                    if dyn.content.strip():
                        store.append_dynamic_text(video_msg_id, dyn.content.strip())
                    logger.debug(
                        "动态 %s 与已注册视频 %s 重复，跳过注册",
                        dyn.dynamic_id,
                        dyn.linked_bvid,
                    )
                    continue
                # 罕见：动态先于视频被发现（视频超出时间窗口或抓取失败）
                # PR-1 临时：未反查 bvid 前先按 TEXT 注册,Task 6 会重写此分支
            store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.TEXT,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )

# 改后(本 task 只实现 case 3 纯文字分支,case 1/2 留 Task 6):
    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            if dyn.has_video:
                # 视频型动态：Task 6 实现去重追加 / 反查 bvid 注册 VIDEO 分支
                # 本 task 暂保留「未处理就跳过」的临时行为,Task 6 覆盖
                logger.debug("视频型动态 %s 由 Task 6 处理", dyn.dynamic_id)
                continue

            # case 3: 纯文字 / 图文动态 → 注册为 TEXT
            new_msg = store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.TEXT,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )
            if new_msg is not None and dyn.content.strip():
                # plan D3: detector 同步把动态正文写入 body,供 push 阶段渲染全文
                store.mark_body(f"bili_dyn:{dyn.dynamic_id}", dyn.content.strip())
```

注意:`add_new` 返回 `None` 表示消息已存在或超期,只有新建时才 mark_body。

### Step 4.4: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_dynamic_detector.py -v 2>&1 | tail -10
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
```

预期:新测试过,其他无回归。

### Step 4.5: 提交

- [ ] 提交:

```bash
git add platforms/bilibili/handlers.py tests/test_bili_dynamic_detector.py
git commit -m "feat(handlers): bili_dynamic_detector 纯文字动态注册为 TEXT + mark_body

纯文字 / 图文动态 (has_video=False) 注册为 bili_dyn:{id}, content_type=TEXT。
detector 同步调 store.mark_body 写入 dyn.content, 供 push 阶段渲染全文 (plan D3)。

视频型动态分支由 Task 6 实现反查 bvid 逻辑。

Refs: issue #46, spec §2 case 3"
```

---

## Task 5: bili_download handler 加 bili_dyn: 前缀特判

**目标**:修复 Oracle 审查发现的阻断 bug——detector 把纯文字动态注册为 `bili_dyn:{id}` + `ContentType.TEXT`(Task 4),TEXT flow 会推进到 `Phase.DOWNLOADED`,但现有 `bili_download` handler 用 `ctx.msg.msg_id.replace("bili:", "")` 切 bvid,对 `bili_dyn:xxx` 会得到 `dyn:xxx`,然后调 `download_video(bvid="dyn:xxx")` 必然失败,消息卡死在 DOWNLOADED。

**方案(D3 续)**:在 `bili_download` 函数开头加 `bili_dyn:` 前缀特判,no-op return True,并把 detector 已通过 `store.mark_body()` 写入的 `msg.body` 复制到 `ctx.content_text`,让 push 阶段能拿到正文。

### Step 5.1: 写测试 — bili_dyn: 前缀 TEXT 消息走 DOWNLOADED handler 不卡死

- [ ] 在 `/home/zyw10/proj/trawler/tests/test_engine.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_bili_download_handles_dynamic_text_prefix(
    config: Config, store: MessageStore
) -> None:
    """bili_dyn: 前缀的 TEXT 消息走到 DOWNLOADED 时,bili_download 应 no-op return True
    并把 msg.body 复制到 ctx.content_text,让 push 阶段能拿到正文 (plan D3)。

    背景:detector 把纯文字动态注册为 bili_dyn:{id} + TEXT,TEXT flow 含 DOWNLOADED。
    若 bili_download 不特判,会用 msg_id.replace('bili:', '') 切出错误 bvid='dyn:xxx',
    调 download_video 必然失败,消息卡死在 DOWNLOADED。
    """
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    # 重新注册真实的 bili_download(不依赖模块导入副作用)
    from platforms.bilibili.handlers import bili_download

    PipelineEngine._handlers[("bili", Phase.DOWNLOADED)] = bili_download

    msg = store.add_new(
        msg_id="bili_dyn:12345",
        platform="bili",
        content_type=ContentType.TEXT,
        pubdate=2000000000,
        title="纯文字动态",
        author="UP1",
    )
    assert msg is not None
    # 模拟 detector 阶段已写入的动态正文
    store.mark_body("bili_dyn:12345", "动态的完整正文内容")

    # 重新读出含 body 的 msg
    msg = store.get_message("bili_dyn:12345")
    assert msg is not None
    ctx = PhaseContext(msg=msg, config=config)

    result = await bili_download(ctx)

    assert result is True
    # 关键:body 被复制到 content_text,push 阶段直接读 ctx.content_text
    assert ctx.content_text == "动态的完整正文内容"
    assert ctx.error == ""
```

### Step 5.2: 跑测试 — 应失败(bili_download 切错 bvid 调 download_video 异常)

- [ ] 运行:

```bash
uv run pytest -x tests/test_engine.py::test_bili_download_handles_dynamic_text_prefix -v 2>&1 | tail -20
```

预期 FAIL:`bili_download` 用 `msg_id.replace("bili:", "")` 得到 `dyn:12345`,调 `download_video(bvid="dyn:12345", ...)` 触发异常被 try/except 捕获 → `ctx.error` 非空、`return False`。断言 `result is True` 和 `ctx.content_text == "..."` 都会失败。

### Step 5.3: 实现 — bili_download 加 bili_dyn: 前缀特判

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/handlers.py` `bili_download`(L102 起):

```python
# 改前(L102-105):
@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool:
    """下载 B站视频音频。"""
    bvid = ctx.msg.msg_id.replace("bili:", "")

# 改后(开头加特判,其余逻辑不变):
@PipelineEngine.register("bili", Phase.DOWNLOADED)
async def bili_download(ctx: PhaseContext) -> bool:
    """下载 B站视频音频。

    纯文字动态(bili_dyn: 前缀, plan D3)无媒体可下载,no-op 推进。
    detector 已通过 store.mark_body 写入的正文复制到 ctx.content_text,
    让 push 阶段能拿到动态正文。
    """
    if ctx.msg.msg_id.startswith("bili_dyn:"):
        ctx.content_text = ctx.msg.body
        return True

    bvid = ctx.msg.msg_id.replace("bili:", "")
```

只加 `if ctx.msg.msg_id.startswith("bili_dyn:"): ...` 这 3 行(含空行)和 docstring 更新,其余函数体不动。

### Step 5.4: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/test_engine.py::test_bili_download_handles_dynamic_text_prefix -v 2>&1 | tail -10
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
```

预期:新测试过,其他测试无回归(原来走 VIDEO 路径的 `bili:BV...` 消息不受影响,特判只对 `bili_dyn:` 前缀生效)。

### Step 5.5: 提交

- [ ] 提交:

```bash
git add platforms/bilibili/handlers.py tests/test_engine.py
git commit -m "fix(bili): bili_download 处理 bili_dyn: 前缀的纯文字动态

detector 把纯文字动态注册为 bili_dyn:{id} + ContentType.TEXT (Task 4),
TEXT flow 推进到 DOWNLOADED 时若 bili_download 不特判,会用 msg_id.replace
切出错误 bvid='dyn:xxx' 调 download_video,消息卡死在 DOWNLOADED。

加 bili_dyn: 前缀特判: no-op return True, 并把 msg.body 复制到 ctx.content_text
(detector 已通过 store.mark_body 写入), 让 push 阶段能拿到正文 (plan D3)。

Refs: issue #46, Oracle 审查阻断项"
```

---

## Task 6: bili_dynamic_detector 视频型动态 → 反查 bvid 注册为 VIDEO

**目标**:实现 spec §2 case 1(视频已注册→追加 dynamic_text)和 case 2(视频未注册→反查 bvid 后注册为 VIDEO)。case 2 失败时跳过入库(D4)。

**反查 API 决策(D9)**:bili 动态详情 API 在 `bilibili_api` 包中,通过 `dynamic.Dynamic.get_dynamic_info(dynamic_id)` 拿到原始 dict。但视频型动态本身在 `_parse_dynamic` 时已经从 `major.archive.bvid` 拿到了 linked_bvid!所以**正常情况下 case 2 不需要额外 API 调用**——linked_bvid 已经在 dyn 上了。

**何时需要反查?**当 linked_bvid 为空但 has_video 为 True 的边缘场景。但 Task 3 决策是 `has_video = bool(linked_bvid)`,两者同步,所以**实际不会出现 has_video=True 但 linked_bvid 为空**的情况。

**简化方案**:spec §2 case 2 的"反查 bvid"在当前 `_parse_dynamic` 实现下天然不需要——视频型动态的 linked_bvid 必非空。所以 Task 6 只需:
- case 1:`store.is_known(bili:{bvid})` → 追加 dynamic_text(原逻辑保留)
- case 2:`store.is_known` 为 False → 直接以 `bili:{bvid}` 注册为 VIDEO(spec §2 case 2 的"反查"步骤省略,bvid 已在 dyn 上)
- 反查失败场景(D4):**不会发生**(bvid 已知)

**保留 spec §2 case 2 失败语义的方法**:如果未来动态 API 改版导致 linked_bvid 缺失,`has_video` 也为 False,会走 Task 4 的 TEXT 分支。这不是 spec §2 case 2 的"反查失败",而是"动态 API 没告诉我们这是视频"——按 TEXT 处理是合理降级(至少能推动态文字)。

### Step 6.1: 写测试 — 视频型动态 case 1 / case 2

- [ ] 在 `/home/zyw10/proj/trawler/tests/test_bili_dynamic_detector.py` 末尾追加:

```python
def _make_video_dynamic(dynamic_id: str = "vdyn1", bvid: str = "BV1xx8888") -> DynamicInfo:
    """视频型动态(has_video=True, linked_bvid 非空)。"""
    return DynamicInfo(
        dynamic_id=dynamic_id,
        title="视频动态标题",
        author="UP1",
        uid=100,
        pubdate=2000000000,
        link=f"https://t.bilibili.com/{dynamic_id}",
        content="视频动态的附加说明文字",
        image_urls=[],
        linked_bvid=bvid,
        has_video=True,
    )


@pytest.mark.asyncio
async def test_video_dynamic_with_existing_video_appends_dynamic_text(
    config: Config, store: MessageStore
) -> None:
    """case 1: 视频型动态,对应 bili:{bvid} 已注册 → 追加 dynamic_text, 不新增消息。"""
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]

    # 预先注册对应视频(bili_detector 会先于 bili_dynamic_detector 执行)
    store.add_new(
        msg_id="bili:BV1xx8888",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=2000000000,
        title="视频标题",
        author="UP1",
        subscription_ref="100",
    )

    dyn = _make_video_dynamic()
    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 断言:未新增 bili_dyn: 消息
    assert store.get_message("bili_dyn:vdyn1") is None
    # 断言:已注册视频的 dynamic_text 被追加
    video_msg = store.get_message("bili:BV1xx8888")
    assert video_msg is not None
    assert video_msg.dynamic_text == "视频动态的附加说明文字"


@pytest.mark.asyncio
async def test_video_dynamic_without_existing_video_registers_as_video(
    config: Config, store: MessageStore
) -> None:
    """case 2: 视频型动态,对应 bili:{bvid} 未注册 → 以 bili:{bvid} 注册为 VIDEO。

    spec §2 case 2 描述的「反查 bvid」在当前实现下不需要——linked_bvid 已在 dyn 上。
    """
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]

    dyn = _make_video_dynamic(bvid="BV1xx7777")
    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 关键断言:以 bili:{bvid} 注册为 VIDEO(不是 bili_dyn:{dynamic_id})
    assert store.get_message("bili_dyn:vdyn1") is None
    msg = store.get_message("bili:BV1xx7777")
    assert msg is not None
    assert msg.content_type == ContentType.VIDEO
    assert msg.phase == Phase.DISCOVERED
    assert msg.author == "UP1"
    # 动态正文作为 dynamic_text 附加(plan D7)
    assert msg.dynamic_text == "视频动态的附加说明文字"


@pytest.mark.asyncio
async def test_video_dynamic_without_content_does_not_append_empty_dynamic_text(
    config: Config, store: MessageStore
) -> None:
    """case 1 边缘:视频型动态无附加文字(content 为空) → 不追加空字符串到 dynamic_text。"""
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]
    store.add_new(
        msg_id="bili:BV1xx6666",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=2000000000,
        title="V",
        author="UP1",
        subscription_ref="100",
    )

    dyn = DynamicInfo(
        dynamic_id="vdyn2",
        title="t",
        author="UP1",
        uid=100,
        pubdate=2000000000,
        link="https://t.bilibili.com/vdyn2",
        content="   ",  # 空白
        linked_bvid="BV1xx6666",
        has_video=True,
    )
    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 断言:dynamic_text 保持空(未被空白污染)
    assert store.get_message("bili:BV1xx6666").dynamic_text == ""
```

### Step 6.2: 跑测试 — 应失败(Task 4 的临时 `continue` 跳过了视频分支)

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_dynamic_detector.py -v 2>&1 | tail -30
```

预期:3 个新测试失败(case 1 没追加 dynamic_text,case 2 没注册 bili:{bvid})。

### Step 6.3: 实现 — detector 视频型分支

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/handlers.py` 把 Task 4 的临时 `continue` 替换为完整分支:

```python
# 改前(Task 4 临时):
    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            if dyn.has_video:
                # 视频型动态：Task 6 实现去重追加 / 反查 bvid 注册 VIDEO 分支
                # 本 task 暂保留「未处理就跳过」的临时行为,Task 6 覆盖
                logger.debug("视频型动态 %s 由 Task 6 处理", dyn.dynamic_id)
                continue

            # case 3: 纯文字 / 图文动态 → 注册为 TEXT
            new_msg = store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.TEXT,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )
            if new_msg is not None and dyn.content.strip():
                # plan D3: detector 同步把动态正文写入 body,供 push 阶段渲染全文
                store.mark_body(f"bili_dyn:{dyn.dynamic_id}", dyn.content.strip())

# 改后(完整实现):
    for sub in config.bilibili.subscriptions:
        dynamics = await fetch_new_dynamics(uid=sub.uid, config=config)
        for dyn in dynamics:
            if dyn.has_video:
                # 视频型动态(spec §2 case 1/2):
                # - case 1: linked_bvid 对应视频已被 bili_detector 注册 → 追加 dynamic_text
                # - case 2: 视频未注册 → 以 bili:{bvid} 注册为 VIDEO,动态正文作 dynamic_text
                video_msg_id = f"bili:{dyn.linked_bvid}"
                content_text = dyn.content.strip()
                if store.is_known(video_msg_id):
                    # case 1: 视频已注册,追加附加文字(若有)
                    if content_text:
                        store.append_dynamic_text(video_msg_id, content_text)
                    logger.debug(
                        "视频型动态 %s 与已注册视频 %s 重复,追加 dynamic_text",
                        dyn.dynamic_id,
                        dyn.linked_bvid,
                    )
                    continue

                # case 2: 视频未注册,以 bili:{bvid} 注册为 VIDEO
                # (spec §2 提到的「反查 bvid」在当前 _parse_dynamic 实现下不需要——
                #  linked_bvid 已从动态 API 的 major.archive.bvid 直接拿到)
                new_msg = store.add_new(
                    msg_id=video_msg_id,
                    platform="bili",
                    content_type=ContentType.VIDEO,
                    pubdate=dyn.pubdate,
                    title=dyn.title or f"bili:{dyn.linked_bvid}",
                    author=dyn.author,
                    subscription_ref=str(sub.uid),
                )
                if new_msg is not None and content_text:
                    # plan D7: 动态正文作为 dynamic_text 附加到 VIDEO 消息
                    store.append_dynamic_text(video_msg_id, content_text)
                continue

            # case 3: 纯文字 / 图文动态 → 注册为 TEXT
            new_msg = store.add_new(
                msg_id=f"bili_dyn:{dyn.dynamic_id}",
                platform="bili",
                content_type=ContentType.TEXT,
                pubdate=dyn.pubdate,
                title=dyn.title,
                author=dyn.author,
                subscription_ref=str(sub.uid),
            )
            if new_msg is not None and dyn.content.strip():
                # plan D3: detector 同步把动态正文写入 body,供 push 阶段渲染全文
                store.mark_body(f"bili_dyn:{dyn.dynamic_id}", dyn.content.strip())
```

### Step 6.4: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_dynamic_detector.py -v 2>&1 | tail -15
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
```

预期:全部 4 个 detector 测试过,其他无回归。

### Step 6.5: 提交

- [ ] 提交:

```bash
git add platforms/bilibili/handlers.py tests/test_bili_dynamic_detector.py
git commit -m "feat(handlers): bili_dynamic_detector 视频型动态按 case 1/2 分流

- case 1: linked_bvid 对应视频已注册 → 追加 dynamic_text (原逻辑保留)
- case 2: 视频未注册 → 以 bili:{bvid} 注册为 VIDEO, 动态正文作 dynamic_text

spec §2 提到的「反查 bvid」在当前 _parse_dynamic 实现下不需要——linked_bvid
已从动态 API 的 major.archive.bvid 直接拿到。

Refs: issue #46, spec §2 case 1/2"
```

---

## Task 7: 移除 transcribe_phase 的 content_type 特判

**目标**:删除 `transcribe_phase` 的 `if ctx.msg.content_type != ContentType.VIDEO: return True` 特判(spec §5)。PHASE_FLOW 已保证只有 VIDEO 会到 TRANSCRIBED。

### Step 7.1: 写测试 — TEXT 不会到达 TRANSCRIBED

- [ ] 在 `/home/zyw10/proj/trawler/tests/test_engine.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_text_message_never_reaches_transcribe_phase(
    config: Config, store: MessageStore
) -> None:
    """TEXT flow 不含 TRANSCRIBED: engine 不会调用 transcribe handler。

    验证 PHASE_FLOW[TEXT] 简化后,即使 transcribe_phase 移除 content_type 特判,
    TEXT 消息也走不到 TRANSCRIBED 阶段(由 PHASE_FLOW 保证,而非 handler 特判)。
    """
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        return True

    @PipelineEngine.register("*", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        pytest.fail("TRANSCRIBED handler 不应被 TEXT 消息调用")

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("bili_dyn:t1", "bili", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili_dyn:t1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED
```

### Step 7.2: 跑测试 — 应通过(特判还在时也过,因为 PHASE_FLOW 已保证)

- [ ] 运行:

```bash
uv run pytest -x tests/test_engine.py::test_text_message_never_reaches_transcribe_phase -v 2>&1 | tail -10
```

预期:**通过**(PHASE_FLOW 保证 TEXT 不进 TRANSCRIBED,与 transcribe_phase 内的特判无关)。这就是测试驱动的好处——先证明「移除特判后行为不变」。

### Step 7.3: 实现 — 删除 transcribe_phase 的 content_type 特判

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/handlers.py` `transcribe_phase`(L134-181):

```python
# 改前(L134-148):
@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。

    Bug 3 fix:
    - ``filepath`` 缺失时不再静默 return True，而是 ``ctx.error='downloaded_filepath missing'``
      并 return False，让消息停留在当前阶段并暴露在 dashboard 上，避免
      空 transcript 推送低质量通知。``process_message`` 的 rewind 网关通常
      会先一步重新下载，这里只是兜底。
    - ``transcribe_file_async`` 真异常时记 WARNING 并降级用 ``content_text``
      继续流程（return True），保持既有的优雅降级语义。
    """
    if ctx.msg.content_type != ContentType.VIDEO:
        return True

    filepath = ctx.downloaded_filepath

# 改后(删除 content_type 特判,docstring 补充说明):
@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。

    仅 VIDEO 类型消息会到达此阶段(PHASE_FLOW 保证:TEXT flow 不含 TRANSCRIBED),
    所以不需要 content_type 特判(spec §5 / issue #46 重构)。

    Bug 3 fix:
    - ``filepath`` 缺失时不再静默 return True，而是 ``ctx.error='downloaded_filepath missing'``
      并 return False，让消息停留在当前阶段并暴露在 dashboard 上，避免
      空 transcript 推送低质量通知。``process_message`` 的 rewind 网关通常
      会先一步重新下载，这里只是兜底。
    - ``transcribe_file_async`` 真异常时记 WARNING 并降级用 ``content_text``
      继续流程（return True），保持既有的优雅降级语义。
    """
    filepath = ctx.downloaded_filepath
```

只删除 `if ctx.msg.content_type != ContentType.VIDEO: return True` 这 2 行 + 1 行空行。其余函数体不动。

### Step 7.4: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
```

预期:全过。如果 `test_transcribe_phase_missing_filepath_returns_false_with_error` 失败,检查是否破坏了 VIDEO 路径(应该不会,改动只是删了一个早 return)。

### Step 7.5: 提交

- [ ] 提交:

```bash
git add platforms/bilibili/handlers.py tests/test_engine.py
git commit -m "refactor(handlers): 移除 transcribe_phase 的 content_type 特判

PHASE_FLOW 保证只有 VIDEO 类型消息到达 TRANSCRIBED 阶段, content_type 特判
是冗余的(spec §5)。删除后 TEXT 消息仍不会进 TRANSCRIBED, 由 PHASE_FLOW 保证。

Refs: issue #46, spec §5"
```

---

## Task 8: 移除 bili_push 的 is_dynamic 特判 + summarize_phase 审计

**目标**:
1. `bili_push` 用 `msg_id.startswith("bili_dyn:")` 替代 `is_dynamic = content_type == DYNAMIC`,媒体清理条件改为 `downloaded_filepath is not None`(D5/D6)。
2. `summarize_phase` 当前**没有 DYNAMIC 特判**(只有 VIDEO/xhs 分支),仅需补一个测试确认 VIDEO 类型走评论抓取。

### Step 8.1: 写测试 — bili_dyn push 仍走 t.bilibili.com URL

- [ ] 创建 `/home/zyw10/proj/trawler/tests/test_bili_push.py`:

```python
"""Tests for bili_push — URL 渲染 / 通知 type 按前缀分流。

Covers plan D5: 改造后 bili_dyn:{id} 消息(纯文字动态,TEXT 类型)仍走
t.bilibili.com/{id} URL 和 type='dynamic' 通知模板。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.engine import PipelineEngine
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NotificationContent, Phase, PhaseContext


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path)


@pytest.mark.asyncio
async def test_bili_dyn_text_push_uses_dynamic_url(
    config: Config, store: MessageStore
) -> None:
    """bili_dyn: 前缀(TEXT 类型纯文字动态) push 时 URL 用 t.bilibili.com/{id}。

    plan D5: is_dynamic 判断改为 msg_id 前缀判断,与 content_type 解耦。
    """
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [
        BiliSubscription(uid=100, name="UP1", notify_endpoints=["ep1"])
    ]

    captured_content: list[NotificationContent] = []

    async def fake_send(cfg, platform, endpoints, content):
        captured_content.append(content)
        from shared.protocols import SendResult

        return [SendResult(endpoint_name="ep1", success=True)]

    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    try:
        import platforms.bilibili.handlers  # noqa: F401

        msg = store.add_new(
            "bili_dyn:dynX", "bili", ContentType.TEXT, 2000000000, "T", "A"
        )
        assert msg is not None
        store.mark_body("bili_dyn:dynX", "动态正文")
        msg = store.get_message("bili_dyn:dynX")
        assert msg is not None

        ctx = PhaseContext(msg=msg, config=config)
        ctx.content_text = "动态正文"  # 模拟 download handler 已写入

        handler = PipelineEngine._handlers.get(("bili", Phase.PUSHED))
        assert handler is not None

        with patch("platforms.bilibili.handlers.send_to_subscription", new=fake_send):
            result = await handler(ctx)

        assert result is True
        assert len(captured_content) == 1
        c = captured_content[0]
        # 关键:URL 用 t.bilibili.com(动态 URL)
        assert c.url == "https://t.bilibili.com/dynX"
        # 关键:通知 type 仍是 'dynamic'(plan D5 保留通知模板渲染)
        assert c.type == "dynamic"
        # source_id 不含前缀
        assert c.source_id == "dynX"
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)


@pytest.mark.asyncio
async def test_bili_video_push_uses_video_url(
    config: Config, store: MessageStore
) -> None:
    """bili: 前缀(VIDEO 类型) push 时 URL 用 bilibili.com/video/{bvid}。"""
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [
        BiliSubscription(uid=100, name="UP1", notify_endpoints=["ep1"])
    ]

    captured_content: list[NotificationContent] = []

    async def fake_send(cfg, platform, endpoints, content):
        captured_content.append(content)
        from shared.protocols import SendResult

        return [SendResult(endpoint_name="ep1", success=True)]

    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    try:
        import platforms.bilibili.handlers  # noqa: F401

        msg = store.add_new(
            "bili:BV1xx1234", "bili", ContentType.VIDEO, 2000000000, "T", "A"
        )
        assert msg is not None
        msg = store.get_message("bili:BV1xx1234")
        assert msg is not None
        msg.subscription_ref = "100"

        ctx = PhaseContext(msg=msg, config=config)

        handler = PipelineEngine._handlers.get(("bili", Phase.PUSHED))
        assert handler is not None

        with patch("platforms.bilibili.handlers.send_to_subscription", new=fake_send):
            result = await handler(ctx)

        assert result is True
        assert len(captured_content) == 1
        c = captured_content[0]
        # 关键:VIDEO 用 bilibili.com/video/
        assert c.url == "https://www.bilibili.com/video/BV1xx1234"
        assert c.type == "content"
        assert c.source_id == "BV1xx1234"
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)
```

### Step 8.2: 跑测试 — 应失败(AttributeError, ContentType.DYNAMIC 已删但代码还在用)

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_push.py -v 2>&1 | tail -20
```

预期:`AttributeError: DYNAMIC`(handlers.py:256 仍引用 ContentType.DYNAMIC,虽然 Task 2 删了枚举但代码引用还在)。这就是要修复的「红灯」。

### Step 8.3: 实现 — 改造 bili_push

- [ ] 修改 `/home/zyw10/proj/trawler/platforms/bilibili/handlers.py` `bili_push`(L245-302):

```python
# 改前(L256-301):
    is_dynamic = ctx.msg.content_type == ContentType.DYNAMIC
    source_id = ctx.msg.msg_id.replace("bili_dyn:" if is_dynamic else "bili:", "")

    # 通过 subscription_ref 精确匹配订阅
    matched = None
    for sub in ctx.config.bilibili.subscriptions:
        if str(sub.uid) == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅，跳过通知", ctx.msg.subscription_ref)
        return True

    if not matched.notify_endpoints:
        logger.info("订阅 %s 未配置 endpoints，跳过通知", ctx.msg.msg_id)
        return True

    content = NotificationContent(
        platform="bili",
        source_id=source_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
        url=(f"https://t.bilibili.com/{source_id}" if is_dynamic else f"https://www.bilibili.com/video/{source_id}"),
        type="dynamic" if is_dynamic else "content",
    )

    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(
        ctx.config,
        "bili",
        matched.notify_endpoints,
        content,
    )
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))

    # 媒体清理（仅视频）
    if not is_dynamic and ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=source_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)

    return True

# 改后(用 msg_id 前缀判断替代 content_type, plan D5/D6):
    # plan D5: 用 msg_id 前缀判断动态/视频,与 content_type 解耦
    # (改造后 bili_dyn: 前缀的消息只可能是 TEXT 纯文字动态,但通知渲染仍按动态格式)
    is_dynamic = ctx.msg.msg_id.startswith("bili_dyn:")
    source_id = ctx.msg.msg_id.replace("bili_dyn:" if is_dynamic else "bili:", "")

    # 通过 subscription_ref 精确匹配订阅
    matched = None
    for sub in ctx.config.bilibili.subscriptions:
        if str(sub.uid) == ctx.msg.subscription_ref:
            matched = sub
            break
    if matched is None:
        logger.warning("未找到 subscription_ref=%s 对应的订阅，跳过通知", ctx.msg.subscription_ref)
        return True

    if not matched.notify_endpoints:
        logger.info("订阅 %s 未配置 endpoints，跳过通知", ctx.msg.msg_id)
        return True

    content = NotificationContent(
        platform="bili",
        source_id=source_id,
        title=ctx.msg.title,
        author=ctx.msg.author,
        summary=ctx.summary_text,
        keywords=ctx.keywords,
        comment_highlights=ctx.comment_highlights or "",
        url=(f"https://t.bilibili.com/{source_id}" if is_dynamic else f"https://www.bilibili.com/video/{source_id}"),
        type="dynamic" if is_dynamic else "content",
    )

    logger.info("推送 %s 到 %d 个端点...", ctx.msg.msg_id, len(matched.notify_endpoints))
    results = await send_to_subscription(
        ctx.config,
        "bili",
        matched.notify_endpoints,
        content,
    )
    ok = sum(1 for r in results if r.success)
    logger.info("通知推送完成 (%d/%d)", ok, len(results))

    # plan D6: 媒体清理条件改为 downloaded_filepath is not None
    # (改造后 TEXT 类型无视频文件,filepath 始终 None,条件自然成立)
    if ctx.config.transcribe.delete_after_transcribe and ctx.downloaded_filepath is not None:
        try:
            cleanup_media(filepath=ctx.downloaded_filepath, source_id=source_id)
        except Exception as exc:
            logger.warning("媒体清理失败 %s: %s", ctx.msg.msg_id, exc)

    return True
```

### Step 8.4: 跑测试 — 全过

- [ ] 运行:

```bash
uv run pytest -x tests/test_bili_push.py -v 2>&1 | tail -10
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
```

预期:全过。注意 `ContentType` 在 handlers.py L21 的 import 仍保留(其他地方还在用,如 detector、bili_download 等)。

### Step 8.5: 提交

- [ ] 提交:

```bash
git add platforms/bilibili/handlers.py tests/test_bili_push.py
git commit -m "refactor(handlers): bili_push 用 msg_id 前缀替代 content_type 判断

- is_dynamic 改为 msg_id.startswith('bili_dyn:'), 与 content_type 解耦 (plan D5)
- 媒体清理条件从 'not is_dynamic' 改为 'downloaded_filepath is not None' (plan D6)
- TEXT 类型(bili_dyn:) 仍走 t.bilibili.com URL 和 type='dynamic' 通知模板

Refs: issue #46, spec §5"
```

---

## Task 9: 审计 dashboard/formatter/web 残留 DYNAMIC 引用

**目标**:确认除 `_DYNAMIC_TYPE_MAP` 等动态 API 字段外,无其他 `ContentType.DYNAMIC` 残留。

### Step 9.1: grep 残留引用

- [ ] 运行:

```bash
rg -n "ContentType\.DYNAMIC|content_type == ContentType|is_dynamic" --type py -g '!docs/**' -g '!**/plans/**' -g '!**/specs/**'
```

预期结果分析:
- ✅ `_DYNAMIC_TYPE_MAP`(dynamic.py L17)— bili 动态 API 的 type 字段名,**保留**
- ✅ `DYNAMIC_TYPE_AV/WORD/DRAW/FORWARD`(dynamic.py L18-21, L64-65)— API 常量,**保留**
- ✅ `dynamic_type` 局部变量(dynamic.py L80)— 解析逻辑用,**保留**
- ✅ `is_dynamic`(handlers.py bili_push 内)— Task 8 改造后用前缀判断,**保留**(语义已变)
- ✅ `core/engine.py:133` `content_type == ContentType.VIDEO` — Bug-3 rewind 逻辑,**保留**(与 DYNAMIC 无关)
- ✅ `core/engine.py:331-334` `content_type.name` 在错误日志里 — 普通使用,**保留**
- ❌ 任何剩余 `ContentType.DYNAMIC` 引用 — **必须修复**

### Step 9.2: 修复任何残留(条件性)

- [ ] 如果 Step 8.1 发现任何 `ContentType.DYNAMIC` 残留(本 plan 写作时已确认无),按以下模板修复:

```bash
# 如果有 fixture 残留 DYNAMIC:
rg -n "ContentType\.DYNAMIC" tests/
# 改为 TEXT 或 VIDEO, 根据测试语义:
# - 测 phase 推进 → TEXT (flow 不含 TRANSCRIBED/SUMMARIZED)
# - 测视频路径 → VIDEO
```

- [ ] 如果 web/dashboard 有渲染逻辑按 `content_type.name == 'DYNAMIC'` 分流:

```bash
rg -n "DYNAMIC" web/
```

本 plan 写作时已确认 web/ 无 DYNAMIC 引用,本步骤应为 no-op。

### Step 9.3: 全套验证

- [ ] 运行:

```bash
uv run pytest -x tests/ 2>&1 | tail -5
uv run pyright 2>&1 | tail -5
uv run ruff check . 2>&1 | tail -5
```

预期:全过。

### Step 9.4: 提交(条件性)

- [ ] **如果有改动**才提交:

```bash
git add -A
git commit -m "chore: 清理 PR-1 重构后的 DYNAMIC 残留引用

Refs: issue #46"
```

如果 Step 8.2 是 no-op,**跳过此提交**。

---

## Task 10: 最终验证 + 数据重置文档

**目标**:跑完整验证三件套,在 spec 关联文件中标注数据重置需求(D2)。

### Step 10.1: 跑完整验证

- [ ] 运行(ruff + pyright + pytest 三件套):

```bash
uv run ruff check . 2>&1 | tail -3
uv run pyright 2>&1 | tail -3
uv run pytest -x -q tests/ 2>&1 | tail -5
```

预期:
- `ruff`: All checks passed
- `pyright`: 0 errors, 0 warnings
- `pytest`: 全部通过,数量应 ≥ Task 1 基线 + 新增的 detector/push/bili_download 测试(约 7 个新测试)

### Step 10.2: 手动 smoke 验证(可选,本地有 bili 配置时)

- [ ] 如果本地有 bili 凭证和订阅,跑一次实际检查:

```bash
uv run trawler check --platform bili
```

观察日志:
- 一条新视频应该走 `DOWNLOADED → TRANSCRIBED → SUMMARIZED → PUSHED`
- 一条纯文字动态应该注册为 `bili_dyn:{id}` 走 `DOWNLOADED → PUSHED`(body 已在 detector 写入)
- 视频对应的动态应该追加 dynamic_text,不重复推送

如果本地无凭证,跳过此步骤,单元测试已覆盖。

### Step 10.3: PR description 标注数据重置

- [ ] 在创建 PR 时(由主 agent 操作,本 plan 不自动创建),description 中必须包含:

```markdown
## ⚠️ 数据重置要求

本次重构删除了 `ContentType.DYNAMIC`,旧的 `data/messages.json` 中含 DYNAMIC 类型(枚举值 3)的消息会反序列化失败。

**升级前必须删除 `data/messages.json`**(或备份后清空)。升级后第一次 cron 会重新拉取所有订阅源,无进度保留。

详见 spec §7: `docs/superpowers/specs/2026-06-29-ai-summary-trigger-refactor-design.md`
```

### Step 10.4: 最终 commit(如果有 ruff format 调整)

- [ ] 跑 ruff format 自动格式化:

```bash
uv run ruff format . 2>&1 | tail -3
git status
```

- [ ] 如果有格式改动,提交:

```bash
git add -A
git commit -m "style: ruff format 自动格式化"
```

如果无改动,跳过。

---

## Self-Review

写完后自查清单:

- [x] **spec §1**(ContentType 简化 + PHASE_FLOW 2 路径)→ Task 2 覆盖
- [x] **spec §2**(bili_dynamic_detector 改造 3 个 case)→ Task 4 (case 3) + Task 6 (case 1/2) 覆盖
- [x] **spec §2 末段**(DynamicInfo has_video)→ Task 3 覆盖
- [x] **spec §5**(transcribe_phase / summarize_phase 移除特判)→ Task 7 (transcribe) + Task 8 (push,含 summarize 审计)覆盖
- [x] **spec §7**(数据重置)→ Task 10 Step 10.3 标注
- [x] **spec §8 PR-1 范围**:ContentType/DYNAMIC + detector + dynamic.py + transcribe/summarize + 测试 fixture → Task 2/3/4/5/6/7/8 全覆盖
- [x] **PR-1 不在范围**:weibo / xhs 改动 → 本 plan 不涉及(Task 8 只审计不改动)
- [x] **无 placeholder**:每个 step 都有完整代码块
- [x] **类型一致**:`has_video` 字段在 Task 3 定义,Task 4/6 测试和实现都用同名同类型
- [x] **commit 规范**:`feat(proto)` / `feat(handlers)` / `fix(bili)` / `refactor(handlers)` / `refactor(proto)` / `chore` / `style`,符合项目惯例
- [x] **TDD 顺序**:每个 task 都是 Write test → Run fail → Implement → Run pass → Commit
- [x] **无跨任务依赖混乱**:Task N 实现不依赖 Task N+2
- [x] **Oracle 阻断项已修**(Task 5):bili_download 加 bili_dyn: 前缀特判,防止纯文字动态 TEXT 消息卡死在 DOWNLOADED

## 预估执行时间

| Task | 估时 |
|---|---|
| Task 1 探索基线 | 5 min |
| Task 2 删 DYNAMIC + 改 fixture | 15 min |
| Task 3 has_video 字段 | 10 min |
| Task 4 纯文字动态 → TEXT | 15 min |
| Task 5 bili_download 特判 bili_dyn: 前缀(Oracle 阻断项) | 10 min |
| Task 6 视频动态 → VIDEO(case 1/2) | 20 min |
| Task 7 移除 transcribe 特判 | 10 min |
| Task 8 bili_push 改造 | 15 min |
| Task 9 审计残留 | 5 min |
| Task 10 最终验证 | 10 min |
| **合计** | **~115 min** |

可由 subagent-driven-development 流程 task-by-task 执行,每个 task 是独立的 commit unit。
