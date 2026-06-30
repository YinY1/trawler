# Issue #56 — AI 摘要解析 silent empty 修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 AI 摘要解析 silent empty 问题 —— 当 LLM 返回空 content（reasoning 模型把答案错放到 `reasoning_content`）或字段标题带尾随冒号/加粗时，摘要不再静默丢失；同时增加 `AnalysisResult.raw` 字段 + handler warning，让此类故障可观测。

**Architecture:** 三层防御 + 一层观测：
1. **Provider 层**（`OpenAIProvider.generate`）：扩展响应解析，`content=""` 时 fallback 到 `reasoning_content`，扩展 except 子句，加 DEBUG 日志。
2. **解析层**（`parse_markdown_analysis`）：放宽 `_SECTION_PATTERNS` 兼容尾随冒号 + 加粗标题 + `总结` 同义词。
3. **观测层**（`AnalysisResult.raw` + `summarize_phase` handler）：原始响应记录到 `AnalysisResult.raw`；handler 在解析为空时打 warning 日志，消息仍接受空 summary 继续推进。
4. **不改 `core/engine.py:49` 的 `if ctx.summary_text:` 守卫**（有意保护，空 summary 不落盘是预期行为）。

**Tech Stack:** Python 3.12, dataclass, re, httpx, pytest (asyncio + caplog)。

---

## File Structure

| 文件 | 操作 | 责任 |
|---|---|---|
| `core/summarizer.py` | Modify | Provider 解析扩展 + 正则放宽 + AnalysisResult 加 raw 字段 + DEBUG 日志 |
| `platforms/bilibili/handlers.py` | Modify | `summarize_phase` 加 warning 兜底观测 |
| `tests/test_summarizer.py` | Modify | 新增 5 个测试用例覆盖 provider fallback / 解析放宽 / raw 字段 |
| `tests/test_handlers_summarize_phase.py` | Create | 新增 handler warning 测试（独立文件，避免污染 test_summarizer.py 的 458 行结构） |

---

## Task 1: 给 AnalysisResult 加 raw 字段（观测前提）

**Files:**
- Modify: `core/summarizer.py:70-80`
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: 写失败测试 — 字段存在且默认空串**

在 `tests/test_summarizer.py` 的 `TestDataModelDefaults` 类（约 line 182）末尾追加：

```python
    def test_analysis_result_has_raw_default_empty_string(self) -> None:
        """Issue #56: AnalysisResult.raw 记录原始 LLM 响应，便于排查 silent empty。"""
        r = AnalysisResult()
        assert r.raw == ""
        assert isinstance(r.raw, str)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_summarizer.py::TestDataModelDefaults::test_analysis_result_has_raw_default_empty_string -v`

Expected: FAIL with `AttributeError: 'AnalysisResult' object has no attribute 'raw'` 或 dataclass `TypeError`。

- [ ] **Step 3: 修改 AnalysisResult 加 raw 字段**

修改 `core/summarizer.py:70-80`，在 `failed` 字段后追加 `raw` 字段：

```python
@dataclass
class AnalysisResult:
    """``analyze_content`` 的结构化结果。"""

    summary: str = ""
    one_line_summary: str = ""
    keywords: list[str] = field(default_factory=lambda: [])
    tags: list[str] = field(default_factory=lambda: [])
    is_ai: bool = False
    source: str = "none"  # provider name | "none" | "empty"
    failed: bool = False  # True 表示 fallback 链全部失败（与 source="empty" 区分）
    # Issue #56: 原始 LLM 响应文本，用于排查 silent empty（解析为空但 HTTP 200 的情况）。
    # analyze_content 在调用 parse_markdown_analysis 之前赋值；失败/禁用分支保持 ""。
    raw: str = ""
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_summarizer.py::TestDataModelDefaults::test_analysis_result_has_raw_default_empty_string -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): add AnalysisResult.raw field for silent-empty observability (issue #56)"
```

---

## Task 2: OpenAIProvider.generate 扩展 — 处理 reasoning_content + None content

**Files:**
- Modify: `core/summarizer.py:161-195`（`OpenAIProvider.generate`）
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: 写失败测试 — reasoning_content fallback**

在 `tests/test_summarizer.py` 末尾（line 458 之后）追加新测试类：

```python
class TestOpenAIProviderResponseParsing:
    """Issue #56 场景 A: reasoning 模型把答案放到 reasoning_content，
    content="" 时 provider 应 fallback 到 reasoning_content。"""

    @pytest.mark.asyncio
    async def test_provider_fallback_to_reasoning_content(self) -> None:
        """content='' 时 fallback 到 reasoning_content，不丢失摘要。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "## 摘要\n这是 reasoning 里的答案\n\n## 一句话总结\n一句话",
                    }
                }
            ]
        }

        with patch("core.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = mock_client.post.return_value
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            result = await provider.generate("test prompt")

        assert "reasoning 里的答案" in result
        assert result.startswith("## 摘要")

    @pytest.mark.asyncio
    async def test_provider_handles_none_content(self) -> None:
        """content=null 不应抛 AttributeError，应 fallback 到 reasoning_content 或返回空串。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "## 摘要\nfallback 答案",
                    }
                }
            ]
        }

        with patch("core.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = mock_client.post.return_value
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            result = await provider.generate("test prompt")

        assert "fallback 答案" in result

    @pytest.mark.asyncio
    async def test_provider_normal_content_unaffected(self) -> None:
        """content 非空时不应触碰 reasoning_content（回归保护）。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="gpt-4o-mini")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": "## 摘要\n正常 content",
                        "reasoning_content": "## 摘要\n这是 reasoning 不应被使用",
                    }
                }
            ]
        }

        with patch("core.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = mock_client.post.return_value
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            result = await provider.generate("test prompt")

        assert "正常 content" in result
        assert "不应被使用" not in result

    @pytest.mark.asyncio
    async def test_provider_empty_content_and_no_reasoning_returns_empty(self) -> None:
        """content='' 且无 reasoning_content 时返回空串（让上层解析层负责 silent empty 观测）。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": "",
                    }
                }
            ]
        }

        with patch("core.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = mock_client.post.return_value
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            result = await provider.generate("test prompt")

        assert result == ""

    @pytest.mark.asyncio
    async def test_provider_missing_choices_field_raises_runtime_error(self) -> None:
        """choices 字段缺失时抛 RuntimeError（与 KeyError/IndexError/AttributeError/TypeError 统一处理）。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="gpt-4o-mini")
        fake_response_json = {"error": "internal"}

        with patch("core.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = mock_client.post.return_value
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            with pytest.raises(RuntimeError, match="解析 API 响应失败"):
                await provider.generate("test prompt")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_summarizer.py::TestOpenAIProviderResponseParsing -v`

Expected:
```
test_provider_fallback_to_reasoning_content FAIL：当前 content='' 时直接返回空串,不 fallback 到 reasoning_content
test_provider_handles_none_content FAIL：content=None 时 `.get('content').strip()` 抛 AttributeError 冒泡(当前 except 仅捕获 KeyError/IndexError)
test_provider_missing_choices_field PASS(已通过 except (KeyError, IndexError) 覆盖)
```

- [ ] **Step 3: 修改 OpenAIProvider.generate（含 DEBUG 日志）**

修改 `core/summarizer.py:186-195`（response 解析部分）。**注意 `import httpx` 在 generate 函数内部（line 162），patch 路径是 `core.summarizer.httpx.AsyncClient`**，因为函数内 `import httpx` 后通过 `httpx.AsyncClient` 访问，模块属性 `core.summarizer.httpx` 在函数运行时存在。

将原本的：

```python
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"解析 API 响应失败: {e}")
```

一次性替换为（含 reasoning_content fallback + DEBUG 日志 + 扩展 except）：

```python
        data = response.json()
        try:
            message = data["choices"][0]["message"]
            # Issue #56 场景 A: reasoning 模型（如 deepseek-v4-flash）通过 exusiai 网关时，
            # 长文本场景 content="" / content=null，答案错放到 reasoning_content 字段。
            # content 非空时优先用 content（保持原有行为），content 为空时 fallback 到 reasoning_content。
            # 不拼接两者：reasoning_content 通常是思考链，包含大量中间推理，
            # 与最终答案混在一起会破坏解析。
            content = message.get("content") or ""
            if not content:
                reasoning = message.get("reasoning_content") or ""
                if reasoning:
                    logger.debug(
                        "OpenAI content 为空，fallback 到 reasoning_content (model=%s, len=%d)",
                        self.model_name,
                        len(reasoning),
                    )
                    content = reasoning
            # Issue #56: 记录 raw response 截断（%.500s 是 printf-style 截断到 500 字符，
            # logging 标准用法，避免 % 字段顺序问题）。仅 INFO 看不到，运维设置
            # LOG_LEVEL=DEBUG 时可见。
            logger.debug(
                "LLM raw response (model=%s, content_len=%d): %.500s",
                self.model_name,
                len(content),
                content,
            )
            return content.strip()
        except (KeyError, IndexError, AttributeError, TypeError) as e:
            raise RuntimeError(f"解析 API 响应失败: {e}")
```

> **关键：** DEBUG 日志和 fallback 逻辑一次性写入。**不要**分两步替换 `return content.strip()`,否则 executor 容易漏改或重复加日志。

- [ ] **Step 4: 跑测试确认全部通过**

Run: `uv run pytest tests/test_summarizer.py::TestOpenAIProviderResponseParsing -v`

Expected: 5 个测试全部 PASS。

- [ ] **Step 5: 跑全量 summarizer 测试，确认无回归**

Run: `uv run pytest tests/test_summarizer.py -v`

Expected: 45 个原有测试 + 5 个新测试 = 50 个 PASS。

- [ ] **Step 6: Commit**

```bash
git add core/summarizer.py tests/test_summarizer.py
git commit -m "fix(summarizer): provider falls back to reasoning_content when content is empty (issue #56)"
```

---

## Task 3: 放宽 _SECTION_PATTERNS — 兼容尾随冒号 + 加粗 + 总结同义词

**Files:**
- Modify: `core/summarizer.py:62-67`
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: 写失败测试 — 各种变体标题都能解析**

在 `tests/test_summarizer.py` 的 `TestParseMarkdownAnalysis` 类末尾（约 line 280）追加 5 个测试：

```python
    def test_parse_summary_with_colon_suffix(self) -> None:
        """Issue #56 场景 B: LLM 输出 '## 摘要：'（全角冒号）应能解析。"""
        raw = """## 摘要：
这是带冒号的摘要内容

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "带冒号的摘要内容" in result.summary

    def test_parse_summary_with_half_width_colon(self) -> None:
        """Issue #56: LLM 输出 '## 摘要:'（半角冒号）应能解析。"""
        raw = """## 摘要:
半角冒号也行

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "半角冒号也行" in result.summary

    def test_parse_summary_with_bold_title(self) -> None:
        """Issue #56: LLM 输出 '## **摘要**'（加粗标题）应能解析。"""
        raw = """## **摘要**
这是加粗标题下的内容

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "加粗标题下的内容" in result.summary

    def test_parse_summary_with_bold_title_and_colon(self) -> None:
        """Issue #56: 加粗 + 冒号组合 '## **摘要**：' 应能解析。"""
        raw = """## **摘要**：
组合变体内容

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "组合变体内容" in result.summary

    def test_parse_one_line_summary_accepts_总结_synonym(self) -> None:
        """Issue #56: LLM 输出 '## 总结' 应作为 one_line_summary 解析（同义词兼容）。"""
        raw = """## 摘要
摘要正文

## 总结
这是同义词的一句话总结

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "同义词的一句话总结" in result.one_line_summary
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_summarizer.py::TestParseMarkdownAnalysis -v -k "colon or bold or synonym"`

Expected: 5 个新测试 FAIL（现有严格正则不匹配 `：` `:` `**摘要**` `总结`）。

- [ ] **Step 3: 修改 _SECTION_PATTERNS**

修改 `core/summarizer.py:62-67`，将：

```python
_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"^#{1,3}\s*摘要\s*$", re.MULTILINE),
    "one_line_summary": re.compile(r"^#{1,3}\s*一句话总结\s*$", re.MULTILINE),
    "keywords": re.compile(r"^#{1,3}\s*关键词\s*$", re.MULTILINE),
    "tags": re.compile(r"^#{1,3}\s*标签\s*$", re.MULTILINE),
}
```

替换为：

```python
# Issue #56 场景 B: 放宽标题格式 —— 允许行尾 [:：] 和加粗 **...**。
# 常见 LLM 不严格输出：'## 摘要：' / '## 摘要:' / '## **摘要**' / '## **摘要**：'。
# one_line_summary 同时接受 '## 总结' 同义词（向后兼容旧 prompt 的「## 一句话总结」）。
_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"^#{1,3}\s*\**摘要\**\s*[:：]?\s*$", re.MULTILINE),
    "one_line_summary": re.compile(r"^#{1,3}\s*\**(一句话总结|总结)\**\s*[:：]?\s*$", re.MULTILINE),
    "keywords": re.compile(r"^#{1,3}\s*\**关键词\**\s*[:：]?\s*$", re.MULTILINE),
    "tags": re.compile(r"^#{1,3}\s*\**标签\**\s*[:：]?\s*$", re.MULTILINE),
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_summarizer.py::TestParseMarkdownAnalysis -v -k "colon or bold or synonym"`

Expected: 5 个新测试 PASS。

- [ ] **Step 5: 跑全量 parse 测试，确认无回归（特别是粗体保留测试）**

Run: `uv run pytest tests/test_summarizer.py::TestParseMarkdownAnalysis -v`

Expected: 全部 PASS，包括关键的 `test_parse_preserves_bold_inside_summary`（line 271-280，字段内部 `**粗体**` 原样保留）。

- [ ] **Step 6: Commit**

```bash
git add core/summarizer.py tests/test_summarizer.py
git commit -m "fix(summarizer): relax section patterns to tolerate trailing colon/bold titles (issue #56)"
```

---

## Task 4: parse_markdown_analysis 写入 raw 字段

**Files:**
- Modify: `core/summarizer.py:124-138`（`parse_markdown_analysis`）
- Modify: `core/summarizer.py:281-320`（`analyze_content`）
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: 写失败测试 — parse_markdown_analysis 填充 raw**

在 `tests/test_summarizer.py::TestParseMarkdownAnalysis` 类末尾追加：

```python
    def test_parse_result_has_raw_field_populated(self) -> None:
        """Issue #56: parse_markdown_analysis 应把原始输入赋给 result.raw。"""
        raw = """## 摘要
内容

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert result.raw == raw
```

并在 `tests/test_summarizer.py::TestAnalyzeContent` 类末尾追加：

```python
    @pytest.mark.asyncio
    async def test_analyze_content_populates_raw_field(self) -> None:
        """Issue #56: analyze_content 成功路径应填充 result.raw 供 handler 观测。"""
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"
        config.analysis.api_key = "k"

        ai_output = "## 摘要\n这是 raw 字段测试\n\n## 一句话总结\nok"

        with patch("core.summarizer.create_provider") as mock_cp:
            provider = mock_cp.return_value
            provider.generate = AsyncMock(return_value=ai_output)

            result = await analyze_content(
                source_id="bili:BV1",
                title="T",
                author="A",
                text="正文",
                config=config,
            )

        assert result.raw == ai_output
        assert "raw 字段测试" in result.raw
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_summarizer.py::TestParseMarkdownAnalysis::test_parse_result_has_raw_field_populated tests/test_summarizer.py::TestAnalyzeContent::test_analyze_content_populates_raw_field -v`

Expected: 2 个 FAIL（`result.raw` 是 `""` 默认值，未被赋值）。

- [ ] **Step 3: 修改 parse_markdown_analysis 填充 raw**

修改 `core/summarizer.py:124-138`，将：

```python
def parse_markdown_analysis(raw: str) -> AnalysisResult:
    """将 AI 输出的 Markdown 解析为 ``AnalysisResult``。

    鲁棒性：
    - 自动剥离 ```markdown fence
    - 缺失字段填空值
    - 关键词/标签用混合分隔符拆分
    """
    text = _strip_code_fence(raw)
    return AnalysisResult(
        summary=_extract_section(text, _SECTION_PATTERNS["summary"]),
        one_line_summary=_extract_section(text, _SECTION_PATTERNS["one_line_summary"]),
        keywords=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["keywords"]))[:5],
        tags=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["tags"]))[:3],
    )
```

替换为：

```python
def parse_markdown_analysis(raw: str) -> AnalysisResult:
    """将 AI 输出的 Markdown 解析为 ``AnalysisResult``。

    鲁棒性：
    - 自动剥离 ```markdown fence
    - 缺失字段填空值
    - 关键词/标签用混合分隔符拆分
    - Issue #56: 保留原始 raw 文本到 ``result.raw``，供 handler 在解析为空时观测。
    """
    text = _strip_code_fence(raw)
    return AnalysisResult(
        summary=_extract_section(text, _SECTION_PATTERNS["summary"]),
        one_line_summary=_extract_section(text, _SECTION_PATTERNS["one_line_summary"]),
        keywords=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["keywords"]))[:5],
        tags=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["tags"]))[:3],
        raw=raw,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_summarizer.py::TestParseMarkdownAnalysis::test_parse_result_has_raw_field_populated tests/test_summarizer.py::TestAnalyzeContent::test_analyze_content_populates_raw_field -v`

Expected: 2 个 PASS。

- [ ] **Step 5: 跑全量测试确认无回归**

Run: `uv run pytest tests/test_summarizer.py -v`

Expected: 全部 PASS（注意 `test_analyze_content_ai_failure_returns_empty_and_warns` 等失败路径测试 —— `analyze_content` 的 `except` 分支返回的 `AnalysisResult(source="none", failed=True)` 不显式设 raw，仍为默认 `""`，符合预期）。

- [ ] **Step 6: Commit**

```bash
git add core/summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): parse_markdown_analysis populates result.raw (issue #56)"
```

---

## Task 5: summarize_phase handler 加 warning 观测

**Files:**
- Modify: `platforms/bilibili/handlers.py:259-285`（`summarize_phase`）
- Create: `tests/test_handlers_summarize_phase.py`

- [ ] **Step 1: 写失败测试 — 解析为空时打 warning**

新建 `tests/test_handlers_summarize_phase.py`：

```python
"""Tests for platforms.bilibili.handlers.summarize_phase — Issue #56 silent empty 观测。

独立文件：避免污染 tests/test_summarizer.py 的 458 行结构，
summarize_phase 是 cross-platform handler（@register('*', Phase.SUMMARIZED)），

重点验证：解析成功但 summary 为空时，handler 必须打 warning 让运维可见，
同时仍返回 True（消息继续推进，避免重试爆炸）。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.summarizer import AnalysisResult
from platforms.bilibili.handlers import summarize_phase
from shared.config import Config
from shared.protocols import ContentType, MessageRecord, Phase, PhaseContext


def _make_video_ctx() -> PhaseContext:
    """构造一个最小可用的 VIDEO PhaseContext（B站视频，已 TRANSCRIBED）。"""
    msg = MessageRecord(
        msg_id="bili:BV1test",
        platform="bili",
        content_type=ContentType.VIDEO,
        phase=Phase.TRANSCRIBED,
        pubdate=0,
        title="测试视频",
        author="测试UP",
    )
    ctx = PhaseContext(msg=msg, config=Config())
    ctx.transcript_text = "这是测试用的转写正文"
    return ctx


class TestSummarizePhaseEmptyWarning:
    """Issue #56: 解析成功但 summary 为空时打 warning，不阻塞流水线。"""

    @pytest.mark.asyncio
    async def test_warns_on_empty_summary_but_returns_true(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = _make_video_ctx()
        empty_analysis = AnalysisResult(
            summary="",
            keywords=[],
            is_ai=True,
            source="openai",
            failed=False,
            raw="## 摘要\n\n## 关键词\n",
        )

        with patch("platforms.bilibili.handlers.analyze_content", new=AsyncMock(return_value=empty_analysis)):
            with patch("platforms.bilibili.handlers.fetch_comment_highlights", new=AsyncMock(return_value=[])):
                with caplog.at_level(logging.WARNING, logger="platforms.bilibili.handlers"):
                    result = await summarize_phase(ctx)

        # 接受空 summary，仍返回 True（避免重试爆炸）
        assert result is True
        # ctx.summary_text 为空字符串
        assert ctx.summary_text == ""
        # 必须打 warning 让运维可见
        assert any(
            "AI 摘要解析为空" in r.getMessage() for r in caplog.records
        ), f"未找到 silent empty warning，实际日志: {[r.getMessage() for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_no_warning_when_summary_non_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """summary 非空时不应打 silent empty warning（回归保护）。"""
        ctx = _make_video_ctx()
        good_analysis = AnalysisResult(
            summary="这是有效的摘要内容",
            keywords=["关键词"],
            is_ai=True,
            source="openai",
            failed=False,
            raw="## 摘要\n这是有效的摘要内容\n\n## 关键词\n关键词",
        )

        with patch("platforms.bilibili.handlers.analyze_content", new=AsyncMock(return_value=good_analysis)):
            with patch("platforms.bilibili.handlers.fetch_comment_highlights", new=AsyncMock(return_value=[])):
                with caplog.at_level(logging.WARNING, logger="platforms.bilibili.handlers"):
                    result = await summarize_phase(ctx)

        assert result is True
        assert ctx.summary_text == "这是有效的摘要内容"
        assert not any(
            "AI 摘要解析为空" in r.getMessage() for r in caplog.records
        ), "summary 非空时不应打 silent empty warning"

    @pytest.mark.asyncio
    async def test_failed_analysis_still_returns_false(self) -> None:
        """failed=True 时 handler 返回 False（保持原有 retry 行为，warning 已有）。"""
        ctx = _make_video_ctx()
        failed_analysis = AnalysisResult(
            summary="",
            keywords=[],
            is_ai=False,
            source="none",
            failed=True,
            raw="",
        )

        with patch("platforms.bilibili.handlers.analyze_content", new=AsyncMock(return_value=failed_analysis)):
            with patch("platforms.bilibili.handlers.fetch_comment_highlights", new=AsyncMock(return_value=[])):
                result = await summarize_phase(ctx)

        assert result is False
        assert ctx.error  # 必须设置 error 让 engine 走 retry 路径
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_handlers_summarize_phase.py -v`

Expected: `test_warns_on_empty_summary_but_returns_true` FAIL（当前 handler 不打 silent empty warning）。其他可能 PASS。

- [ ] **Step 3: 修改 summarize_phase 加 warning**

修改 `platforms/bilibili/handlers.py:282-285`，将：

```python
    ctx.summary_text = analysis.summary
    ctx.keywords = analysis.keywords

    return True
```

替换为：

```python
    # Issue #56: 解析成功（HTTP 200 + 无异常）但 summary 为空 —— silent empty。
    # 接受空 summary 继续推进（避免重试爆炸），但必须打 warning 让运维可见。
    # core/engine.py:49 的 `if ctx.summary_text:` 守卫会确保空 summary 不落 messages.json，
    # 这是预期行为，handler 只负责让"摘要丢失"这件事可观测。
    if not analysis.summary:
        logger.warning(
            "⚠️  AI 摘要解析为空（source_id=%s, source=%s, raw 长度=%d）— 检查 LLM 输出格式或 reasoning_content 兜底",
            source_id,
            analysis.source,
            len(analysis.raw),
        )
    ctx.summary_text = analysis.summary
    ctx.keywords = analysis.keywords

    return True
```

> **注意 emoji 颜色标签：** 使用 `⚠️ ` 前缀（黄色 emoji）与现有 `logger.warning("⚠️  评论获取失败: ...")` (handlers.py:231) 风格一致，符合 AGENTS.md "不改 emoji" 约束。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_handlers_summarize_phase.py -v`

Expected: 3 个测试全部 PASS。

- [ ] **Step 5: 跑全量测试确认无回归**

Run: `uv run pytest -x`

Expected: 全部 PASS（如有 bili pipeline 集成测试 mock 了 summarize_phase，不应受影响 —— 此改动只增 warning 不改控制流）。

- [ ] **Step 6: Commit**

```bash
git add platforms/bilibili/handlers.py tests/test_handlers_summarize_phase.py
git commit -m "fix(handlers): warn on silent empty AI summary to make it observable (issue #56)"
```

---

## Task 6: 全量验证 + push

**Files:** 全部修改过的文件

- [ ] **Step 1: ruff lint**

Run: `uv run ruff check .`

Expected: 无新增 lint error。

- [ ] **Step 2: ruff format（如有格式问题）**

Run: `uv run ruff format core/summarizer.py platforms/bilibili/handlers.py tests/test_summarizer.py tests/test_handlers_summarize_phase.py`

Expected: 文件已格式化或无变化。

- [ ] **Step 3: pyright 类型检查**

Run: `uv run pyright`

Expected: 无新增 type error（**注意：不要加 `.` 参数，避免扫描 `.venv/`**）。

- [ ] **Step 4: 全量 pytest**

Run: `uv run pytest -x`

Expected: 全部 PASS。

- [ ] **Step 5: 检查 git 状态**

Run: `git status && git log --oneline -10`

Expected: 5 个 commit（Task 1-5），工作区干净。

- [ ] **Step 6: Push 分支**

Run: `git push -u origin fix/issue-54-56-ai-summary-resilience`

Expected: 推送成功。

- [ ] **Step 7: 创建 PR**

```bash
gh pr create \
  --base master \
  --head fix/issue-54-56-ai-summary-resilience \
  --title "fix: issue #56 - AI 摘要解析 silent empty 修复" \
  --body "## 修复内容

修复 #56 (silent empty)：AI 返回空 content / 标题格式不严格时摘要静默丢失。

## 改动
- \`OpenAIProvider.generate\`：content 空时 fallback 到 \`reasoning_content\`；扩展 except 捕获 AttributeError/TypeError；加 DEBUG 日志
- \`_SECTION_PATTERNS\`：放宽 \`^#{1,3}\\s*\\**摘要\\**\\s*[:：]?\\s*$\`，兼容尾随冒号 + 加粗标题；\`one_line_summary\` 接受「总结」同义词
- \`AnalysisResult.raw\`：新增字段记录原始 LLM 响应，便于排查
- \`parse_markdown_analysis\`：填充 \`result.raw\`
- \`summarize_phase\`：解析为空时打 warning（接受空 + warning，不改 \`engine.py\` 守卫）

## 测试
新增 12 个测试用例：
- 5 个 provider 响应解析测试（reasoning fallback / None content / 正常路径回归 / 空 content / 缺失 choices）
- 5 个 parse 正则放宽测试（全角冒号 / 半角冒号 / 加粗标题 / 加粗+冒号 / 总结同义词）
- 2 个 raw 字段测试（parse / analyze_content）
- 3 个 handler warning 测试（独立文件 \`test_handlers_summarize_phase.py\`）

## 不改的部分
- \`core/engine.py:49\` 的 \`if ctx.summary_text:\` 守卫（有意保护，空 summary 不落盘是预期行为）
- 字段内 \`**粗体**\` 不去除（决策 7：现有测试 \`test_parse_preserves_bold_inside_summary\` 锁定原样保留）

## 关联
- 不包含 issue #54 改动（#54 是 prompt 约束，会在另一个 PR 做，依赖此 PR 的解析层改动）
- 容器跑的代码版本：\`master @ c82db29\`（2026-06-30 22:29 构建）"
```

Expected: PR 创建成功，返回 PR URL。

- [ ] **Step 8: 等待 CI + Qodo review**

创建 PR 后等待 5-15 分钟让 Qodo 完成 review，按全局 AGENTS.md 的 PR review 轮询流程处理（每 3 分钟 `gh pr view <PR> --comments`，连续 2 次无新评论视为完成）。

---

## 验收清单

| 验收项 | 验证方式 | 预期结果 |
|---|---|---|
| reasoning_content fallback | `test_provider_fallback_to_reasoning_content` | PASS |
| None content 不抛 AttributeError | `test_provider_handles_none_content` | PASS |
| 尾随冒号解析 | `test_parse_summary_with_colon_suffix` + `_with_half_width_colon` | PASS |
| 加粗标题解析 | `test_parse_summary_with_bold_title` + `_with_bold_title_and_colon` | PASS |
| 总结同义词 | `test_parse_one_line_summary_accepts_总结_synonym` | PASS |
| raw 字段观测 | `test_parse_result_has_raw_field_populated` + `test_analyze_content_populates_raw_field` | PASS |
| handler warning | `test_warns_on_empty_summary_but_returns_true` | PASS |
| 粗体保留（不破坏） | `test_parse_preserves_bold_inside_summary`（原有） | PASS |
| 全量测试 | `uv run pytest -x` | 全部 PASS |
| 类型检查 | `uv run pyright` | 0 error |
| Lint | `uv run ruff check .` | 无新增 |

## 手动验收（服务端，PR 合入后）

```bash
# 容器内重跑受影响的 video 消息
trawler check --reset-phase summarized
# 对比 messages.json，确认 bili:BV1KdKo69EV4 / bili:BV1mXKo6REw3 的 summary 字段补回
# 用 LOG_LEVEL=DEBUG 重跑可以看 LLM raw response
```
