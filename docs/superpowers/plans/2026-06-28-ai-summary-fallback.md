# Implementation Plan — AI 摘要 Fallback 链 + 失败卡阶段

**日期**: 2026-06-28
**规模**: 大（4 层：config / summarizer / engine / handlers；含数据模型与 UI 兼容）
**TDD 节奏**: 是（每个 task 先写测试）

---

## 1. 背景

服务器现状：LLM API 余额耗尽返回 401，`core/summarizer.py:analyze_content` 吞掉异常并返回空 `AnalysisResult(source="none")`，`summarize_phase` 永远返回 `True`，消息推进到 `PUSHED`，历史消息永久无 summary。

两个强耦合需求：
1. **需求 1（Fallback 链）**：配置支持多个 LLM provider，按序尝试，前一个失败（含 401/超时/网络错/5xx）才 fallback 到下一个。
2. **需求 2（失败卡阶段）**：fallback 链全部失败时让消息卡在 `SUMMARIZED` 不进 `PUSHED`，等待下一轮 cron 重试；同时区分「真正失败」和「合理跳过」（如 `text=''` 走 `source="empty"` 不应卡住）。

---

## 2. 调研发现（每条引用 file:line）

### F1. AI 调用点共 4 处（必须全部纳入 fallback 体系）

| # | 位置 | 调用入口 | 说明 |
|---|------|----------|------|
| A1 | `core/summarizer.py:254` | `analyze_content()` 内部 `create_provider(config.analysis)` | 唯一真正调用 LLM 的地方，其余入口都委托这里 |
| A2 | `platforms/bilibili/handlers.py:216` | 跨平台 `summarize_phase`（`@register("*", Phase.SUMMARIZED)`） | bili VIDEO/DYNAMIC + xhs VIDEO 走此路径 |
| A3 | `platforms/weibo/handlers.py:109,123` | weibo download handler 内联 `generate_summary` + `extract_keywords` | TEXT 类型不走 SUMMARIZED 阶段；**双 AI 请求**（一次出 summary，又一次出 keywords）— 已知遗留 |
| A4 | `web/routes/settings.py:75` | `_probe_provider` → `create_provider` | 用户「测试连通性」按钮；不应走 fallback 链（用户明确指定了 provider） |

**结论**：fallback 链只需在 A1 (`analyze_content`) 内部实现，A2/A3/A4 调用方完全不需要改（继续委托 `analyze_content`）。A3 的「双 AI 请求」问题不在本 plan 范围（见 §8）。

### F2. `OpenAIProvider.generate` 的异常类型不可区分失败原因

`core/summarizer.py:177-194`：所有失败路径都包成 `RuntimeError(msg)`，但 msg 含原始信息（如 `"API 返回错误 (401): ..."`、`"OpenAI API 调用超时 (60s)"`、`"无法连接到 API: ..."`）。

**影响**：要按错误类型决定是否 fallback（如 4xx 永久失败不 fallback，5xx/timeout 临时失败 fallback）需要解析字符串。本 plan 选择**简化策略**：所有失败都 fallback（与原始需求一致：「前一个失败含 401/超时/网络错/5xx 等」）。永久失败的 provider 反复重试是已知代价，靠 §3 D6 的 retry_count 上限兜底。

### F3. 当前 summarize_phase 吞掉失败永远返回 True

`platforms/bilibili/handlers.py:215-230`：
```python
try:
    analysis = await analyze_content(...)
    ctx.summary_text = analysis.summary
    ctx.keywords = analysis.keywords
except Exception as exc:
    logger.error("✗ 摘要/关键词生成失败: %s", exc)
    logger.exception("Analysis failed for %s", source_id)
return True   # ← 即使失败也返回 True
```

**注意**：`analyze_content` 内部已经吞了所有异常返回 `AnalysisResult(source="none")`（`summarizer.py:262-265`），所以 line 225 的 `except` 几乎不会触发；line 230 的 `return True` 是默认路径。**改造重点**：让 `analyze_content` 在 fallback 链全失败时返回可识别的失败信号（不是空 `AnalysisResult`，因为空正文 `text=''` 也产生空 `AnalysisResult`）。

### F4. engine 的失败处理：mark_error + break，且 cron 不重试已 error 消息

`core/engine.py:157-161`：
```python
success = await handler(ctx)
if not success:
    store.mark_error(msg.msg_id, ctx.error)
    store.save()
    break
```

`core/engine.py:226-234`（`run_platform`）：
```python
pending = list(store.get_messages(phase=Phase.PUSHED, exclude=True, platform=platform))
for msg in pending:
    if msg.error:
        # 跳过已有错误的消息，避免永久失败的消息无限重试
        logger.info("⏭ 跳过错误消息: %s (%s)", msg.title, msg.error)
        continue
    await cls.process_message(msg, config, store)
```

**关键发现（与用户原始假设矛盾）**：
- summarize_phase 返回 False → engine `mark_error` → 下次 cron **直接跳过**（line 230-232）
- 这意味着「卡在 SUMMARIZED 让下次 cron 自动重试」**不能简单返回 False**，否则消息永久死锁

**真正的重试机制需要**：
- 失败时不写 `error` 字段（或写专用字段 `last_error` 但不触发 cron 跳过）
- 引入 `retry_count` 字段，连续失败 N 次后才 `mark_error` 让 cron 永久跳过
- N 次成功后 `retry_count` 重置

### F5. PHASE_FLOW 确认卡在 SUMMARIZED 后下次 cron 会重新捞到

`shared/protocols.py:255-273`：VIDEO 和 DYNAMIC 的 flow 都包含 `SUMMARIZED → PUSHED`。

`store.get_messages(phase=Phase.PUSHED, exclude=True)` 含义：**所有 phase 不是 PUSHED 的消息**（包括 DISCOVERED/DOWNLOADED/TRANSCRIBED/SUMMARIZED）。所以卡在 SUMMARIZED 的消息下次 cron 确实会被 `pending` 列表捞到 — 但前提是 `msg.error` 为空（F4）。

### F6. TEXT 类型（weibo / xhs 图文）跳过 SUMMARIZED 阶段

`shared/protocols.py:263-267`：TEXT flow 是 `[DISCOVERED, DOWNLOADED, PUSHED]`。

- weibo 在 download handler 内联生成 summary（A3）— 失败时当前 fallback 到 `ctx.content_text[:500]`（`weibo/handlers.py:120`），不返回 False
- 改造：weibo download handler 也要按 fallback 链全部失败的语义返回 False，卡在 DOWNLOADED（不是 SUMMARIZED）

### F7. AnalysisConfig 现状 + 4 个 surface 需要兼容

`shared/config.py:117-125`：
```python
@dataclass
class AnalysisConfig:
    enabled: bool = True
    provider: str = "openai"
    api_base: str = ""
    api_key: str = ""
    model_name: str = ""
```

4 个受影响的 surface：
- **S1** `shared/config.py:268-269` `_parse_config`：`_dict_to_dataclass(AnalysisConfig, ana)`
- **S2** `shared/config.py:321-324` `_apply_env_overrides`：`TRAWLER_LLM_API_KEY` / `TRAWLER_LLM_API_BASE`
- **S3** `web/routes/settings.py:65-93` `_probe_provider` + `/settings/analysis/test` + `/settings/analysis/save`
- **S4** `web/templates/settings.html:79-119` AI 分析表单（含 provider select / api_base / api_key / model_name）

### F8. `messages.json` 反序列化是显式字段映射

`shared/message_store.py:73-90`：`_msg_from_dict` 用 `data.get("xxx", default)` 逐字段构造 `MessageRecord`。新增字段必须：
1. `MessageRecord` dataclass 加默认值（`shared/protocols.py:277-300`）
2. `_msg_from_dict` 加 `xxx=data.get("xxx", default)`
3. `add_new`（line 194-205）考虑是否需要初始化新字段

向后兼容：旧 `messages.json` 反序列化时新字段得默认值，零迁移成本。

### F9. 现有测试套件骨架

- `tests/test_summarizer.py`：`TestCreateProvider` / `TestParseMarkdownAnalysis` / `TestAnalyzeContent` / `TestLegacyWrappersReturnEmptyOnFailure`（共 13 测试）
- `tests/test_engine.py`：`test_process_message_handler_failure_stops_flow` 是失败语义的模板
- `tests/test_message_store.py`：mark_* 方法的测试模板
- `tests/test_web_settings_analysis.py`：settings 路由测试模板
- `tests/test_config.py`：config 解析测试（**重要**：要看是否覆盖 `_parse_config`）

### F10. `LLMProvider` Protocol 现状

`shared/protocols.py:224-228`：
```python
@runtime_checkable
class LLMProvider(Protocol):
    async def generate(self, prompt: str) -> str: ...
```

fallback 链 provider 实现此 Protocol 即可（鸭子类型，不需要继承）。

---

## 3. 关键决策

| ID | 决策 | 选项对比 | 选定 + 理由 |
|----|------|----------|-------------|
| D1 | 多 provider 配置的 dataclass 结构 | (a) `providers: list[ProviderConfig]` (b) dict (c) 旧字段保留 + 新增 `extra_providers: list[...]` | **(c) 旧字段保留 + 新增 `extra_providers`**。理由：(1) 单 provider 是 99% 用户场景，避免强迫用户改 config；(2) S3/S4 UI 表单几乎不改（只追加可选「备用 provider」段）；(3) env override（S2）保持单 key 不破；(4) 旧 `messages.json` / `config.toml` 完全向上兼容（**Issue 4 细化**：`providers_chain` 在主 provider `api_base=''` 时跳过主，让残留默认值场景仍返回空链，与旧版 source="none" 行为对齐，避免退化卡 SUMMARIZED）。代价：AnalysisConfig 有两组字段（旧的 4 个 + 新的 list），略冗余，但 dataclass 层面可加 `@property providers_chain` 方法整合。 |
| D2 | `ProviderConfig` dataclass 是否独立定义 | (a) 复用 AnalysisConfig 嵌套 (b) 新建独立 `LLMProviderConfig` | **(b) 新建 `LLMProviderConfig`**。`AnalysisConfig.enabled` 是全局开关，不应出现在每个 provider 上；嵌套结构混乱。新 dataclass 只含 `name / provider / api_base / api_key / model_name`。 |
| D3 | fallback 链 provider 的实现位置 | (a) 在 `core/summarizer.py` 内新建 `FallbackChainProvider` 类 (b) 改 `create_provider` 函数返回链 (c) 改 `analyze_content` 内联循环 | **(a) 新建 `FallbackChainProvider`** 实现 `LLMProvider` Protocol。理由：(1) 单元测试易（mock 单个 provider 注入链）；(2) `analyze_content` 不需要循环逻辑（保持单 `provider.generate(prompt)` 调用形态）；(3) `create_provider` 函数语义保持「按 config 返回单个 provider」，但返回的是「链 provider」对调用方透明。 |
| D4 | `analyze_content` 如何传递「fallback 全失败」信号 | (a) 抛异常 (b) 在 `AnalysisResult` 加 `failed: bool` 字段 (c) 改返回类型为 `AnalysisResult | None` | **(b) `AnalysisResult` 加 `failed: bool = False`**。理由：(1) 不破坏现有签名；(2) 区分「合理空正文（source='empty', failed=False）」vs「真失败（source='none', failed=True）」；(3) 旧调用方（generate_summary / extract_keywords）即使不读 `failed` 也兼容。 |
| D5 | `summarize_phase` 返回 False 的条件 | (a) 只要 `analysis.failed` 就 False (b) 综合考虑评论获取失败也卡 (c) 加 retry_count 阈值 | **(a) 仅 `analysis.failed` 触发 False；retry_count 阈值在 engine 层处理（见 D7）**。理由：保持 handler 单一职责（只判断本次成败），重试次数策略由 engine 统一控制。**关键**：评论获取失败仍然 return True（保持当前行为，避免评论 API 抖动卡死所有消息）。 |
| D6 | weibo download handler 内联摘要失败是否也卡 | (a) 卡（与 SUMMARIZED 一致） (b) 不卡（保持现状 fallback 到 `content_text[:500]`） | **(a) 卡**。需求 2 的「fallback 链全失败」语义必须跨阶段统一。weibo download handler 改造：`generate_summary` 失败时返回 False（卡在 DOWNLOADED），由 engine 层 retry_count 接管。**注意**：weibo 当前是双调用（summary + keywords），改造后 keywords 失败不影响 summary 成功（保持现状）。 |
| D7 | 重试次数上限机制 | (a) `MessageRecord.retry_count: int` + engine 检查 (b) 时间窗口（每小时最多重试 N 次） (c) 不加上限，依赖 24h cleanup 自然淘汰 | **(a) `MessageRecord.retry_count: int = 0` + `last_error: str = ""`**。engine 流程：handler 返回 False 时 `retry_count += 1`，写入 `last_error`（**不是 `error`**），如果 `retry_count >= MAX` 才 `mark_error` 让 cron 跳过。MAX = 5（可调常量）。理由：(1) F4 已确认写 `error` 会立即跳过；(2) `last_error` 与 `error` 分离让 cron 区分「可重试」vs「永久失败」；(3) 24h cleanup 太晚（消息在 24h 内可能重试几百次刷日志）。 |
| D8 | retry_count 在何时重置 | (a) handler 成功时重置为 0 (b) 永不重置（一旦失败过就累积） (c) phase 推进时重置 | **(a) handler 成功时重置为 0**。理由：失败的 provider 临时抖动恢复后应该重置，否则消息会因累积失败次数被永久跳过。在 engine `process_message` 推进 phase 成功的代码块（line 163-167）里加 `store.mark_retry_reset(msg_id)`。 |
| D9 | MAX_RETRY 的默认值 | (a) 3 (b) 5 (c) 10 (d) 配置化 | **(b) 5**，硬编码常量 `MAX_SUMMARY_RETRIES = 5` 放在 `shared/constants.py`。理由：(1) cron 默认 3-10 分钟一次，5 次约覆盖 15-50 分钟，足以跨越临时故障窗口；(2) 配置化过度（用户不会调）；(3) 太小（3）可能误杀临时抖动；(4) 太大（10）让坏消息刷太久日志。 |
| D10 | settings UI 是否改造支持多 provider 编辑 | (a) 完整改造成动态 list 编辑器 (b) MVP 只支持主 provider + 一个备用 provider 输入框 (c) 不改 UI（用户改 config.toml） | **(c) 本 plan 不改 UI**。理由：(1) UI 改造是独立大任务（动态表单 + JS 难度高）；(2) 多 provider fallback 是高级用户场景，高级用户能改 toml；(3) `_probe_provider` 的「测试连通性」按钮仍然工作（用户单独测试每个 provider 时改主 provider 即可）。**列为 §8 后续清理**。settings.html 的副标题（line 87-89）需要更新，因为「AI 失败时自动降级到本地 TF 摘要」是历史描述，当前已无 TF fallback。 |
| D11 | 环境变量覆盖（S2）的兼容性 | (a) 旧 env key 仍然只覆盖主 provider (b) 引入 `TRAWLER_LLM_FALLBACK_*` 新 key | **(a) 旧 env key 仍然只覆盖主 provider**。理由：(1) env override 主要给 CI/Docker 用，单 provider 场景；(2) 多 provider fallback 的密钥不应该走 env（多 key 难以表达）；(3) 用户需要 fallback 时改 config.toml。 |

---

## 4. 文件清单

### 修改（8 个）
| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `shared/config.py` | 加 dataclass + 兼容解析 | 新增 `LLMProviderConfig`；`AnalysisConfig` 加 `extra_providers: list[LLMProviderConfig]`；`_parse_config` 把 list of dict 转 `LLMProviderConfig`；保留旧字段语义 |
| `shared/protocols.py` | 加字段 | `MessageRecord` 加 `retry_count: int = 0` 和 `last_error: str = ""`；`AnalysisResult` 加 `failed: bool = False` |
| `shared/message_store.py` | 加反序列化 + 2 个写入方法 | `_msg_from_dict` 加两行 `.get()`；新增 `mark_retry_failure(msg_id, error)` 和 `mark_retry_reset(msg_id)` |
| `shared/constants.py` | 加常量 | `MAX_SUMMARY_RETRIES = 5` |
| `core/summarizer.py` | 加 `FallbackChainProvider` + 改 `analyze_content` | 新增 `FallbackChainProvider` 类（实现 `LLMProvider`）；`create_provider` 改为按 config 构建链；`analyze_content` 在失败时设 `result.failed = True` 而不是吞掉 |
| `core/engine.py` | 改 `process_message` 失败分支 | handler 返回 False 时根据 ctx 决定走 retry 还是 mark_error；phase 推进成功时 reset retry |
| `platforms/bilibili/handlers.py` | 改 `summarize_phase` 返回值 | 按 `analysis.failed` 返回 True/False；评论失败仍 True |
| `platforms/weibo/handlers.py` | 改 download handler 内联摘要 | summary 失败时 `ctx.error = ...; return False`；keywords 失败仍继续 |

### 配套修改（3 个，可选项 / 测试 / 文档）
| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `web/templates/settings.html` | 改副标题文案 | 删除「AI 失败时自动降级到本地 TF 摘要」历史描述；改为说明 fallback 链机制（仅文档，UI 表单不改） |
| `tests/test_summarizer.py` | 加测试 | `FallbackChainProvider` 单测 + `analyze_content` 的 `failed=True` 路径 |
| `tests/test_engine.py` | 加测试 | retry_count 累积 / 达上限 mark_error / 成功 reset |
| `tests/test_message_store.py` | 加测试 | `mark_retry_failure` / `mark_retry_reset` / 反序列化新字段 |
| `tests/test_config.py` | 加测试 | 多 provider 配置解析 + 旧配置兼容 |
| `config/config.toml.example` | 加注释示例 | `[analysis]` 段加 `[[analysis.extra_providers]]` 示例（注释掉） |

### 新增（0 个）
无需新文件。

---

## 5. 任务分解（TDD）

### 任务 1：扩展数据模型（protocols + constants）

**测试先写** `tests/test_message_store.py`（追加）：
```python
def test_record_has_retry_and_last_error_defaults() -> None:
    from shared.protocols import MessageRecord, ContentType, Phase
    r = MessageRecord(
        msg_id="x", platform="bili", content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED, pubdate=0, title="t", author="a",
    )
    assert r.retry_count == 0
    assert r.last_error == ""
```

`tests/test_summarizer.py`（追加在 `TestParseMarkdownAnalysis` 后）：
```python
def test_analysis_result_has_failed_default_false() -> None:
    from core.summarizer import AnalysisResult
    r = AnalysisResult()
    assert r.failed is False

def test_phase_context_has_permanent_error_default_false() -> None:
    """Issue 6: PhaseContext.permanent_error 默认 False（保持现有 retry 行为）。"""
    from shared.protocols import PhaseContext, MessageRecord, Config
    ctx = PhaseContext(msg=MessageRecord(
        msg_id="x", platform="bili", content_type="video",
        phase="discovered", pubdate=0, title="t", author="a",
    ), config=Config())
    assert ctx.permanent_error is False
```

**实现**：

1. `shared/constants.py` 末尾追加：
```python
# AI 摘要重试上限（连续失败 N 次后 mark_error 让 cron 永久跳过）
MAX_SUMMARY_RETRIES = 5
```

2. `shared/protocols.py` 的 `AnalysisResult`（line 70-79）末尾加：
```python
    failed: bool = False  # True 表示 fallback 链全部失败（与 source="empty" 区分）
```

3. `shared/protocols.py` 的 `MessageRecord`（line 277-300）末尾追加（在 `summary` 字段后）：
```python
    # 摘要失败重试计数（engine 层使用，handler 不直接读写）
    retry_count: int = 0
    # 最近一次可重试失败的错误信息（与 error 字段区分：error 表示永久失败，cron 跳过）
    last_error: str = ""
```

4. `shared/protocols.py` 的 `PhaseContext`（line 304-316）末尾追加（在 `error` 字段后）：
```python
    # handler 标记本次失败为「永久失败」：engine 跳过 retry 直接 mark_error（cron 永久跳过）。
    # 用于 fail-fast 场景：transcribe 文件路径缺失、download access_limited 等重试无意义的失败。
    # 默认 False（保持现有 retry 行为）。handler 在 return False 前置 True 即可。
    permanent_error: bool = False
```

**说明**：`permanent_error` 是 PhaseContext 的运行时字段，**不持久化**到 `MessageRecord` /
`messages.json`。每次 `process_message` 创建新 ctx 时默认 False，handler 仅在本次失败需要
跳过 retry 时设 True。engine 失败分支优先检查此字段。见 Issue 6 / R10。

**验证**：`uv run pytest tests/test_message_store.py::test_record_has_retry_and_last_error_defaults tests/test_summarizer.py::test_analysis_result_has_failed_default_false tests/test_summarizer.py::test_phase_context_has_permanent_error_default_false -x`

---

### 任务 2：MessageStore 加反序列化 + retry 写入方法

**测试先写** `tests/test_message_store.py`（追加）：
```python
def test_msg_from_dict_loads_retry_and_last_error(store: MessageStore) -> None:
    store._messages["bili:BV1"] = {
        "platform": "bili", "content_type": "video", "phase": "discovered",
        "pubdate": int(time.time()), "title": "T", "author": "A",
        "retry_count": 3, "last_error": "API timeout",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.retry_count == 3 and msg.last_error == "API timeout"

def test_msg_from_dict_defaults_retry_when_missing(store: MessageStore) -> None:
    # 旧 messages.json 兼容
    store._messages["bili:BV1"] = {
        "platform": "bili", "content_type": "video", "phase": "discovered",
        "pubdate": int(time.time()), "title": "T", "author": "A",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.retry_count == 0 and msg.last_error == ""

def test_mark_retry_failure_increments_count(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_retry_failure("bili:BV1", "first fail")
    store.mark_retry_failure("bili:BV1", "second fail")
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.retry_count == 2 and msg.last_error == "second fail"
    assert msg.error == ""  # 关键：不写 error，cron 不跳过

def test_mark_retry_reset_clears_count(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_retry_failure("bili:BV1", "fail")
    store.mark_retry_reset("bili:BV1")
    msg = store.get_message("bili:BV1")
    assert msg is not None and msg.retry_count == 0 and msg.last_error == ""
```

**实现** `shared/message_store.py`：

1. `_msg_from_dict`（line 75-90）末尾追加（在 `summary=data.get("summary", "")` 后）：
```python
            retry_count=data.get("retry_count", 0),
            last_error=data.get("last_error", ""),
```

2. 在 `mark_summary` 方法后（line 262）加两个方法：
```python
    def mark_retry_failure(self, msg_id: str, error: str) -> None:
        """记录一次可重试失败：retry_count += 1，写 last_error（不写 error）。

        与 ``mark_error`` 的区别：
        - ``mark_error`` 写 ``error`` 字段 → cron ``run_platform`` 跳过此消息（永久失败语义）
        - ``mark_retry_failure`` 写 ``last_error`` 字段 → cron 仍会重试此消息

        engine 层根据 ``retry_count`` 是否达到 ``MAX_SUMMARY_RETRIES`` 决定调哪个。
        """
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["retry_count"] = self._messages[msg_id].get("retry_count", 0) + 1
        self._messages[msg_id]["last_error"] = error
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def mark_retry_reset(self, msg_id: str) -> None:
        """handler 成功后重置 retry_count 和 last_error。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["retry_count"] = 0
        self._messages[msg_id]["last_error"] = ""
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True
```

3. `reset_to_phase`（line 264-279）也要同步重置 retry_count（用户手动 reset 时清状态）：
```python
            if current_phase >= target.value:
                data["phase"] = target.value
                data["error"] = ""
                data["retry_count"] = 0      # 新增
                data["last_error"] = ""      # 新增
                data["updated_at"] = time.time()
                self._dirty = True
```

**验证**：`uv run pytest tests/test_message_store.py -x`

---

### 任务 3：扩展 AnalysisConfig 支持多 provider

**测试先写** `tests/test_config.py`（追加）：
```python
def test_parse_config_legacy_single_provider_still_works() -> None:
    """旧 config.toml（无 extra_providers）必须 100% 向上兼容。"""
    from shared.config import _parse_config, AnalysisConfig
    raw = {"analysis": {"enabled": True, "provider": "openai", "api_base": "https://x", "api_key": "k"}}
    cfg = _parse_config(raw)
    assert cfg.analysis.enabled is True
    assert cfg.analysis.provider == "openai"
    assert cfg.analysis.api_base == "https://x"
    assert cfg.analysis.api_key == "k"
    assert cfg.analysis.extra_providers == []

def test_parse_config_extra_providers_list_parsed() -> None:
    from shared.config import _parse_config, LLMProviderConfig
    raw = {
        "analysis": {
            "provider": "openai",
            "api_base": "https://primary", "api_key": "k1", "model_name": "gpt-4o-mini",
            "extra_providers": [
                {"provider": "openai", "api_base": "https://secondary", "api_key": "k2", "model_name": "gpt-4o"},
                {"provider": "ollama", "api_base": "http://local:11434/v1", "model_name": "qwen2.5:7b"},
            ],
        }
    }
    cfg = _parse_config(raw)
    assert len(cfg.analysis.extra_providers) == 2
    assert isinstance(cfg.analysis.extra_providers[0], LLMProviderConfig)
    assert cfg.analysis.extra_providers[0].api_base == "https://secondary"
    assert cfg.analysis.extra_providers[1].provider == "ollama"

def test_analysis_providers_chain_property_returns_main_plus_extras() -> None:
    """AnalysisConfig.providers_chain 是统一访问入口（主 + 备用 list）。"""
    from shared.config import AnalysisConfig, LLMProviderConfig
    cfg = AnalysisConfig(provider="openai", api_base="https://x", api_key="k")
    cfg.extra_providers = [LLMProviderConfig(provider="ollama", api_base="http://l:11434/v1")]
    chain = cfg.providers_chain
    assert len(chain) == 2
    assert chain[0].api_base == "https://x"     # 主 provider 在前
    assert chain[1].provider == "ollama"

def test_analysis_providers_chain_empty_when_main_unconfigured() -> None:
    """主 provider 未配置 api_base + provider 默认值时返回空链（disabled 语义）。

    覆盖两类「未配置」：
    - enabled=False → 空（disabled）
    - enabled=True 但主 provider api_base='' → 空（残留默认值场景，避免退化）
    见 Issue 4：旧配置 enabled=true 且 api_base/api_key 都为空时，
    旧版本直接 source="none" 不卡，新版本 providers_chain 跳过主 provider 让 create_provider
    抛 ValueError → analyze_content except 后 failed=True 会卡——必须从链里跳过让「未配置」
    与「禁用」语义对齐。
    """
    from shared.config import AnalysisConfig
    cfg = AnalysisConfig(enabled=False)
    assert cfg.providers_chain == []
    # enabled=True 但主 provider 未填 api_base（残留默认值场景）
    cfg2 = AnalysisConfig(enabled=True)  # api_base 默认 ""
    assert cfg2.providers_chain == []

def test_analysis_providers_chain_skips_unconfigured_main_but_keeps_extras() -> None:
    """主 provider api_base 为空但 extra_providers 配了 → 链里跳过主，只用 extras。

    这是 Issue 4 退化修复的关键场景：用户可能配置「主 provider 占位（api_base 空）
    + extra_providers 实际 provider」。链里应跳过占位主 provider，避免退化。
    """
    from shared.config import AnalysisConfig, LLMProviderConfig
    cfg = AnalysisConfig(enabled=True)  # 主 provider api_base 默认空
    cfg.extra_providers = [
        LLMProviderConfig(provider="ollama", api_base="http://l:11434/v1"),
        LLMProviderConfig(provider="openai", api_base="https://x", api_key="k"),
    ]
    chain = cfg.providers_chain
    # 主 provider 因 api_base='' 被跳过，只剩两个 extra
    assert len(chain) == 2
    assert chain[0].provider == "ollama"
    assert chain[1].api_base == "https://x"
```

**实现** `shared/config.py`：

1. 在 `AnalysisConfig` 前（line 114 之前）新增 dataclass：
```python
@dataclass
class LLMProviderConfig:
    """单个 LLM provider 配置（fallback 链的一节）。

    与 ``AnalysisConfig`` 的关系：
    - ``AnalysisConfig`` 顶层有 ``enabled`` 全局开关 + 旧的 4 个字段（作为「主 provider」）
    - ``AnalysisConfig.extra_providers`` 是 ``list[LLMProviderConfig]``（备用链，按序 fallback）
    """
    name: str = ""           # 可选标识符（仅日志用，无 name 时用 provider+api_base）
    provider: str = "openai"
    api_base: str = ""
    api_key: str = ""
    model_name: str = ""
```

2. `AnalysisConfig` 加新字段 + `providers_chain` property（line 117-125 替换为）：
```python
@dataclass
class AnalysisConfig:
    """AI 分析配置。

    支持两种配置方式（向上兼容）：
    1. **单 provider（旧）**：直接填 ``provider`` / ``api_base`` / ``api_key`` / ``model_name``
    2. **多 provider fallback（新）**：填上面的主 provider + ``extra_providers`` 列表。
       ``providers_chain`` property 返回 [主 provider, *extra_providers] 作为 fallback 链。

    ``enabled=False`` 时 ``providers_chain`` 返回空列表。
    """

    enabled: bool = True
    provider: str = "openai"
    api_base: str = ""
    api_key: str = ""
    model_name: str = ""
    # fallback 链（按序尝试，前一个失败才用下一个）
    extra_providers: list[LLMProviderConfig] = field(default_factory=list)

    @property
    def providers_chain(self) -> list[LLMProviderConfig]:
        """统一访问入口：返回 fallback 链。

        ``enabled=False`` 时返回空链（disabled 语义）。

        **Issue 4 退化修复**：``enabled=True`` 但主 provider ``api_base`` 为空时，
        主 provider 被跳过（视为「未配置」，与 disabled 语义对齐）。避免旧配置残留
        默认值场景（enabled=true 且 api_base/api_key 都为空）退化成卡 SUMMARIZED：
        旧版本直接 source="none" 不卡，新版本若主 provider 进入链会让 create_provider
        抛 ValueError → analyze_content except 后 failed=True 卡住。跳过后：
        - 若 extras 非空 → 链用 extras
        - 若 extras 也空 → 空链 → create_provider 抛 ValueError → analyze_content
          返回 failed=True。但此时 enabled=True 且配置全空本就异常，应通过 disabled
          语义处理，本 plan 视为「用户配置错误」由 §8 第 7 条增强。
        """
        if not self.enabled:
            return []
        chain: list[LLMProviderConfig] = []
        # 主 provider：api_base 非空才纳入链（避免残留默认值退化）
        if self.api_base:
            chain.append(LLMProviderConfig(
                provider=self.provider, api_base=self.api_base,
                api_key=self.api_key, model_name=self.model_name,
            ))
        chain.extend(self.extra_providers)
        return chain
    ```

3. `_parse_config` 的 analysis 段（line 268-269）改为：
```python
    if ana := raw.get("analysis"):
        # 单独处理 extra_providers（list of dict → list[LLMProviderConfig]）
        extras_raw = ana.get("extra_providers", [])
        extras = [
            _dict_to_dataclass(LLMProviderConfig, ep) if isinstance(ep, dict) else ep
            for ep in extras_raw
        ]
        ana_no_extras = {k: v for k, v in ana.items() if k != "extra_providers"}
        cfg.analysis = _dict_to_dataclass(AnalysisConfig, ana_no_extras)
        cfg.analysis.extra_providers = extras
```

**验证**：`uv run pytest tests/test_config.py -x`

---

### 任务 4：FallbackChainProvider + 改造 create_provider

**测试先写** `tests/test_summarizer.py`（新增 `TestFallbackChainProvider` 类）：
```python
class TestFallbackChainProvider:
    """Tests for FallbackChainProvider — 按序尝试，前一个失败才 fallback。"""

    @pytest.mark.asyncio
    async def test_first_provider_success_no_fallback(self) -> None:
        from core.summarizer import FallbackChainProvider
        from shared.config import LLMProviderConfig

        primary = AsyncMock()
        primary.generate = AsyncMock(return_value="primary response")
        secondary = AsyncMock()
        secondary.generate = AsyncMock(return_value="secondary response")

        chain = FallbackChainProvider(providers=[primary, secondary])
        result = await chain.generate("ping")
        assert result == "primary response"
        primary.generate.assert_awaited_once()
        secondary.generate.assert_not_awaited()  # 关键：第一个成功就不 fallback

    @pytest.mark.asyncio
    async def test_first_fail_second_success(self) -> None:
        from core.summarizer import FallbackChainProvider

        primary = AsyncMock()
        primary.generate = AsyncMock(side_effect=RuntimeError("401 unauthorized"))
        primary.__class__.__name__ = "MockPrimary"  # for logging
        secondary = AsyncMock()
        secondary.generate = AsyncMock(return_value="secondary response")

        chain = FallbackChainProvider(providers=[primary, secondary])
        result = await chain.generate("ping")
        assert result == "secondary response"
        primary.generate.assert_awaited_once()
        secondary.generate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_fail_raises_runtime_error(self, caplog: pytest.LogCaptureFixture) -> None:
        from core.summarizer import FallbackChainProvider

        p1 = AsyncMock()
        p1.generate = AsyncMock(side_effect=RuntimeError("401"))
        p2 = AsyncMock()
        p2.generate = AsyncMock(side_effect=RuntimeError("timeout"))
        p3 = AsyncMock()
        p3.generate = AsyncMock(side_effect=RuntimeError("connect refused"))

        chain = FallbackChainProvider(providers=[p1, p2, p3])
        with caplog.at_level("WARNING", logger="core.summarizer"):
            with pytest.raises(RuntimeError, match="所有 provider 失败"):
                await chain.generate("ping")
        # 每个 provider 的失败都要被记录。
        # 注意：r.message 是格式化前的模板字符串（如 "⚠️  %s"），
        # 格式化后的最终文本在 record.getMessage() 里。断言必须读 getMessage()。
        fail_logs = [
            r for r in caplog.records
            if "provider #" in r.getMessage() or "provider 失败" in r.getMessage()
        ]
        assert len(fail_logs) >= 3

    @pytest.mark.asyncio
    async def test_empty_chain_raises(self) -> None:
        from core.summarizer import FallbackChainProvider
        chain = FallbackChainProvider(providers=[])
        with pytest.raises(RuntimeError, match="无可用 provider"):
            await chain.generate("ping")


class TestCreateProviderChain:
    """Tests for create_provider — 现在返回 FallbackChainProvider。"""

    def test_create_provider_single_returns_chain_with_one(self) -> None:
        from core.summarizer import FallbackChainProvider, create_provider
        config = AnalysisConfig(provider="openai", api_base="https://x", api_key="k")
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)
        assert len(chain._providers) == 1

    def test_create_provider_multiple_returns_chain_with_all(self) -> None:
        from core.summarizer import FallbackChainProvider, create_provider
        from shared.config import LLMProviderConfig
        config = AnalysisConfig(provider="openai", api_base="https://x", api_key="k")
        config.extra_providers = [
            LLMProviderConfig(provider="ollama", api_base="http://l:11434/v1"),
        ]
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)
        assert len(chain._providers) == 2
```

**实现** `core/summarizer.py`：

1. 在 `OpenAIProvider` 类后（line 195 之后）新增 `FallbackChainProvider`：
```python
class FallbackChainProvider:
    """按序尝试多个 provider，前一个失败（异常）才 fallback 到下一个。

    实现 ``LLMProvider`` Protocol（鸭子类型，无需显式继承）。

    设计要点：
    - 所有失败类型（401 / 超时 / 网络错 / 5xx / parse 错）都触发 fallback。
      不区分「永久失败」vs「临时失败」（见 plan F2），永久失败的 provider
      反复重试由 ``MessageRecord.retry_count`` 上限兜底。
    - 每个 provider 失败时记 WARNING（运维可见），所有失败后抛 RuntimeError
      让上层 ``analyze_content`` 标记 ``failed=True``。
    - 空 providers 列表抛 ``RuntimeError("无可用 provider")``。
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("providers 列表不能为空")
        self._providers = providers

    async def generate(self, prompt: str) -> str:
        errors: list[str] = []
        for idx, provider in enumerate(self._providers, start=1):
            try:
                result = await provider.generate(prompt)
                if idx > 1:
                    logger.info("✓ fallback 到第 %d 个 provider 成功", idx)
                return result
            except Exception as e:
                msg = f"provider #{idx} 失败: {e}"
                errors.append(msg)
                logger.warning("⚠️  %s", msg)
        raise RuntimeError(f"所有 provider 失败 ({len(errors)} 个): {' | '.join(errors)}")
```

2. 改造 `create_provider`（line 200-223），改为按 `providers_chain` 构建链：
```python
def _build_single_provider(p_cfg) -> LLMProvider:
    """根据单个 LLMProviderConfig 构建 OpenAIProvider（内部辅助）。

    p_cfg 可以是 AnalysisConfig（主 provider 走旧字段）或 LLMProviderConfig（备用）。
    两者字段名一致（provider/api_base/api_key/model_name），鸭子类型兼容。
    """
    provider_name = p_cfg.provider.lower().strip()

    if provider_name == "openai":
        if not p_cfg.api_base:
            raise ValueError("OpenAI provider 需要配置 api_base")
        return OpenAIProvider(
            api_base=p_cfg.api_base,
            api_key=p_cfg.api_key,
            model_name=p_cfg.model_name or "gpt-4o-mini",
        )
    elif provider_name == "ollama":
        return OpenAIProvider(
            api_base=p_cfg.api_base or "http://localhost:11434/v1",
            api_key=p_cfg.api_key or "ollama",
            model_name=p_cfg.model_name or "qwen2.5:7b",
        )
    else:
        raise ValueError(f"不支持的 provider: {p_cfg.provider}")


def create_provider(config: AnalysisConfig) -> LLMProvider:
    """根据配置构建 provider 链。

    - 单 provider 配置（无 extra_providers）→ 长度为 1 的链
    - 多 provider 配置 → 主 provider + extra_providers 按序组成的链
    - ``enabled=False`` 或主 provider 未配置 → 抛 ValueError（调用方应先检查）

    兼容性：返回类型仍是 ``LLMProvider``（``FallbackChainProvider`` 实现此协议），
    调用方代码（``analyze_content`` / ``_probe_provider``）不需要改。

    Raises:
        ValueError: 不支持的 provider 类型，或链为空
    """
    chain = config.providers_chain
    if not chain:
        raise ValueError("AI 分析未启用或未配置 provider")
    providers = [_build_single_provider(p) for p in chain]
    return FallbackChainProvider(providers=providers)
```

**关键**：`web/routes/settings.py:75` 的 `_probe_provider` **不变** — 它构造 `AnalysisConfig(provider=provider, api_base=..., api_key=..., model_name=...)` 没有 `extra_providers`，`providers_chain` 返回单元素 list，`create_provider` 返回长度为 1 的链，行为与单 provider 完全一致。

**验证**：
- `uv run pytest tests/test_summarizer.py -x`（现有 + 新增测试全过）
- 重点确认：`test_create_provider_openai`（line 21-32）仍 pass（链长度 1 + `isinstance(chain, FallbackChainProvider)`），但断言 `provider.api_base == "https://api.openai.com/v1"` 需要改为访问 `chain._providers[0].api_base`（**测试需要小改**）

**重要测试调整说明**：
现有 `TestCreateProvider` 类的断言（line 28-32）写的是 `isinstance(provider, OpenAIProvider)`，改造后 `create_provider` 返回 `FallbackChainProvider`，这些断言会失败。**改造测试时不要删除旧测试**，改为：
```python
def test_create_provider_openai_returns_chain_with_openai_inside(self) -> None:
    config = AnalysisConfig(provider="openai", api_base="https://api.openai.com/v1", api_key="sk-x", model_name="gpt-4o-mini")
    chain = create_provider(config)
    assert isinstance(chain, FallbackChainProvider)
    assert len(chain._providers) == 1
    assert isinstance(chain._providers[0], OpenAIProvider)
    assert chain._providers[0].api_base == "https://api.openai.com/v1"
    assert chain._providers[0].api_key == "sk-x"
    assert chain._providers[0].model_name == "gpt-4o-mini"
```
同理改造 `test_create_provider_ollama_default_api_base` / `test_create_provider_ollama_custom_api_base` / `test_create_provider_case_insensitive`。`test_create_provider_unknown_raises` / `test_create_provider_codebuddy_removed` / `test_create_provider_openai_missing_api_base_raises` 不变（仍抛 ValueError）。

---

### 任务 5：改造 analyze_content 标记 failed=True

**测试先写** `tests/test_summarizer.py`（`TestAnalyzeContent` 类追加）：
```python
@pytest.mark.asyncio
async def test_analyze_content_all_providers_fail_sets_failed_true(
    self, caplog: pytest.LogCaptureFixture
) -> None:
    """fallback 链全失败时 result.failed=True（不是空 result）。"""
    config = Config()
    config.analysis.enabled = True
    config.analysis.provider = "openai"
    config.analysis.api_base = "https://example.com/v1"
    config.analysis.api_key = "k"

    with patch("core.summarizer.create_provider") as mock_cp:
        chain = mock_cp.return_value
        chain.generate = AsyncMock(side_effect=RuntimeError("all failed"))

        with caplog.at_level("WARNING", logger="core.summarizer"):
            result = await analyze_content(
                source_id="bili:BV1", title="T", author="A", text="正文", config=config,
            )

    assert result.failed is True
    assert result.is_ai is False
    assert result.summary == ""  # 失败时字段为空
    assert any("AI 内容分析失败" in r.message for r in caplog.records)

@pytest.mark.asyncio
async def test_analyze_content_empty_text_does_not_set_failed(self) -> None:
    """空正文走 source='empty' 分支，不应标记 failed=True（合理跳过）。"""
    config = Config()
    config.analysis.enabled = True

    result = await analyze_content(source_id="x", title="t", author="a", text="   ", config=config)
    assert result.failed is False
    assert result.source == "empty"

@pytest.mark.asyncio
async def test_analyze_content_disabled_does_not_set_failed(self) -> None:
    config = Config()
    config.analysis.enabled = False

    result = await analyze_content(source_id="x", title="t", author="a", text="txt", config=config)
    assert result.failed is False
    assert result.source == "none"
```

**实现** `core/summarizer.py`（改造 `analyze_content` line 226-265）：
```python
async def analyze_content(
    source_id: str,
    title: str,
    author: str,
    text: str,
    config: Config,
) -> AnalysisResult:
    """一次性产出摘要/关键词/标签/一句话总结（fallback 链入口）。

    fallback 链全部失败时返回 ``AnalysisResult(failed=True)``，调用方
    （``summarize_phase``）据此决定是否卡住 phase。

    Args:
        source_id: 来源标识（仅用于日志）
        title/author/text: 待分析内容
        config: 全局配置

    Returns:
        AnalysisResult。失败时 ``failed=True``，``is_ai=False``，字段为空。
    """
    if not config.analysis.enabled:
        logger.debug("AI 分析已禁用，返回空结果: %s", source_id)
        return AnalysisResult(source="none")

    if not text.strip():
        return AnalysisResult(source="empty")  # 合理跳过，failed 保持 False

    try:
        provider = create_provider(config.analysis)
        prompt = _ANALYSIS_PROMPT_TEMPLATE.format(title=title, author=author, text=text)
        raw = await provider.generate(prompt)
        result = parse_markdown_analysis(raw)
        result.is_ai = True
        result.source = config.analysis.provider
        logger.info("AI 内容分析成功: %s", source_id)
        return result
    except Exception as e:
        # fallback 链全失败（或单 provider 失败）
        logger.warning("AI 内容分析失败 (%s): %s", source_id, e)
        return AnalysisResult(source="none", failed=True)
```

**唯一改动**：`except` 分支的 `AnalysisResult(source="none")` 加 `failed=True`。

**验证**：`uv run pytest tests/test_summarizer.py::TestAnalyzeContent -x`

---

### 任务 6：改造 summarize_phase 返回值（bili 跨平台 handler）

**测试先写** `tests/test_platform_handlers.py`（追加，参照 `test_transcribe_phase_missing_filepath_returns_false_with_error` 模式 line 271-310）：
```python
@pytest.mark.asyncio
async def test_summarize_phase_returns_false_on_analysis_failed(
    config: Config, store: MessageStore
) -> None:
    """AI 摘要 fallback 全失败时 summarize_phase 必须 return False。"""
    import sys
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401

        from core.summarizer import AnalysisResult
        from unittest.mock import AsyncMock, patch

        # mock analyze_content 返回 failed=True
        with patch("platforms.bilibili.handlers.analyze_content", new=AsyncMock(
            return_value=AnalysisResult(source="none", failed=True)
        )):
            handler = PipelineEngine._handlers.get(("*", Phase.SUMMARIZED))
            assert handler is not None

            msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
            assert msg is not None
            ctx = PhaseContext(msg=msg, config=config)
            ctx.transcript_text = "transcript 内容"  # 提供正文让 analyze_content 真的被调

            result = await handler(ctx)

        assert result is False
        assert "AI 摘要失败" in ctx.error or "摘要" in ctx.error
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)

@pytest.mark.asyncio
async def test_summarize_phase_returns_true_when_analysis_succeeds(config: Config, store: MessageStore) -> None:
    """analyze_content 成功（failed=False）时 summarize_phase 返回 True。

    覆盖两类 success 场景：
    - 空正文 → analyze_content 走 source='empty' 分支返回 failed=False
    - LLM 配置 disabled → analyze_content 返回 source='none' failed=False

    本测试走 disabled 路径（不需要 mock，端到端验证 analyze_content 真实行为）。
    空正文路径由 test_summarizer.py::test_analyze_content_empty_text_does_not_set_failed 覆盖。
    """
    import sys
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401

        # 不 mock analyze_content：让真实实现跑。
        # config.analysis.enabled=False → analyze_content 返回 AnalysisResult(source='none', failed=False)
        config.analysis.enabled = False

        handler = PipelineEngine._handlers.get(("*", Phase.SUMMARIZED))
        assert handler is not None

        msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
        assert msg is not None
        ctx = PhaseContext(msg=msg, config=config)

        result = await handler(ctx)

        assert result is True  # 关键：分析成功（非 failed）就推进
        assert ctx.error == ""
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)
```

**实现** `platforms/bilibili/handlers.py`（改造 line 215-230）：
```python
    try:
        analysis = await analyze_content(
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=text_to_summarize,
            config=ctx.config,
        )
        if analysis.failed:
            # fallback 链全部失败：标记 ctx.error 让 engine 处理 retry
            # （engine 会读 retry_count 决定是 mark_retry_failure 还是 mark_error）
            ctx.error = "AI 摘要失败：所有 provider 不可用"
            logger.warning("⚠️  %s — 消息将卡在 SUMMARIZED 阶段等待重试", ctx.error)
            return False
        ctx.summary_text = analysis.summary
        ctx.keywords = analysis.keywords
    except Exception as exc:
        # analyze_content 内部已吞异常；这里兜底防极端情况
        ctx.error = f"摘要/关键词生成异常: {exc}"
        logger.error("✗ %s", ctx.error)
        logger.exception("Analysis failed for %s", source_id)
        return False

    return True
```

**关键改动**：
1. 加 `analysis.failed` 分支检查
2. `except` 分支也返回 False（原来 return True）
3. **不设 `ctx.permanent_error`**：LLM 失败是临时性故障（provider 余额/网络抖动），retry 是合理语义。与 transcribe 文件缺失等永久失败区分（见 Issue 6 / R10）。

**验证**：`uv run pytest tests/test_platform_handlers.py -x`

---

### 任务 7：改造 weibo download handler 内联摘要

**测试先写** `tests/test_weibo_integration.py`（参照已有测试模式追加；如果该文件不方便加，新建 `tests/test_weibo_summary_retry.py`）：
```python
@pytest.mark.asyncio
async def test_weibo_download_returns_false_on_summary_failed(config: Config, store: MessageStore) -> None:
    """weibo 内联摘要 fallback 全失败时 download handler 必须 return False（卡在 DOWNLOADED）。"""
    import sys
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.weibo.handlers  # noqa: F401

        from core.summarizer import AnalysisResult
        from unittest.mock import AsyncMock, patch, MagicMock

        # mock download_weibo_media 返回成功
        mock_dl_result = MagicMock()
        mock_dl_result.success = True
        mock_dl_result.image_paths = []
        mock_dl_result.text = "微博正文"

        # mock generate_summary 返回 failed=True（通过 source='none', is_ai=False）
        # 但 generate_summary 是 (summary, source, is_ai) 三元组签名，failed 不在三元组里
        # 解决：generate_summary 内部委托 analyze_content；我们 patch analyze_content
        with patch("platforms.weibo.handlers.download_weibo_media", new=AsyncMock(return_value=mock_dl_result)), \
             patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)), \
             patch("platforms.weibo.handlers.generate_summary", new=AsyncMock(
                 # 模拟失败：summary='', source='none', is_ai=False
                 return_value=("", "none", False)
             )), \
             patch("platforms.weibo.handlers.extract_keywords", new=AsyncMock(return_value=[])), \
             patch("platforms.weibo.handlers.fetch_weibo_comment_highlights", new=AsyncMock(return_value=[])):

            handler = PipelineEngine._handlers.get(("weibo", Phase.DOWNLOADED))
            assert handler is not None

            msg = store.add_new("weibo:abc", "weibo", ContentType.TEXT, 2000000000, "T", "A")
            assert msg is not None
            ctx = PhaseContext(msg=msg, config=config)
            # 让 cookie 路径不触发长文获取
            ctx.config.weibo.auth.cookie = ""

            result = await handler(ctx)

        assert result is False
        assert "摘要" in ctx.error or "summary" in ctx.error.lower()
    finally:
        sys.modules.pop("platforms.weibo.handlers", None)
```

**注意**：weibo 调用的是旧签名 `generate_summary`（返回三元组 `(summary, source, is_ai)`），不是 `analyze_content`（返回 `AnalysisResult`）。`failed` 信息没有传到 `generate_summary` 的返回值。**需要小改 `generate_summary` 包装层**或**直接改 weibo 调用 `analyze_content`**。后者更彻底：

**实现** `platforms/weibo/handlers.py`（改造 line 107-131）：
```python
    # Generate summary and keywords (TEXT type skips SUMMARIZED phase)
    from core.summarizer import analyze_content
    try:
        analysis = await analyze_content(
            source_id=post_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=ctx.content_text,
            config=ctx.config,
        )
        if analysis.failed:
            ctx.error = "AI 摘要失败：所有 provider 不可用"
            logger.warning("⚠️  %s — 消息将卡在 DOWNLOADED 阶段等待重试", ctx.error)
            return False
        ctx.summary_text = analysis.summary
        ctx.keywords = analysis.keywords
        logger.info("📝 摘要 (%s)", analysis.source)
    except Exception as exc:
        ctx.error = f"摘要生成异常: {exc}"
        logger.warning("⚠️  %s", ctx.error)
        return False
```

**关键改动**：
1. 不再调 `generate_summary` + `extract_keywords`（双 AI 请求），改用 `analyze_content`（单请求拿全部字段）— **顺带修了 F1/A3 的双调用低效问题**
2. 摘要失败时 return False（卡 DOWNLOADED）
3. 删除 `ctx.summary_text = ctx.content_text[:500]` fallback（D6 决策）
4. **不设 `ctx.permanent_error`**：与任务 6 一致，LLM 失败是临时故障，retry 是合理语义（见 Issue 6 / R10）

**imports 改动**（line 15）：
```python
# 旧：from core.summarizer import extract_keywords, generate_summary
from core.summarizer import analyze_content
```

**注意遗留测试**：`tests/test_weibo_integration.py` 现有测试如果有 mock `generate_summary` 的，改造后会失败。需要把 mock target 改成 `analyze_content`。**实施时先跑 `uv run pytest tests/test_weibo_integration.py -x` 看哪些破，逐个修**。

**验证**：`uv run pytest tests/test_weibo_integration.py tests/test_weibo_*.py -x`

---

### 任务 8：改造 engine 处理 retry_count 上下限

**⚠️ 现有测试调整说明**（Issue 1 / R1 同等级风险）

engine 失败分支改造后（"retry_count < MAX 时走 mark_retry_failure 不写 error"），
以下现有测试会破：

1. **`tests/test_engine.py:124` `test_process_message_handler_failure_stops_flow`**
   - 现状断言：`updated.error == "download failed"`（断言失败立即写 error）
   - 改造后：handler 失败走 `mark_retry_failure`，写 `last_error` 而非 `error`，
     `updated.error` 会是 `""`，断言失败。
   - **修复方案（推荐）**：把 fixture 改为预置 `retry_count = MAX_SUMMARY_RETRIES - 1`，
     让单次失败就触发 `mark_error`，保持原断言语义（"失败达到上限写 error"）。
     ```python
     # 在 add_new 后、process_message 前加：
     from shared.constants import MAX_SUMMARY_RETRIES
     for _ in range(MAX_SUMMARY_RETRIES - 1):
         store.mark_retry_failure(msg.msg_id, "prev fail")
     # 然后保留原断言 updated.error == "download failed"
     ```
   - **备选方案**：改名为 `test_process_message_handler_failure_writes_last_error`，
     断言改为 `updated.last_error == "download failed"` 且 `updated.error == ""`，
     并新建独立的「达上限 mark_error」测试（即下文 `test_handler_failure_after_max_retries_marks_error`）。
   - **推荐方案 1**：改动最小，保留测试名意图（"失败 stops flow"）。

2. **`tests/test_pipeline_e2e.py:73` `test_full_pipeline_handler_failure`**
   - 现状断言：`msg.error == "download failed"`
   - 同上原因：会破。
   - **修复方案**：fixture 预置 `retry_count = MAX_SUMMARY_RETRIES - 1`（推荐，e2e
     验证「达到上限 stops flow」更有意义），或断言改 `msg.last_error`。

实施步骤：先跑 `uv run pytest tests/test_engine.py::test_process_message_handler_failure_stops_flow tests/test_pipeline_e2e.py::test_full_pipeline_handler_failure -x`
确认两个测试在 engine 改造后真的破，再按上述方案修复。**这两个测试改造是任务 8 必须完成的
一部分，不列入 R1 之外的新风险（与 R1 同等级）。**

**测试先写** `tests/test_engine.py`（追加在文件末尾）：
```python
@pytest.mark.asyncio
async def test_handler_failure_increments_retry_count(config: Config, store: MessageStore) -> None:
    """handler 返回 False 且 retry_count < MAX 时：retry_count += 1，不写 error，cron 仍重试。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        ctx.error = "AI 摘要失败"
        return False

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        pytest.fail("PUSHED 不应被调用")

    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.SUMMARIZED  # 未推进
    assert updated.retry_count == 1
    assert updated.last_error == "AI 摘要失败"
    assert updated.error == ""  # 关键：未达上限，不写 error

@pytest.mark.asyncio
async def test_handler_failure_after_max_retries_marks_error(config: Config, store: MessageStore) -> None:
    """retry_count 达到 MAX_SUMMARY_RETRIES 后：写 error，cron 永久跳过。"""
    from shared.constants import MAX_SUMMARY_RETRIES
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        ctx.error = "AI 摘要失败"
        return False

    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "T", "A")
    assert msg is not None
    # 预置 retry_count = MAX - 1，下一次失败应触发 mark_error
    store.mark_retry_failure("bili:BV1", "prev fail")
    for _ in range(MAX_SUMMARY_RETRIES - 2):
        store.mark_retry_failure("bili:BV1", "prev fail")
    pre = store.get_message("bili:BV1")
    assert pre is not None and pre.retry_count == MAX_SUMMARY_RETRIES - 1

    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.SUMMARIZED
    assert updated.error != ""  # 关键：达到上限，写 error
    assert "AI 摘要失败" in updated.error
    assert updated.retry_count == MAX_SUMMARY_RETRIES

@pytest.mark.asyncio
async def test_handler_permanent_error_marks_error_immediately(
    config: Config, store: MessageStore
) -> None:
    """Issue 6: handler 标记 ctx.permanent_error=True 时直接 mark_error，跳过 retry。

    场景：transcribe 文件路径缺失等永久失败重试无意义，必须立即让 cron 永久跳过，
    避免 5 次无意义重试刷日志。
    """
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tc(ctx: PhaseContext) -> bool:
        ctx.error = "transcribe 文件路径缺失"
        ctx.permanent_error = True  # 关键：标记永久失败
        return False

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
    assert msg is not None
    # 即便 retry_count = 0（远未达上限），permanent_error 也立即触发 mark_error
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.TRANSCRIBED  # 未推进
    assert updated.error != ""  # 关键：直接 mark_error，不等 retry
    assert "transcribe 文件路径缺失" in updated.error
    assert updated.retry_count == 0  # 关键：未走 retry 路径，retry_count 不增

@pytest.mark.asyncio
async def test_handler_success_resets_retry_count(config: Config, store: MessageStore) -> None:
    """handler 成功后 retry_count 必须重置为 0（之前失败过的消息恢复后清状态）。"""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        ctx.summary_text = "成功摘要"
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.DYNAMIC, 2000000000, "T", "A")
    assert msg is not None
    store.mark_retry_failure("bili:BV1", "prev fail")
    store.mark_retry_failure("bili:BV1", "prev fail")
    pre = store.get_message("bili:BV1")
    assert pre is not None and pre.retry_count == 2

    msg = store.get_message("bili:BV1")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED
    assert updated.retry_count == 0  # 重置
    assert updated.last_error == ""
```

**实现** `core/engine.py`：

1. 顶部 import 加（line 16 之后）：
```python
from shared.constants import MAX_SUMMARY_RETRIES
```

2. 改造 `process_message` 的失败分支（line 157-161）：
```python
            success = await handler(ctx)
            if not success:
                # 失败处理：三档策略
                # 1. ctx.permanent_error=True（handler 主动标记永久失败）：
                #    直接 mark_error 跳过 retry，cron 永久跳过。
                #    用于 fail-fast 场景：transcribe 文件路径缺失、access_limited 等
                #    重试无意义的失败（Issue 6 / R10）。
                # 2. retry_count < MAX：写 last_error，cron 仍会重试此消息
                # 3. retry_count >= MAX：写 error，cron 永久跳过（避免无限重试）
                current = store.get_message(msg.msg_id)
                current_count = current.retry_count if current else 0
                if ctx.permanent_error:
                    store.mark_error(msg.msg_id, ctx.error)
                    logger.warning(
                        "⛔ %s:%s 永久失败（handler 标记 permanent_error）: %s（cron 将跳过）",
                        msg.platform, msg.msg_id, ctx.error,
                    )
                elif current_count + 1 >= MAX_SUMMARY_RETRIES:
                    store.mark_error(msg.msg_id, ctx.error)
                    logger.warning(
                        "⛔ %s:%s 连续失败 %d 次达到上限，标记永久错误（cron 将跳过）",
                        msg.platform, msg.msg_id, current_count + 1,
                    )
                else:
                    store.mark_retry_failure(msg.msg_id, ctx.error)
                    logger.info(
                        "↻ %s:%s 失败（第 %d/%d 次），将在下次 cron 重试",
                        msg.platform, msg.msg_id, current_count + 1, MAX_SUMMARY_RETRIES,
                    )
                store.save()
                break
```

3. 改造 `process_message` 的成功分支（line 163-167），成功时重置 retry：
```python
            msg.phase = next_phase
            store.mark_phase(msg.msg_id, next_phase)
            _flush_ctx_to_store(msg.msg_id, ctx, store, next_phase)
            # 成功推进：重置 retry_count（之前失败过的消息恢复后清状态）
            updated = store.get_message(msg.msg_id)
            if updated and updated.retry_count > 0:
                store.mark_retry_reset(msg.msg_id)
            logger.info("%s:%s → %s ✓", msg.platform, msg.msg_id, next_phase.name)
            store.save()
```

**注意**：与失败分支保持一致（失败分支已有 `current = store.get_message(...)` 局部变量）。成功分支同样用 `updated` 局部变量避免重复 dict lookup（R3 提到的优化在此一并完成）。

**验证**：`uv run pytest tests/test_engine.py -x`

---

### 任务 9：更新 config.toml.example + settings.html 文案

**测试**：无单元测试（配置示例 + 文案），靠 manual verify + 现有 test_web_settings_analysis 不破。

**实现**：

1. `config/config.toml.example` 的 `[analysis]` 段（line 49-60）后追加（注释示例）：
```toml
# ── AI 分析设置 ────────────────────────────────────────────────
[analysis]
enabled = true
provider = "openai"
api_base = ""
api_key = ""
model_name = ""

# 备用 provider 链（可选）：主 provider 失败（401/超时/网络错）时按序尝试。
# 失败重试上限见 shared/constants.py:MAX_SUMMARY_RETRIES（默认 5 次）。
# 启用方法：取消下面注释，按需填入备用 provider。
#
# [[analysis.extra_providers]]
# provider = "ollama"
# api_base = "http://localhost:11434/v1"
# api_key = "ollama"
# model_name = "qwen2.5:7b"
#
# [[analysis.extra_providers]]
# provider = "openai"
# api_base = "https://backup-provider.com/v1"
# api_key = "sk-backup"
# model_name = "gpt-4o-mini"
```

2. `web/templates/settings.html` 副标题（line 87-89）改为：
```html
<p class="text-xs text-[var(--text-secondary)] mb-4">
  摘要与关键词生成。多 provider 失败自动 fallback；连续失败 5 次的消息会被标记为永久错误（避免无限重试）。
  备用 provider 请在 config.toml 的 <code>[analysis.extra_providers]</code> 中配置。
</p>
```

**验证**：
- `uv run pytest tests/test_web_settings_analysis.py tests/test_web_settings.py -x`
- 启动 web server，访问 `/settings`，确认副标题正确显示

---

### 任务 10：端到端验证

```bash
# 单元测试全套
uv run pytest -x

# 静态检查
uv run ruff check .
uv run pyright

# 集成验证（需要真实 LLM 配置）
uv run trawler check --platform bilibili
# 检查 data/messages.json 出现 "retry_count" 和 "last_error" 字段
# 故意配置错的 api_key，验证消息卡在 SUMMARIZED 且 retry_count 递增
```

**手动验证脚本**：
1. 改 `config.toml` 让主 provider 用错误 key，备用 provider 用正确 key：
```toml
[analysis]
enabled = true
provider = "openai"
api_base = "https://wrong.example.com/v1"
api_key = "invalid"

[[analysis.extra_providers]]
provider = "openai"
api_base = "https://real-provider.com/v1"
api_key = "sk-real"
model_name = "gpt-4o-mini"
```
2. 跑 `uv run trawler check --platform bilibili`
3. 预期：日志出现 `⚠️ provider #1 失败` → `✓ fallback 到第 2 个 provider 成功`
4. 摘要正常生成，messages.json 的 `summary` 字段有内容

**反向验证**：
1. 主 + 备用 provider 都用错 key
2. 跑 6 次 cron（手动重复触发）
3. 预期：前 5 次 retry_count 递增（1→5），第 6 次（达上限）后 messages.json 出现 `error` 字段，第 7 次 cron 日志出现 `⏭ 跳过错误消息`

---

## 6. 验证步骤汇总

```bash
# 各模块单元测试（按依赖顺序）
uv run pytest tests/test_message_store.py -x          # 任务 1, 2
uv run pytest tests/test_config.py -x                 # 任务 3
uv run pytest tests/test_summarizer.py -x             # 任务 4, 5
uv run pytest tests/test_platform_handlers.py -x      # 任务 6
uv run pytest tests/test_weibo_integration.py -x      # 任务 7
uv run pytest tests/test_engine.py -x                 # 任务 8
uv run pytest tests/test_web_settings_analysis.py -x  # 任务 9

# 端到端
uv run pytest tests/test_pipeline_e2e.py -x
uv run pytest tests/test_pipeline_concurrent.py -x

# 全套
uv run pytest -x

# 静态
uv run ruff check .
uv run pyright
```

---

## 7. 风险与不确定项

| ID | 风险 | 影响 | 缓解 |
|----|------|------|------|
| R1 | `TestCreateProvider` 现有 7 个测试断言 `isinstance(provider, OpenAIProvider)` 会失败；**Issue 1 同等级**：`test_process_message_handler_failure_stops_flow`（test_engine.py:124）和 `test_full_pipeline_handler_failure`（test_pipeline_e2e.py:73）断言 `error == "download failed"` 也会失败 | 任务 4 和任务 8 实施时阻塞 | 任务 4 明确要求改造 `TestCreateProvider` 断言为 `isinstance(chain, FallbackChainProvider)` + 访问 `chain._providers[0]`，改造清单已列出。**任务 8** 开头的「现有测试调整说明」明确列出另两条测试的两种修复方案（推荐：fixture 预置 retry_count = MAX-1，保持原断言语义）。三组测试改造均为任务执行的一部分，非新风险。 |
| R2 | weibo handler 改造（任务 7）从双 AI 调用改单调用，可能影响 `tests/test_weibo_integration.py` 现有测试 | 任务 7 实施时阻塞 | 任务 7 步骤里写了「先跑一次看哪些破，逐个把 mock target 从 `generate_summary` 改成 `analyze_content`」。预期 1-3 个测试需调整 mock。 |
| R3 | ~~engine 改造（任务 8）的 `store.get_message(msg.msg_id).retry_count` 多次调用 dict lookup~~ | ~~微小性能开销~~ | **已修复**（Issue 5）：任务 8 成功分支改为 `updated = store.get_message(...)` 局部变量，与失败分支一致。 |
| R4 | `MAX_SUMMARY_RETRIES = 5` 是硬编码，用户无法调整 | 高级用户场景受限 | 列入 §8 后续清理。当前足够覆盖 99% 场景。 |
| R5 | 永久失败（mark_error）的消息需要用户手动 reset_to_phase 才能重试 | 用户体验：坏消息卡死后需要干预 | dashboard 应提供「重试」按钮（列入 §8）。当前可接受用户手动改 messages.json 清 error 字段。 |
| R6 | `_probe_provider`（settings 测试连通性）走的是 `create_provider`，现在返回 FallbackChainProvider | 任务 4 后 `_probe_provider` 行为可能变 | F7 已分析：`_probe_provider` 构造的 AnalysisConfig 没有 extra_providers，链长度 1，行为与单 provider 完全一致。`test_web_settings_analysis.py` 应该全过。任务 4 验证步骤会跑此测试。 |
| R7 | `core/pipeline.py` 仍 import 旧的 `generate_summary` / `extract_keywords` 注释（line 8-9） | 文档过时 | 仅注释，不影响运行。可在任务 9 顺手更新文档。 |
| R8 | bili 动态（DYNAMIC）当前 detector 未回填 `dyn.content` 到 body（F5/D6 遗留） | bili 动态消息 text_to_summarize 可能为空，走 source='empty' 分支不卡住 | 这是已知遗留问题（`2026-06-25-messagerecord-body-summary.md` §8 第 1 条）。本 plan 不修。影响：bili 动态即使 LLM 挂了也会推送（合理：动态本身没文字内容）。 |
| R9 | 评论获取失败仍 return True（D5） | 极端情况：评论 API 永久挂但摘要正常，消息持续推送（无评论） | 接受现状。评论是辅助信息，不应卡主流程。 |
| R10 | 任务 8 engine 改造后，所有 phase handler 失败都走 retry_count 机制（不只 SUMMARIZED） | download/transcribe 失败也变成「重试 5 次后才 mark_error」。**问题**：transcribe 因文件路径缺失返回 False 是永久失败，重试 5 次毫无意义（每次 cron 都刷日志） | **已修复**（Issue 6）：在 PhaseContext 加运行时字段 `permanent_error: bool = False`，handler 主动标记后 engine 失败分支**优先检查此字段**，直接 `mark_error` 跳过 retry。任务 1 加字段 + 默认值测试，任务 8 加 engine 消费逻辑 + `test_handler_permanent_error_marks_error_immediately` 测试。现有 fail-fast 测试（如 `test_transcribe_phase_missing_filepath_returns_false_with_error` test_platform_handlers.py:272）**需要小改**：handler 实现里加 `ctx.permanent_error = True` 一行（在 return False 前），断言不变。**未列入 R10 的清理项**：其他可能也需要 permanent_error 的 handler（如 access_limited / unsupported content_type）由实施时按需添加，本 plan 仅提供机制 + 测试模板。 |

---

## 8. 后续可选清理（不在本 plan 范围）

1. **settings UI 支持 fallback 链编辑**（D10）：动态 list 编辑器，让用户在 UI 加/删/排序备用 provider。需要前端 JS 工作量大。当前用户改 config.toml。
2. **MAX_SUMMARY_RETRIES 配置化**（R4）：加到 `AnalysisConfig.max_retries: int = 5`，UI 暴露 slider。
3. **dashboard 加「重试」按钮**（R5）：对 `error != ""` 的消息提供「重置 error + retry_count」按钮，避免用户手改 messages.json。
4. **provider 健康检查 + 自动降级**：记录每个 provider 的近期成功率，自动跳过挂掉的 provider（不必每次都试一遍）。需要 `MessageStore` 加 provider stats 字段。
5. **按错误类型决定是否 fallback**（F2）：解析 `RuntimeError` message 里的 status_code，4xx（永久）不 fallback 直接到 retry 上限，5xx/timeout（临时）正常 fallback。需要 `OpenAIProvider.generate` 抛更结构化的异常类型。
6. **weibo 双调用问题已在任务 7 顺手修复**（顺带把 F1/A3 的双 AI 请求合并为单 `analyze_content` 调用）。如果 `generate_summary` / `extract_keywords` 旧包装函数不再有调用方，可考虑删除（保留也无害，是稳定的薄包装）。
7. **`permanent_error` 标记全量审计**（Issue 6 / R10）：本 plan 提供 perma-fail 机制 + transcribe 文件缺失的示范用法，但其他 fail-fast 场景（download handler 的 `access_limited`、unsupported content_type、auth 失败等）实施时需要审计。建议建一个 grep 清单 `rg "return False" platforms/` 逐个评估是否需要标 `ctx.permanent_error = True`。
8. **`providers_chain` 全空时的诊断**（Issue 4 边界）：当 `enabled=True` 但主 provider `api_base=''` 且 `extra_providers=[]` 时，`providers_chain` 返回空，`create_provider` 抛 ValueError → analyze_content `failed=True` → 卡 SUMMARIZED 重试 5 次。这种"配置残缺"场景应该返回 source='none' 不卡（与 disabled 语义对齐），但本 plan 视为「用户配置错误」由 §8 增强（如 `_parse_config` 时校验 enabled=True 必须至少有一个非空 provider）。

---

## 9. 任务依赖图

```
任务 1 (protocols 加字段 + constants)
    │
    ▼
任务 2 (store 加反序列化 + mark_retry_*)
    │
    ├──► 任务 3 (config 加 LLMProviderConfig + providers_chain)  ← 独立分支
    │       │
    │       ▼
    │   任务 4 (FallbackChainProvider + create_provider)  ← 依赖任务 3
    │       │
    │       ▼
    │   任务 5 (analyze_content 标记 failed=True)  ← 依赖任务 1, 4
    │       │
    │       ├──► 任务 6 (summarize_phase 改返回值)  ← 依赖任务 5
    │       │
    │       └──► 任务 7 (weibo download 改返回值)  ← 依赖任务 5
    │
    ▼
任务 8 (engine retry_count 处理)  ← 依赖任务 2（mark_retry_*）+ 任务 6,7（handler 返回 False 触发）
    │
    ▼
任务 9 (config.toml.example + settings.html 文案)  ← 独立
    │
    ▼
任务 10 (端到端验证)  ← 依赖全部
```

**并行机会**：
- 任务 3 + 任务 1/2 可并行（不同文件，无依赖）
- 任务 6 + 任务 7 可并行（不同 handler 文件）
- 任务 9 任何时候都可做

**串行约束**：
- 任务 4 必须在任务 3 后（FallbackChainProvider 依赖 `providers_chain`）
- 任务 5 必须在任务 4 后（analyze_content 依赖 create_provider 返回链）
- 任务 8 必须在任务 2 + 任务 6/7 后（engine retry 依赖 store 方法 + handler 返回 False）

---

## 调研发现的「惊喜」（用户原始描述未预料的）

1. **「卡在 SUMMARIZED 让 cron 自动重试」与现有 cron 跳过逻辑冲突**（F4）
   用户原描述：「fallback 全失败时返回 False，让消息卡在 SUMMARIZED，下一轮 cron 自动重试」。
   实际：`engine.run_platform:230-232` 的 `if msg.error: continue` 会让任何被 `mark_error` 的消息直接跳过。直接返回 False 会触发 mark_error → 永久死锁。
   **本 plan 解决方案**（D7）：引入 `retry_count` + `last_error` 双字段，与 `error` 字段语义分离。失败 < MAX 次时写 `last_error`（cron 仍重试），达 MAX 才写 `error`（cron 跳过）。

2. **weibo 是双 AI 请求（F1/A3）**
   用户原描述只关注 summarize_phase，但 weibo 在 download handler 内联调 `generate_summary` + `extract_keywords` 两次。本 plan 任务 7 顺带把双调用合并为单 `analyze_content`（更高效）。

3. **`OpenAIProvider.generate` 不区分错误类型（F2）**
   用户原描述提到「401/超时/网络错/5xx 等」都 fallback。实际所有错误都被包成 `RuntimeError(msg)`，无法从异常类型区分。本 plan 选择「全部 fallback」（与用户预期一致），但牺牲了「4xx 永久失败快速跳过」的优化。列入 §8 第 5 条。

4. **`web/routes/settings.py` 的 `_probe_provider` 也走 `create_provider`（A4）**
   用户原描述没提到 settings UI。本 plan F7 + R6 论证：`_probe_provider` 行为透明兼容（链长度 1 时与单 provider 一致），不需要改造。

5. **`text=''` 走 `source='empty'` 不应触发 failed=True（用户已识别）**
   本 plan D4 通过 `AnalysisResult.failed` 字段区分：`source='empty'` 时 `failed=False`（合理跳过），`source='none'` 且 fallback 全失败时 `failed=True`。任务 5 的实现严格区分这两个分支。

6. **engine retry 机制会影响所有 phase handler（不只 SUMMARIZED）**（R10 / Issue 6）
   用户原描述聚焦 summarize_phase，但 engine 的 retry_count 改造是全局的。download/transcribe
   失败也会走 retry。**问题**：transcribe 因文件路径缺失返回 False 是永久失败，重试 5 次无意义。
   **本 plan 解决方案**：加 `PhaseContext.permanent_error: bool` 运行时字段（任务 1），handler
   主动标记后 engine 失败分支优先检查直接 mark_error 跳过 retry（任务 8 + 测试）。R10 已重写
   为「已修复」。LLM 失败（任务 6/7）故意不标 permanent_error（临时故障 retry 合理）。

7. **settings UI 的副标题文案过时**（line 87-89）
   原文案「AI 失败时自动降级到本地 TF 摘要」是历史描述（Bug 2 重构后已删除 TF fallback）。本 plan 任务 9 顺手更新为新机制描述。
