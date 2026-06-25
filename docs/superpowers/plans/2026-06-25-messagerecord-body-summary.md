# Implementation Plan — MessageRecord 扩展 body/summary + dashboard hover 展示

**日期**: 2026-06-25
**前置 plan**: `docs/superpowers/plans/2026-06-25-frontend-build-and-info-display.md` §6
**规模**: 中等（4 层：protocols / store / handlers / templates）
**TDD 节奏**: 是

---

## 1. 背景与现状

用户原始诉求：dashboard 最近消息表格的 hover 卡片应显示「完整正文 + AI 摘要」。但当前 `MessageRecord` schema 不持有这两类数据，hover 卡只能复述 title/author/phase，无价值。

### 当前数据流（关键观察）

```
detector → add_new()        # 只存 title/author/pubdate 等
   │
   ▼
download handler:
   ├─ bili:   不写正文（视频没有"正文"，只有 transcript）
   ├─ xhs:    ctx.content_text = result.content_text  ← 在 ctx，未落盘
   └─ weibo:  ctx.content_text = result.text          ← 在 ctx，未落盘
   │
   ▼
transcribe handler:  ctx.transcript_text              ← 在 ctx，未落盘
   │
   ▼
summarize handler:   ctx.summary_text                 ← 在 ctx，未落盘
   │
   ▼
push handler:        NotificationContent(summary=ctx.summary_text) → 推送即丢弃
```

**核心问题**：`ctx.content_text` / `transcript_text` / `summary_text` 都只活在单次 `process_message` 调用的 `PhaseContext` 内存中，从未写回 `MessageStore`。dashboard 读 store 拿不到这些数据。

---

## 2. 调研发现（每条引用 file:line）

### F1. MessageRecord 现有字段
`shared/protocols.py:276-295` — 当前 11 个字段，已有先例的"动态附加文字"字段 `dynamic_text: str = ""`（line 293），是新字段的最佳模板（同样有默认值、同样经 store 持久化、同样参与摘要输入）。

### F2. PhaseContext 已有 content_text / transcript_text / summary_text
`shared/protocols.py:304-310` — 流水线上下文已经积累这三类文本，但 `MessageRecord` 没有对应字段，所以它们在 `process_message` 结束后被 GC。

### F3. MessageStore 反序列化是显式字段映射
`shared/message_store.py:73-88` — `_msg_from_dict` 用 `data.get("xxx", default)` 逐字段构造 `MessageRecord`。**这意味着旧 `messages.json`（无新字段）反序列化时自动得默认空值，向后兼容零成本**。但**必须**在此处显式加新字段的 `.get()`，否则新字段不会被反序列化（即使 MessageRecord 有默认值）。

### F4. MessageStore 写入有现成模式：mark_phase / mark_error / append_dynamic_text
`shared/message_store.py:208-240` — 三个写入方法都是同一套模式：
- `if msg_id not in self._messages: return`
- `self._messages[msg_id]["xxx"] = value`
- `self._messages[msg_id]["updated_at"] = time.time()`
- `self._dirty = True`

新增 `mark_body` / `mark_summary` 是纯模式复制。

### F5. 三平台 download handler 的"body 可得性"差异巨大
- **bili 视频** `platforms/bilibili/handlers.py:102-125`：视频没有文本正文，只下载音频文件。`content_text` 在此阶段为空，要等 transcribe → `transcript_text`。
- **bili 动态** `platforms/bilibili/handlers.py:53-96` + `dynamic.py`：纯文字/图文动态有正文，但当前已经走 `append_dynamic_text` 路径附加到关联视频上，不独立处理。
- **xhs** `platforms/xiaohongshu/handlers.py:83,90`：`ctx.content_text = result.content_text`，正文直接来自 `note.desc`（`downloader.py:96`），是现成的清洗后纯文本。
- **weibo** `platforms/weibo/handlers.py:95,102`：`ctx.content_text = result.text`，正文来自长文 API + parser，已是纯文本。

### F6. summarize_phase 是跨平台共用 handler（关键复用点）
`platforms/bilibili/handlers.py:181-230` — `@PipelineEngine.register("*", Phase.SUMMARIZED)` 注册，bili 视频 + xhs 视频都走这里。**但 xhs 图文（TEXT）和 weibo（TEXT）跳过 SUMMARIZED 阶段**（在 download handler 内联生成摘要，见 weibo `handlers.py:108-120`）。

这意味着 summary 的写回点**至少有两处**：
- 跨平台 `summarize_phase`（bili 视频 / xhs 视频）
- 各 download handler 的内联摘要分支（weibo `handlers.py:116`，xhs TEXT 没生成摘要只生成评论）

### F7. dashboard hover macro 现状
`web/templates/dashboard.html:7-28` — `stat_tooltip` macro 当前只渲染 title + platform + author 三行，**每条消息一个紧凑条目**（`max-h-72 overflow-y-auto`），不是单条消息的详情卡。

注意 §6 假设的"`msg_detail_card` macro"实际不存在；现成的是 `stat_tooltip`。用户诉求"hover 显示完整正文+摘要"是针对**最近消息表格的 `<tr>` hover**（`dashboard.html:100-109`），当前 `<tr>` 完全没有 hover 卡。

### F8. dashboard 路由已经传 recent_messages
`web/routes/dashboard.py:62,82` — `recent = sorted(...)[:20]`，已注入模板上下文为 `recent_messages`。无需改路由，只需扩展模板。

### F9. body 长度风险：transcript 可能极长
`core/transcriber.py` 产出的 transcript_text 对长视频可达万字级。如果 bili 视频把整个 transcript 当 body 存入 `messages.json`，单文件会迅速膨胀（24h 窗口假设 50 条视频 × 1万字 = 50万字 JSON）。

### F10. 现有 `dynamic_text` 的 mark 接口是 "append" 不是 "set"
`shared/message_store.py:224-240` — `append_dynamic_text` 用换行拼接多次写入。body/summary 应该用 **set（覆盖）** 语义，因为 download/summarize 各只跑一次，没有追加需求。

---

## 3. 关键决策

| ID | 决策 | 选项对比 | 选定 + 理由 |
|----|------|----------|-------------|
| D1 | body 的语义边界 | (a) 原始 HTML (b) 清洗后纯文本 (c) transcript 全文 | **(b) 清洗后纯文本**。xhs/weibo 的 `ctx.content_text` 已是纯文本可直接用；bili 视频无文本正文，body 留空（transcript 太长另算，见 D2）。HTML 会引入 XSS 与渲染复杂度。 |
| D2 | bilibili 视频是否存 transcript 当 body | (a) 全量存 (b) 截断前 N 字 (c) 不存 | **(c) 不存**。transcript 动辄万字（F9），全量存会让 `messages.json` 膨胀失控；截断版又有"伪正文"误导。bili 视频的 body 字段保持空字符串，hover 卡对 bili 视频只显示 summary（D3）。 |
| D3 | body 长度上限 | (a) 无限制 (b) 5000 字 (c) 1000 字 | **(b) 5000 字硬截断**。xhs/weibo 长文极少超 5000 字；超长内容截断后 hover 卡也读不完，加 "…" 省略号。截断在 handler 写回前做，store 层不做限制（保持 store 是无业务逻辑的薄层）。 |
| D4 | summary 写回点 | (a) 只在跨平台 summarize_phase (b) 在每个生成 summary_text 的 handler | **选 (b) 的变体：不注入 store，改由 engine 集中 flush，见 D5**。 |
| D5 | 写回机制：handler 主动 vs engine 集中 | (a) handler 拿 store 引用主动写 (b) engine 在 phase 间统一捞 ctx 落盘 | **(b) engine 集中**。`PhaseContext` 当前不含 store（F2），若让 handler 写需要给所有 handler 注入 store，改动面大且打破 ctx 纯净性。改为在 `engine.process_message` 每次 `mark_phase` 成功后，新增一步 `_flush_ctx_to_store(msg_id, ctx, store)`：把 ctx.content_text → body（仅 DOWNLOADED 阶段后），ctx.summary_text → summary（仅 SUMMARIZED 阶段后）。**集中、可测、不污染 handler 签名**。 |
| D6 | bili 动态的 body | (a) 走 dynamic_text 现有路径 (b) 也写 body | **(a) 不动**。`dynamic_text` 是去重场景的特殊语义（追加到关联视频），与"独立消息的正文"不是一回事。bili 纯文字动态当前注册为 DYNAMIC 类型，但 detector 没回填其 `dyn.content` 到任何 ctx 字段——这是**已知缺口**，本 plan 不修，列为后续清理（§8）。 |
| D7 | hover 卡片视觉 | (a) 复用 stat_tooltip 紧凑列表 (b) 表格 `<tr>` 单独行内 hover 详情卡 | **(b)**。用户诉求是"最近消息表格的 hover 卡"，stat_tooltip 是统计卡片的列表，二者不同。本 plan 给 `<tr>` 加独立 hover 详情卡（绝对定位、`group-hover` 显示），展示 body + summary。视觉精修留给后续 @designer，本 plan 给 MVP 可读版本。 |
| D8 | 数据迁移 | (a) 写迁移脚本 (b) 不迁移 | **(b) 不迁移**。F3 已论证：旧条目反序列化时新字段默认空，hover 卡对旧消息只显示"（无正文）"——可接受。下次该消息重新跑流水线时自动填充。 |

---

## 4. 文件清单

### 修改（7 个）
| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `shared/protocols.py` | 加字段 | MessageRecord 加 `body: str = ""` 和 `summary: str = ""`（line 295 后） |
| `shared/message_store.py` | 加反序列化 + 加 2 个方法 | `_msg_from_dict` 加两行 `.get()`；新增 `mark_body` / `mark_summary` |
| `core/engine.py` | 加 flush 函数 | `process_message` 内 phase 推进成功后调用 `_flush_ctx_to_store` |
| `platforms/xiaohongshu/handlers.py` | 截断 | `ctx.content_text` 在写回前截断 5000 字（实际截断在 engine flush 做，handler 不改） |
| `platforms/weibo/handlers.py` | 无改动 | 同上，flush 在 engine |
| `web/templates/dashboard.html` | 加 `<tr>` hover 卡 | 任务 3 详述 |
| `tests/test_message_store.py` | 加测试 | mark_body / mark_summary / 反序列化新字段 |

### 新增（0 个）
无需新文件。所有改动都在既有文件加方法/字段/macro。

---

## 5. 任务分解（TDD）

### 任务 1: 扩展 MessageRecord schema（protocols）
**测试先写** → `tests/test_message_store.py` 末尾加：
```python
def test_record_has_body_and_summary_defaults() -> None:
    from shared.protocols import MessageRecord, ContentType, Phase
    r = MessageRecord(
        msg_id="x", platform="bili", content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED, pubdate=0, title="t", author="a",
    )
    assert r.body == ""
    assert r.summary == ""
```

**实现**：
- `shared/protocols.py` MessageRecord 末尾（line 295 `subscription_ref` 后）加：
```python
    body: str = ""        # 内容正文（xhs/weibo 纯文本；bili 视频留空，见 plan D2）
    summary: str = ""     # AI 摘要（summarize 阶段或 download 内联摘要回写）
```

**验证**：`uv run pytest tests/test_message_store.py::test_record_has_body_and_summary_defaults -x`

---

### 任务 2: MessageStore 反序列化 + 写入接口
**测试先写**（4 个）：
```python
def test_msg_from_dict_loads_body_and_summary(store: MessageStore) -> None:
    # 手动塞带新字段的 dict，验证反序列化
    store._messages["bili:BV1"] = {
        "platform": "bili", "content_type": "video", "phase": "discovered",
        "pubdate": int(time.time()), "title": "T", "author": "A",
        "body": "正文", "summary": "摘要",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.body == "正文" and msg.summary == "摘要"

def test_msg_from_dict_defaults_body_summary_when_missing(store: MessageStore) -> None:
    # 旧格式兼容：dict 不含新字段
    store._messages["bili:BV1"] = {
        "platform": "bili", "content_type": "video", "phase": "discovered",
        "pubdate": int(time.time()), "title": "T", "author": "A",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.body == "" and msg.summary == ""

def test_mark_body_sets_body(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_body("bili:BV1", "新正文")
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.body == "新正文"

def test_mark_summary_sets_summary(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_summary("bili:BV1", "AI 摘要")
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.summary == "AI 摘要"
```

**实现** `shared/message_store.py`：
1. `_msg_from_dict`（line 75-88）末尾追加：
```python
            body=data.get("body", ""),
            summary=data.get("summary", ""),
```
2. 在 `append_dynamic_text`（line 240）后加两个方法（复制 `mark_phase` 模式）：
```python
    def mark_body(self, msg_id: str, body: str) -> None:
        """更新消息的正文（download 阶段回写）。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["body"] = body
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def mark_summary(self, msg_id: str, summary: str) -> None:
        """更新消息的 AI 摘要（summarize 阶段回写）。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["summary"] = summary
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True
```

**验证**：`uv run pytest tests/test_message_store.py -x`

---

### 任务 3: engine.process_message 集中 flush ctx → store
**测试先写** `tests/test_engine.py` 加：
```python
async def test_process_message_flushes_body_after_download(...) -> None:
    # 注册一个 DOWNLOADED handler 设置 ctx.content_text
    # 跑 process_message 从 DISCOVERED → DOWNLOADED
    # 断言 store.get_message().body == ctx.content_text 的值

async def test_process_message_flushes_summary_after_summarized(...) -> None:
    # 注册 SUMMARIZED handler 设置 ctx.summary_text
    # 断言 store.get_message().summary == ...

async def test_process_message_flushes_inline_summary_after_downloaded(...) -> None:
    # 覆盖 weibo 内联摘要路径（F6/R2）：DOWNLOADED 阶段 handler 内直接设置 ctx.summary_text
    # 注册 DOWNLOADED handler，在 handler 内 `ctx.summary_text = "内联摘要"`（模拟 weibo 行为）
    # 不注册 SUMMARIZED handler，流程不走 SUMMARIZED 阶段
    # 跑 process_message 从 DISCOVERED → DOWNLOADED
    # 断言 store.get_message().summary == "内联摘要"
```

**实现** `core/engine.py`：
1. 模块顶部加常量：
```python
_BODY_MAX_CHARS = 5000  # 见 plan D3
```
2. 加私有辅助函数（双 if 设计，覆盖 weibo 内联摘要，见 F6/R2）：
```python
def _flush_ctx_to_store(msg_id: str, ctx: PhaseContext, store: MessageStore, just_completed: Phase) -> None:
    """阶段推进成功后，把 ctx 上对应阶段的产出回写到 store。

    - DOWNLOADED 完成：ctx.content_text → body（截断到 _BODY_MAX_CHARS）
    - DOWNLOADED 或 SUMMARIZED 完成：ctx.summary_text → summary
      （weibo 在 download handler 内联生成摘要，所以 DOWNLOADED 也捞 summary）
    """
    if just_completed == Phase.DOWNLOADED and ctx.content_text:
        body = ctx.content_text[:_BODY_MAX_CHARS]
        if len(ctx.content_text) > _BODY_MAX_CHARS:
            body += "…"
        store.mark_body(msg_id, body)
    if ctx.summary_text and just_completed in (Phase.DOWNLOADED, Phase.SUMMARIZED):
        store.mark_summary(msg_id, ctx.summary_text)
```
这样 weibo 在 DOWNLOADED 阶段就把内联摘要落盘，bili/xhs 视频在 SUMMARIZED 阶段落盘。
3. 在 `process_message`（line 142-145）的 `store.mark_phase` 之后、`logger.info` 之前插入 `_flush_ctx_to_store`，`store.save()` 放最后：
```python
            msg.phase = next_phase
            store.mark_phase(msg.msg_id, next_phase)
            _flush_ctx_to_store(msg.msg_id, ctx, store, next_phase)  # 新增：mark_phase 之后、logger.info 之前
            logger.info("%s:%s → %s ✓", msg.platform, msg.msg_id, next_phase.name)
            store.save()
```

**验证**：`uv run pytest tests/test_engine.py -x` + `uv run pytest tests/test_pipeline_e2e.py -x`

---

### 任务 4: dashboard `<tr>` hover 卡（MVP 视觉）
**说明**：本任务无单元测试（Jinja2 模板渲染测试成本高），靠 manual verify + 现有 `test_web_dashboard.py` 不破。

**实现** `web/templates/dashboard.html`：

1. 在文件顶部 macro 区（line 28 后）加新 macro：
```jinja
{# Hover detail card for a single message row #}
{% macro msg_hover_card(msg) %}
<div class="absolute left-0 top-full hidden group-hover:block z-50 w-96 pt-2">
  <div class="bg-[var(--card-bg)] backdrop-blur-[12px] border border-[var(--card-border)] rounded-[10px] shadow-tooltip p-3 max-h-96 overflow-y-auto">
    <div class="text-sm font-semibold text-[var(--text-primary)] mb-1">{{ msg.title }}</div>
    <div class="text-xs text-[var(--text-secondary)] mb-2 flex items-center gap-1.5">
      <span class="font-mono">{{ msg.platform }}</span>
      <span>·</span>
      <span>{{ msg.author }}</span>
      <span>·</span>
      <span>{{ msg.phase.name }}</span>
    </div>
    {% if msg.summary %}
    <div class="mb-2">
      <div class="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)] mb-0.5">AI 摘要</div>
      <div class="text-xs text-[var(--text-primary)] leading-relaxed whitespace-pre-wrap">{{ msg.summary }}</div>
    </div>
    {% endif %}
    {% if msg.body %}
    <div>
      <div class="text-[10px] uppercase tracking-wider text-[var(--text-tertiary)] mb-0.5">正文</div>
      <div class="text-xs text-[var(--text-secondary)] leading-relaxed whitespace-pre-wrap">{{ msg.body }}</div>
    </div>
    {% else %}
    <div class="text-xs text-[var(--text-tertiary)] italic">（此消息无正文，如视频仅含转写）</div>
    {% endif %}
  </div>
</div>
{% endmacro %}
```

2. 改 `<tr>`（line 101）加 `relative group` 类：
```html
<tr class="relative group border-t border-gray-100 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/30 transition-colors">
```

3. 在 `<tr>` 内最后一个 `<td>` 后（line 107 后、`</tr>` 前）加一个空 `<td>` 包裹 hover 卡（避免 `<tr>` 直接子级是 div 引起 HTML 校验告警）：
```html
<td class="px-5 py-3">{{ badge(msg.phase.name, msg.phase | phase_color) }}</td>
<td class="p-0 m-0 relative">
  {{ msg_hover_card(msg) }}
</td>
```

**注意 R1 风险**：`<tr>` 设 `position: relative` 后，`group-hover` 卡片的绝对定位锚点是 `<tr>` 还是 `<td>` 需实测。前置 plan R6 已记录此不确定性，本 plan 实施时浏览器实测调整（可能要把 `relative` 移到包裹 `<td>` 上）。

**验证**：
- `uv run pytest tests/test_web_dashboard.py -x`（确保路由不破）
- 启动 web server，手动 hover 一行 xhs/weibo 消息，确认卡出、内容渲染
- hover bili 视频消息，确认 body 显示"（此消息无正文…）"，summary 正常显示

---

### 任务 5: 端到端验证
1. `uv run ruff check .`
2. `uv run pyright .`
3. `uv run pytest -x`
4. 在本地有真实 `data/messages.json` 的环境跑 `uv run trawler check --platform xhs`，确认新消息落盘后 `messages.json` 出现 `"body"` 和 `"summary"` 字段
5. 浏览器开 dashboard，hover 验证

---

## 6. 验证步骤汇总

```bash
# 单元测试
uv run pytest tests/test_message_store.py -x
uv run pytest tests/test_engine.py -x
uv run pytest tests/test_pipeline_e2e.py -x
uv run pytest tests/test_web_dashboard.py -x

# 全套
uv run pytest -x

# 静态检查
uv run ruff check .
uv run pyright .

# 集成验证（需真实配置）
uv run trawler check --platform xhs
# 检查 data/messages.json 是否含 body/summary 字段
```

---

## 7. 风险与不确定项

| ID | 风险 | 影响 | 缓解 |
|----|------|------|------|
| R1 | `<tr>` 设 `relative` 后 hover 卡定位锚点不确定 | 任务 4 视觉错位 | 浏览器实测，必要时把 `relative group` 移到包裹 `<td>` |
| R2 | weibo 在 download handler 内联生成 summary，D5 集中 flush 改为 DOWNLOADED 也捞 summary 才能覆盖 | 任务 3 漏 weibo | 已在任务 3 修正方案中处理（去掉 elif，DOWNLOADED + SUMMARIZED 都捞 summary） |
| R3 | 旧 `messages.json` 反序列化兼容 | 无（F3 论证） | 无需处理 |
| R4 | body 字段让 `messages.json` 膨胀（即使 5000 字上限） | 长期存储增长 | 24h cleanup 已存在；50 条 × 5KB = 250KB，可接受。如未来仍紧张再加压缩或在 cleanup 时主动剔 body |
| R5 | xhs 图文（TEXT）类型当前不生成 summary（只生成 comment_highlights） | xhs 图文 hover 卡 summary 为空 | 接受现状，hover 卡对 xhs TEXT 只显示 body + 评论（评论字段不在本 plan 范围） |
| R6 | bili 动态（DYNAMIC 类型）当前 detector 未回填 `dyn.content` 到 body | bili 动态消息 body 为空 | 列入 §8 后续清理 |
| R7 | dashboard hover 卡视觉是 MVP，颜色/间距/动效未精修 | 用户视觉体验欠佳 | 本 plan 显式声明视觉精修留给后续 @designer 任务 |

---

## 8. 后续可选清理（不在本 plan 范围）

1. **bili 动态 body 回填**：DYNAMIC 类型 detector 拿到 `dyn.content` 后，要么独立处理（不依赖 linked_bvid 去重路径），要么新增 `mark_body` 调用。涉及 `platforms/bilibili/dynamic.py` + `handlers.py:88-96`。
2. **xhs TEXT 类型摘要**：xhs 图文当前跳过 SUMMARIZED 阶段（在 download handler 内联只取评论亮点）。若用户希望 xhs 图文也有 AI 摘要，需调整 PHASE_FLOW 或在 download handler 内联调 summarize。
3. **hover 卡视觉精修**：交给 @designer，统一与 stat_tooltip 视觉语言、加 fade-in 动效、暗色模式调优。
4. **dashboard 分页**：当前 `recent = sorted(...)[:20]`，body/summary 落盘后单条消息变大，但 20 条不会拖累首屏。若未来 messages.json 膨胀到影响 dashboard 路由查询性能，再加分页/虚拟滚动。
5. **body/summary 的全文搜索**：dashboard 加搜索框需要倒排或 LIKE 查询，本 plan 不做。

---

## 9. 任务依赖图

```
任务 1 (protocols 加字段)
   │
   ▼
任务 2 (store 加反序列化 + mark_body/mark_summary)
   │
   ▼
任务 3 (engine flush)  ←── 依赖任务 1+2
   │
   ▼
任务 4 (dashboard hover 卡)  ←── 独立，可与任务 3 并行
   │
   ▼
任务 5 (端到端验证)  ←── 依赖全部
```

任务 3 和任务 4 无相互依赖，可并行开发（不同文件，无 merge 冲突）。

---

## 调研发现的"惊喜"（plan §6 未预料的）

1. **`summarize_phase` 是 `@register("*", ...)` 跨平台 handler，但 weibo 完全不走它** —— weibo 在 download handler 内联生成 summary（`handlers.py:108-120`）。plan §6 假设"summarizer 阶段回写 summary"对 weibo 不成立。本 plan D5/R2 已处理。

2. **`<tr>` hover 卡 ≠ stat_tooltip** —— plan §6 第 4 点说"扩展 `msg_detail_card` macro"，实际该 macro 不存在；现成的 `stat_tooltip` 是列表型，与表格行 hover 不是一回事。本 plan 任务 4 新建独立 macro。

3. **xhs 图文（TEXT）类型不生成 summary** —— 当前 xhs TEXT 在 download handler 内只取评论亮点，从不调 summarize。本 plan 不修（§8 第 2 条），但 hover 卡对 xhs 图文的 summary 段会为空。

4. **PhaseContext 不持有 store 引用** —— 若按 plan §6 第 3 点"handlers 回写"需要给所有 handler 注入 store，改动面远大于集中 flush 方案。本 plan D5 改为 engine 集中 flush，handler 完全不改。

5. **bili 视频没有"正文"** —— plan §6 假设所有平台 download 都能拿到正文，但 bili 视频只有 transcript（且太长不适合当 body，D2）。本 plan 显式让 bili 视频 body 留空，hover 卡显示占位文案。
