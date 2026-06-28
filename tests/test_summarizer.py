"""Tests for core.summarizer — provider factory, parser, and analyze_content."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.summarizer import (
    OpenAIProvider,
    analyze_content,
    create_provider,
    parse_markdown_analysis,
)
from shared.config import AnalysisConfig, Config
from shared.protocols import MessageRecord


class TestCreateProvider:
    """Tests for the create_provider factory function."""

    def test_create_provider_openai(self) -> None:
        config = AnalysisConfig(
            provider="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
            model_name="gpt-4o-mini",
        )
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_base == "https://api.openai.com/v1"
        assert provider.api_key == "sk-x"
        assert provider.model_name == "gpt-4o-mini"

    def test_create_provider_openai_missing_api_base_raises(self) -> None:
        config = AnalysisConfig(
            provider="openai",
            api_base="",
            api_key="sk-x",
        )
        with pytest.raises(ValueError):
            create_provider(config)

    def test_create_provider_ollama_default_api_base(self) -> None:
        config = AnalysisConfig(provider="ollama", api_base="")
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_base == "http://localhost:11434/v1"

    def test_create_provider_ollama_custom_api_base(self) -> None:
        config = AnalysisConfig(
            provider="ollama",
            api_base="http://my-host:11434/v1",
        )
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_base == "http://my-host:11434/v1"

    def test_create_provider_unknown_raises(self) -> None:
        config = AnalysisConfig(provider="foobar")
        with pytest.raises(ValueError, match="不支持的 provider"):
            create_provider(config)

    def test_create_provider_codebuddy_removed(self) -> None:
        config = AnalysisConfig(provider="codebuddy")
        with pytest.raises(ValueError):
            create_provider(config)

    def test_create_provider_case_insensitive(self) -> None:
        config = AnalysisConfig(
            provider="OpenAI",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
        )
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)


class TestDataModelDefaults:
    """新加字段的默认值测试。"""

    def test_analysis_result_has_failed_default_false(self) -> None:
        from core.summarizer import AnalysisResult

        r = AnalysisResult()
        assert r.failed is False

    def test_phase_context_has_permanent_error_default_false(self) -> None:
        """Issue 6: PhaseContext.permanent_error 默认 False（保持现有 retry 行为）。"""
        from shared.protocols import PhaseContext

        ctx = PhaseContext(
            msg=MessageRecord(
                msg_id="x",
                platform="bili",
                content_type="video",
                phase="discovered",
                pubdate=0,
                title="t",
                author="a",
            ),
            config=Config(),
        )
        assert ctx.permanent_error is False


class TestParseMarkdownAnalysis:
    """Tests for parse_markdown_analysis — robust parsing of AI output."""

    def test_parse_well_formed(self) -> None:
        raw = """## 摘要
1. 第一点
2. 第二点

## 一句话总结
这是个测试视频

## 关键词
Python；异步；测试

## 标签
教程, 评测"""
        result = parse_markdown_analysis(raw)
        assert "第一点" in result.summary
        assert "第二点" in result.summary
        assert result.one_line_summary == "这是个测试视频"
        assert result.keywords == ["Python", "异步", "测试"]
        assert result.tags == ["教程", "评测"]

    def test_parse_strips_markdown_code_fence(self) -> None:
        raw = """```markdown
## 摘要
内容

## 一句话总结
一句话

## 关键词
A；B

## 标签
```"""
        result = parse_markdown_analysis(raw)
        assert "内容" in result.summary
        assert result.keywords == ["A", "B"]
        assert result.tags == []

    def test_parse_missing_section_returns_empty(self) -> None:
        raw = """## 摘要
只有摘要

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "只有摘要" in result.summary
        assert result.one_line_summary == ""
        assert result.keywords == ["A"]
        assert result.tags == []

    def test_parse_keywords_with_mixed_separators(self) -> None:
        raw = """## 摘要
x

## 关键词
A；B,C；D
"""
        result = parse_markdown_analysis(raw)
        assert result.keywords == ["A", "B", "C", "D"]

    def test_parse_preserves_bold_inside_summary(self) -> None:
        """LLM 偶尔不遵守 plain 约束返回 **bold**，解析层应容忍并原样保留。
        渲染层（plain text）将原样透传，不再尝试去除 markdown。"""
        raw = """## 摘要
这是 **粗体** 测试

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "**粗体**" in result.summary  # 原样保留，渲染时 plain 端只是显示字面量


class TestAnalyzeContent:
    """Tests for analyze_content — AI orchestration + failure semantics."""

    @pytest.mark.asyncio
    async def test_analyze_content_success_caches_on_result(self) -> None:
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"
        config.analysis.api_key = "k"

        ai_output = """## 摘要
1. 要点一

## 一句话总结
一句话

## 关键词
A；B

## 标签
教程"""

        with patch("core.summarizer.create_provider") as mock_cp:
            provider = mock_cp.return_value
            provider.generate = AsyncMock(return_value=ai_output)

            result = await analyze_content(
                source_id="bili:BV1",
                title="T",
                author="A",
                text="正文内容",
                config=config,
            )

        assert "要点一" in result.summary
        assert result.keywords == ["A", "B"]
        assert result.is_ai is True

    @pytest.mark.asyncio
    async def test_analyze_content_ai_failure_returns_empty_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        with patch("core.summarizer.create_provider") as mock_cp:
            provider = mock_cp.return_value
            provider.generate = AsyncMock(side_effect=RuntimeError("API timeout"))

            with caplog.at_level("WARNING", logger="core.summarizer"):
                result = await analyze_content(
                    source_id="bili:BV1",
                    title="T",
                    author="A",
                    text="正文",
                    config=config,
                )

        assert result.summary == ""
        assert result.keywords == []
        assert result.is_ai is False
        # Bug 2 requirement: failure logged at WARNING (not DEBUG)
        assert any("AI 内容分析失败" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_analyze_content_disabled_ai_returns_empty(self) -> None:
        config = Config()
        config.analysis.enabled = False

        result = await analyze_content(source_id="x", title="t", author="a", text="txt", config=config)
        assert result.summary == ""
        assert result.keywords == []
        assert result.is_ai is False

    @pytest.mark.asyncio
    async def test_analyze_content_empty_text_returns_empty(self) -> None:
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        result = await analyze_content(source_id="x", title="t", author="a", text="", config=config)
        assert result.summary == ""
        assert result.is_ai is False


class TestLegacyWrappersReturnEmptyOnFailure:
    """generate_summary and extract_keywords must keep their old signatures
    but now delegate to analyze_content. On AI failure they return empty
    values (not n-gram fallback)."""

    @pytest.mark.asyncio
    async def test_generate_summary_failure_returns_empty(self) -> None:
        from core.summarizer import generate_summary

        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        with patch("core.summarizer.create_provider") as mock_cp:
            mock_cp.return_value.generate = AsyncMock(side_effect=RuntimeError("fail"))
            summary, source, is_ai = await generate_summary(
                source_id="x", title="t", author="a", text="正文", config=config
            )
        assert summary == ""
        assert is_ai is False

    @pytest.mark.asyncio
    async def test_extract_keywords_failure_returns_empty_list(self) -> None:
        from core.summarizer import extract_keywords

        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        with patch("core.summarizer.create_provider") as mock_cp:
            mock_cp.return_value.generate = AsyncMock(side_effect=RuntimeError("fail"))
            kws = await extract_keywords(text="正文", title="t", author="a", config=config)
        assert kws == []
