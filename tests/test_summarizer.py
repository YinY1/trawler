"""Tests for core.summarizer — provider factory, parser, and analyze_content."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.summarizer import (
    AnalysisResult,
    OpenAIProvider,
    analyze_content,
    create_provider,
    parse_markdown_analysis,
)
from shared.config import AnalysisConfig, Config, LLMProviderConfig
from shared.protocols import MessageRecord


class TestCreateProvider:
    """Tests for create_provider — now returns FallbackChainProvider."""

    def test_create_provider_openai_returns_chain_with_openai_inside(self) -> None:
        from core.summarizer import FallbackChainProvider

        config = AnalysisConfig(
            provider="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
            model_name="gpt-4o-mini",
        )
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)
        assert len(chain._providers) == 1
        assert isinstance(chain._providers[0], OpenAIProvider)
        assert chain._providers[0].api_base == "https://api.openai.com/v1"
        assert chain._providers[0].api_key == "sk-x"
        assert chain._providers[0].model_name == "gpt-4o-mini"

    def test_create_provider_openai_missing_api_base_raises(self) -> None:
        config = AnalysisConfig(
            provider="openai",
            api_base="",
            api_key="sk-x",
        )
        with pytest.raises(ValueError):
            create_provider(config)

    def test_create_provider_ollama_default_api_base(self) -> None:
        """ollama 默认 api_base 注入：providers_chain 跳过 api_base='' 的主 provider
        （plan Issue 4 退化修复），所以测试 ollama 默认 URL 注入必须显式指定 api_base
        让主 provider 进入链，再由 _build_single_provider 处理默认 ollama URL。
        这里直接测 _build_single_provider 的默认 URL 注入逻辑。"""
        from core.summarizer import _build_single_provider

        provider = _build_single_provider(LLMProviderConfig(provider="ollama", api_base=""))
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_base == "http://localhost:11434/v1"

    def test_create_provider_ollama_custom_api_base(self) -> None:
        from core.summarizer import FallbackChainProvider

        config = AnalysisConfig(
            provider="ollama",
            api_base="http://my-host:11434/v1",
        )
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)
        assert chain._providers[0].api_base == "http://my-host:11434/v1"

    def test_create_provider_unknown_raises(self) -> None:
        # api_base 必须非空才能进入 _build_single_provider 的 provider 类型检查
        config = AnalysisConfig(provider="foobar", api_base="https://x")
        with pytest.raises(ValueError, match="不支持的 provider"):
            create_provider(config)

    def test_create_provider_codebuddy_removed(self) -> None:
        config = AnalysisConfig(provider="codebuddy", api_base="https://x")
        with pytest.raises(ValueError):
            create_provider(config)

    def test_create_provider_case_insensitive(self) -> None:
        from core.summarizer import FallbackChainProvider

        config = AnalysisConfig(
            provider="OpenAI",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
        )
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)


class TestFallbackChainProvider:
    """Tests for FallbackChainProvider — 按序尝试，前一个失败才 fallback。"""

    @pytest.mark.asyncio
    async def test_first_provider_success_no_fallback(self) -> None:
        from core.summarizer import FallbackChainProvider

        primary = AsyncMock()
        primary.generate = AsyncMock(return_value="primary response")
        secondary = AsyncMock()
        secondary.generate = AsyncMock(return_value="secondary response")

        chain = FallbackChainProvider(providers=[primary, secondary])
        result = await chain.generate("ping")
        assert result == "primary response"
        primary.generate.assert_awaited_once()
        secondary.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_fail_second_success(self) -> None:
        from core.summarizer import FallbackChainProvider

        primary = AsyncMock()
        primary.generate = AsyncMock(side_effect=RuntimeError("401 unauthorized"))
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
        # 每个 provider 的失败都要被记录。断言用 r.getMessage()（issue N7），
        # 因为 r.message 是格式化前的模板字符串。
        fail_logs = [r for r in caplog.records if "provider #" in r.getMessage() or "provider 失败" in r.getMessage()]
        assert len(fail_logs) >= 3

    def test_empty_chain_raises_value_error(self) -> None:
        """空 providers 列表在构造时抛 ValueError（修正 N3：构造时检查，非 generate 时）。"""
        from core.summarizer import FallbackChainProvider

        with pytest.raises(ValueError, match="providers 列表不能为空"):
            FallbackChainProvider(providers=[])


class TestCreateProviderChain:
    """Tests for create_provider — 现在返回 FallbackChainProvider。"""

    def test_create_provider_single_returns_chain_with_one(self) -> None:
        from core.summarizer import FallbackChainProvider

        config = AnalysisConfig(provider="openai", api_base="https://x", api_key="k")
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)
        assert len(chain._providers) == 1

    def test_create_provider_multiple_returns_chain_with_all(self) -> None:
        from core.summarizer import FallbackChainProvider

        config = AnalysisConfig(provider="openai", api_base="https://x", api_key="k")
        config.extra_providers = [
            LLMProviderConfig(provider="ollama", api_base="http://l:11434/v1"),
        ]
        chain = create_provider(config)
        assert isinstance(chain, FallbackChainProvider)
        assert len(chain._providers) == 2


class TestDataModelDefaults:
    """新加字段的默认值测试。"""

    def test_analysis_result_has_failed_default_false(self) -> None:
        r = AnalysisResult()
        assert r.failed is False

    def test_analysis_result_has_raw_default_empty_string(self) -> None:
        """Issue #56: AnalysisResult.raw 记录原始 LLM 响应，便于排查 silent empty。"""
        r = AnalysisResult()
        assert r.raw == ""
        assert isinstance(r.raw, str)

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

    def test_parse_one_line_summary_accepts_summary_synonym(self) -> None:
        """Issue #56: LLM 输出 '## 总结' 应作为 one_line_summary 解析（同义词兼容）。"""
        raw = """## 摘要
摘要正文

## 总结
这是同义词的一句话总结

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "同义词的一句话总结" in result.one_line_summary

    def test_parse_result_has_raw_field_populated(self) -> None:
        """Issue #56: parse_markdown_analysis 应把原始输入赋给 result.raw。"""
        raw = """## 摘要
内容

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert result.raw == raw

    def test_parse_long_summary_with_numbered_keypoints(self) -> None:
        """Issue #54 回归: prompt 改完后 LLM 输出 400+ 字 + 3-8 条序号要点，
        解析层必须完整保留，不被「## 关键词」提前截断或被「1. 2. 」序号干扰。"""
        # 构造一个 400+ 字、5 条要点的摘要（模拟新 prompt 输出）
        long_summary = (
            "1. 视频开篇作者引用了一组关键数据：2024 年中国短视频用户规模达到 9.8 亿，"
            "占总网民的 87.5%，相比 2022 年增长了 12 个百分点。"
            "这组数据来源于中国互联网络信息中心发布的第 53 次统计报告，"
            "作者特别强调下沉市场（三线及以下城市）贡献了增量的 68%。\n"
            "2. 第二个论点围绕算法推荐机制展开，作者以抖音的协同过滤为例，"
            "解释了「信息茧房」效应如何在 6 个月内形成。"
            "他引用了一位北大新闻传播学院教授的访谈，"
            "指出每天刷 90 分钟短视频的用户，"
            "接触异质观点的概率会从最初的 35% 下降到 8% 左右。\n"
            "3. 第三个案例是某三线城市 UP 主通过分析后台数据，"
            "在 3 个月内将完播率从 22% 提升到 47% 的实操路径。"
            "具体方法包括：前 3 秒设置悬念、每 15 秒插入信息增量、"
            "片尾用开放式提问引导评论，这三步让互动率同步上涨 3 倍。\n"
            "4. 时间线梳理：2023Q1 政策收紧 → 2023Q3 平台调整 → 2024Q2 创作者生态反弹，"
            "整个周期约 18 个月。作者特别提到 2023 年 8 月的「清朗行动」"
            "导致 12% 的中腰部账号停更，但同期 MCN 机构数量反而增长了 9%。\n"
            "5. 最后作者提出三个开放性问题，邀请观众在评论区讨论，"
            "并引用了《注意力经济》一书的观点作为收尾。"
            "他呼吁平台方公开推荐权重的可解释性指标，"
            "同时建议创作者建立独立邮件列表降低对算法分发的依赖。"
        )
        assert len(long_summary) > 400  # 满足新 prompt 的字数下限

        raw = f"""## 摘要
{long_summary}

## 一句话总结
新 prompt 下产出的长摘要解析回归测试

## 关键词
短视频；算法推荐；信息茧房

## 标签
教程, 评测"""

        result = parse_markdown_analysis(raw)

        # 全部 5 条要点必须完整保留（不被截断）
        assert "9.8 亿" in result.summary
        assert "协同过滤" in result.summary
        assert "三线城市 UP 主" in result.summary
        assert "2023Q1" in result.summary
        assert "《注意力经济》" in result.summary
        # 序号格式保留
        assert "1." in result.summary
        assert "5." in result.summary
        # 总长度仍 > 400（未被截断）
        assert len(result.summary) > 400

    def test_parse_summary_with_bold_numbered_keypoints_pr54(self) -> None:
        """Issue #54 回归: LLM 偶尔会用「**1.** **要点标题**：内容」格式，
        解析层（PR-1 已放宽正则兼容加粗标题）必须原样保留字段内容。"""
        raw = """## 摘要
**1.** **核心观点**：作者认为算法推荐已经改变了内容创作的底层逻辑
**2.** **关键数据**：调研样本量 N=12000，置信度 95%

## 关键词
算法"""
        result = parse_markdown_analysis(raw)
        # 决策 7：字段内 **粗体** 原样保留（不剥离）
        assert "**核心观点**" in result.summary
        assert "**关键数据**" in result.summary
        assert "N=12000" in result.summary


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
        # 用 r.getMessage() 而非 r.message（issue N7：r.message 是格式化前的模板）
        assert any("AI 内容分析失败" in r.getMessage() for r in caplog.records)

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

    @pytest.mark.asyncio
    async def test_analyze_content_all_providers_fail_sets_failed_true(self, caplog: pytest.LogCaptureFixture) -> None:
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
                    source_id="bili:BV1",
                    title="T",
                    author="A",
                    text="正文",
                    config=config,
                )

        assert result.failed is True
        assert result.is_ai is False
        assert result.summary == ""  # 失败时字段为空
        # 用 r.getMessage()（issue N7）
        assert any("AI 内容分析失败" in r.getMessage() for r in caplog.records)

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

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
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

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            result = await provider.generate("test prompt")

        assert "fallback 答案" in result

    @pytest.mark.asyncio
    async def test_provider_whitespace_only_content_falls_back_to_reasoning(self) -> None:
        """Issue #56: whitespace-only content（'   ' / '\\n'）是 truthy 字符串，
        仅判 falsy 会漏过最终 silent empty。判 .strip() 让空白 content fallback 到 reasoning_content。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": "   \n  ",
                        "reasoning_content": "## 摘要\n空白 content fallback 答案",
                    }
                }
            ]
        }

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            result = await provider.generate("test prompt")

        assert "空白 content fallback 答案" in result

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

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
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

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
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

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = fake_response_json

            with pytest.raises(RuntimeError, match="解析 API 响应失败"):
                await provider.generate("test prompt")


class TestPromptTemplateConstraints:
    """Issue #54: _ANALYSIS_PROMPT_TEMPLATE 必须含字数下限 + 要点数量下限 + 序号格式硬约束，
    让 LLM 不再过度压缩长视频/长文摘要。"""

    def test_prompt_contains_word_count_lower_bound(self) -> None:
        """prompt 模板必须包含「400 字」字数下限约束。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "400" in _ANALYSIS_PROMPT_TEMPLATE, "prompt 必须含 400 字下限"
        assert "字" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_contains_word_count_upper_bound(self) -> None:
        """prompt 模板必须包含「1200 字」上限（避免 LLM 输出过长触发 max_tokens 截断）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "1200" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_contains_keypoints_count_lower_bound(self) -> None:
        """prompt 必须含「3-8 条要点」数量约束。

        断言强化（issue #54 review）：去掉宽松 OR 后半段（`"3" in ... and "8" in ...`，
        模板里 3 和 8 任意出处都会假 PASS），只接受紧邻的「3-8」字面约束。
        """
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "3-8" in _ANALYSIS_PROMPT_TEMPLATE, "prompt 必须含「3-8 条要点」数量约束"

    def test_prompt_requires_numbered_list_format(self) -> None:
        """prompt 必须要求用「1. 」「2. 」中文序号格式表达要点。

        断言强化（issue #54 review）：不能只看模板任意位置是否有 "1."，
        要定位到 `## 摘要` 段说明区（## 摘要 之后、## 一句话总结 之前），
        断言该子串同时含「1. 」和「3. 」序号约束。
        """
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        # 定位 `## 摘要\n` 之后到 `## 一句话总结\n` 之前的子串
        # （两个 find 都带 \n，避免匹配到 prompt 第一段说明区的标题引用）
        summary_start = _ANALYSIS_PROMPT_TEMPLATE.find("## 摘要\n")
        next_section = _ANALYSIS_PROMPT_TEMPLATE.find("## 一句话总结\n")
        assert summary_start != -1, "prompt 必须含 `## 摘要` 段标题"
        assert next_section != -1, "prompt 必须含 `## 一句话总结` 段标题"
        summary_block = _ANALYSIS_PROMPT_TEMPLATE[summary_start:next_section]
        assert "1. " in summary_block, "## 摘要 段必须含「1. 」序号约束"
        assert "3. " in summary_block, "## 摘要 段必须含「3. 」序号约束（要求至少 3 条要点）"

    def test_prompt_requires_concrete_info_per_point(self) -> None:
        """prompt 必须要求每条要点含具体信息（数据/案例/时间/论据），不只复述标题。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        # 至少要提到具体信息的某一种
        assert any(
            kw in _ANALYSIS_PROMPT_TEMPLATE
            for kw in ("数据", "案例", "时间", "论据", "人名", "引用")
        ), "prompt 必须要求每条要点含具体信息"

    def test_prompt_still_has_four_sections(self) -> None:
        """回归保护：prompt 仍然包含 4 个标准字段标题（解析层依赖）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        for section in ("## 摘要", "## 一句话总结", "## 关键词", "## 标签"):
            assert section in _ANALYSIS_PROMPT_TEMPLATE, f"prompt 必须保留 {section} 字段标题"

    def test_prompt_keeps_placeholder_format(self) -> None:
        """回归保护：prompt 模板必须保留 {title}/{author}/{text} 占位符供 str.format 调用。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "{title}" in _ANALYSIS_PROMPT_TEMPLATE
        assert "{author}" in _ANALYSIS_PROMPT_TEMPLATE
        assert "{text}" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_format_succeeds(self) -> None:
        """prompt 模板能被 str.format 正常渲染（验证无 stray brace 导致 KeyError）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        rendered = _ANALYSIS_PROMPT_TEMPLATE.format(title="T", author="A", text="正文")
        assert "T" in rendered
        assert "A" in rendered
        assert "正文" in rendered
