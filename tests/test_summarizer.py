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
    async def test_last_provider_name_reflects_hit(self) -> None:
        """Issue #61: generate 成功后 last_provider_name 反映命中的 provider 标识。"""
        from core.summarizer import FallbackChainProvider

        primary = AsyncMock()
        primary.generate = AsyncMock(side_effect=RuntimeError("401"))
        secondary = AsyncMock()
        secondary.generate = AsyncMock(return_value="ok")

        chain = FallbackChainProvider(
            providers=[primary, secondary],
            provider_names=["openai", "ollama"],
        )
        # 初始 None
        assert chain.last_provider_name is None
        await chain.generate("ping")
        # 命中第二个 → 名字反映第二个
        assert chain.last_provider_name == "ollama"

    @pytest.mark.asyncio
    async def test_last_provider_name_default_fallback_idx(self) -> None:
        """Issue #61: 未传 provider_names 时,缺省标识为 primary / fallback#N。"""
        from core.summarizer import FallbackChainProvider

        primary = AsyncMock()
        primary.generate = AsyncMock(return_value="ok")
        chain = FallbackChainProvider(providers=[primary])
        await chain.generate("ping")
        assert chain.last_provider_name == "primary"

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

    @pytest.mark.asyncio
    async def test_analyze_content_fallback_hit_reflects_in_source(self) -> None:
        """Issue #61: fallback 链第一个失败、第二个命中时,result.source 应反映命中的 provider,
        而非主 provider 名（避免可观测性误导）。"""
        from core.summarizer import FallbackChainProvider

        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"
        config.analysis.api_key = "k"

        primary = AsyncMock()
        primary.generate = AsyncMock(side_effect=RuntimeError("401 unauthorized"))
        secondary = AsyncMock()
        secondary.generate = AsyncMock(return_value="## 摘要\nfallback 答案")

        chain = FallbackChainProvider(
            providers=[primary, secondary],
            provider_names=["openai", "ollama"],
        )

        with patch("core.summarizer.create_provider", return_value=chain):
            result = await analyze_content(
                source_id="bili:BV1",
                title="T",
                author="A",
                text="正文",
                config=config,
            )

        assert result.is_ai is True
        # 命中 ollama（第二个）,不是主 provider openai
        assert result.source == "ollama"
        assert result.source != config.analysis.provider

    @pytest.mark.asyncio
    async def test_analyze_content_primary_hit_source_is_primary_name(self) -> None:
        """Issue #61 回归保护: 第一个 provider 直接命中时,source 仍反映主 provider 名。
        （覆盖 isinstance 分支的 last_provider_name 为 truthy 的路径,验证不破坏单 provider 场景）"""
        from core.summarizer import FallbackChainProvider

        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"
        config.analysis.api_key = "k"

        primary = AsyncMock()
        primary.generate = AsyncMock(return_value="## 摘要\n主 provider 答案")

        chain = FallbackChainProvider(
            providers=[primary],
            provider_names=["openai"],
        )

        with patch("core.summarizer.create_provider", return_value=chain):
            result = await analyze_content(
                source_id="bili:BV1",
                title="T",
                author="A",
                text="正文",
                config=config,
            )

        assert result.is_ai is True
        assert result.source == "openai"

    @pytest.mark.asyncio
    async def test_analyze_content_single_provider_source_reflects_real_name_via_real_create_provider(
        self,
    ) -> None:
        """端到端: 不 mock create_provider, 验证单 provider 配置下 result.source 是真实 provider 名。

        锁住关键不变量: create_provider 总是传显式 provider_names, 所以单 provider 场景下
        result.source == "openai" (而非缺省的 "primary")。
        若未来有人误改 create_provider 删掉 provider_names 参数, 此测试会失败。
        """
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"
        config.analysis.api_key = "test-key"

        ai_output = "## 摘要\n这是真实链路测试\n\n## 一句话总结\nok"

        # 不 mock create_provider，让真实的 FallbackChainProvider（单 provider）跑；
        # 只 mock 底层 LLM 调用（OpenAIProvider.generate 实例方法），避免真实 HTTP。
        with patch.object(OpenAIProvider, "generate", AsyncMock(return_value=ai_output)):
            result = await analyze_content(
                source_id="test-src",
                title="t",
                author="a",
                text="some text",
                config=config,
            )

        assert result.source == "openai"
        assert result.source != "primary"
        assert result.source != "none"
        assert result.is_ai is True


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
    """CoT 泄露修复: reasoning_content 是思考链,不是答案。
    content 为空时不再 fallback 到 reasoning_content,而是返回空串让上游可观测。
    """

    @pytest.mark.asyncio
    async def test_openai_provider_no_fallback_to_reasoning_content(self) -> None:
        """content='' 且 reasoning_content 非空时,generate 返回 ''(不再用思考链当答案)。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "这样5点,每条字数约...总约300字,不够400。所以需要每条扩充...",
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

        # 不再用 reasoning_content 填充答案
        assert result == ""
        assert "不够400" not in result
        assert "需要每条扩充" not in result

    @pytest.mark.asyncio
    async def test_provider_handles_none_content_no_fallback(self) -> None:
        """content=null 且 reasoning_content 非空时也不 fallback,返回空串。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "## 摘要\n这是思考链不应被使用",
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
        assert "不应被使用" not in result

    @pytest.mark.asyncio
    async def test_provider_whitespace_only_content_no_fallback(self) -> None:
        """whitespace-only content 同样不 fallback 到 reasoning_content。"""
        provider = OpenAIProvider(api_base="https://example.com/v1", api_key="k", model_name="deepseek-v4")
        fake_response_json = {
            "choices": [
                {
                    "message": {
                        "content": "   \n  ",
                        "reasoning_content": "## 摘要\n思考链",
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
        """content='' 且无 reasoning_content 时返回空串。"""
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
    """CoT 泄露修复后, _ANALYSIS_PROMPT_TEMPLATE 必须含:
    - 禁止输出思考过程的最高优先级规则
    - 元话语黑名单(让 LLM 识别并停止 CoT 泄露)
    - 字数上限(防止 max_tokens 截断),但不再有 400 字下限(凑字数注水元凶)
    - 要点数量按信息量自然决定(2-3 / 5-8),不强制 3-8
    - 序号格式 + 每条含具体信息

    注意：本类测试只验证 prompt 文本字面量,不验证 LLM 实际遵守。
    """

    def test_prompt_template_forbids_cot_output(self) -> None:
        """prompt 必须含「禁止输出任何思考过程」+ 列出元话语黑名单。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "禁止输出任何思考过程" in _ANALYSIS_PROMPT_TEMPLATE, (
            "prompt 必须明令禁止输出思考过程(CoT 泄露根因)"
        )
        # 元话语黑名单至少含几个关键模式(覆盖线上泄露样本)
        for meta_phrase in ("我重新组织", "总字数", "需要扩充", "不如忽略"):
            assert meta_phrase in _ANALYSIS_PROMPT_TEMPLATE, (
                f"prompt 必须把元话语 {meta_phrase!r} 列入黑名单"
            )

    def test_prompt_does_not_have_word_count_lower_bound(self) -> None:
        """CoT 修复: prompt 必须不再含「400 字」下限(凑字数注水元凶)。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "400 字" not in _ANALYSIS_PROMPT_TEMPLATE, (
            "prompt 不应再含「400 字」字数下限(促使 LLM 输出凑字数 CoT)"
        )
        assert "字数下限" not in _ANALYSIS_PROMPT_TEMPLATE, (
            "prompt 不应再有「字数下限」措辞(改为「无硬性下限」)"
        )

    def test_prompt_contains_word_count_upper_bound(self) -> None:
        """prompt 模板必须包含「1200 字」上限（避免 LLM 输出过长触发 max_tokens 截断）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "1200" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_does_not_force_min_keypoints_count(self) -> None:
        """CoT 修复: prompt 不再强制「3-8 条」下限,改为按信息量自然决定(2-3 / 5-8)。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        # 不应再有「3-8」这种硬性范围(导致信息不足时硬凑要点)
        assert "3-8" not in _ANALYSIS_PROMPT_TEMPLATE, (
            "prompt 不应再有「3-8 条」硬性范围(改为按信息量 2-3 / 5-8)"
        )
        # 应该有按信息量分档的描述
        assert "2-3" in _ANALYSIS_PROMPT_TEMPLATE
        assert "5-8" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_requires_numbered_list_format(self) -> None:
        """prompt 必须要求用「1. 」「2. 」中文序号格式表达要点。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        summary_start = _ANALYSIS_PROMPT_TEMPLATE.find("## 摘要\n")
        next_section = _ANALYSIS_PROMPT_TEMPLATE.find("## 一句话总结\n")
        assert summary_start != -1, "prompt 必须含 `## 摘要` 段标题"
        assert next_section != -1, "prompt 必须含 `## 一句话总结` 段标题"
        summary_block = _ANALYSIS_PROMPT_TEMPLATE[summary_start:next_section]
        assert "1. " in summary_block, "## 摘要 段必须含「1. 」序号约束"
        assert "3. " in summary_block, "## 摘要 段必须含「3. 」序号样例"

    def test_prompt_requires_concrete_info_per_point(self) -> None:
        """prompt 必须要求每条要点含具体信息（数据/案例/时间/论据），不只复述标题。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "数据" in _ANALYSIS_PROMPT_TEMPLATE, "prompt 必须要求每条含「数据」类具体信息"
        assert "论据" in _ANALYSIS_PROMPT_TEMPLATE, "prompt 必须要求每条含「论据」类具体信息"

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


class TestStripCotLeakage:
    """CoT 泄露修复: parse_markdown_analysis 解析后对 summary 做 CoT 截断。"""

    def test_strip_cot_leakage_truncates_at_meta_speech(self) -> None:
        """summary 含「我重新组织:...」时,从该处截断保留前半部分(真正答案)。"""
        from core.summarizer import _strip_cot_leakage

        # 模拟线上泄露样本:前半是真正摘要,后半是模型自言自语
        cot_summary = (
            "1. 视频讲的是短视频起号方法论。\n"
            "2. 关键点是稳定输出。\n"
            "我重新组织:这样5点,每条字数约...总约300字,不够400。"
            "所以需要每条扩充..."
        )
        result = _strip_cot_leakage(cot_summary)
        # 截断在「我重新组织」之前
        assert "我重新组织" not in result
        assert "不够400" not in result
        # 前半部分保留
        assert "短视频起号方法论" in result
        assert "稳定输出" in result

    def test_strip_cot_leakage_preserves_clean_summary(self) -> None:
        """正常 summary 不含元话语,不应被截断。"""
        from core.summarizer import _strip_cot_leakage

        clean_summary = (
            "1. 视频开篇引用 2024 年数据。\n"
            "2. 作者分析了算法机制。\n"
            "3. 最后给出三个开放性问题。"
        )
        result = _strip_cot_leakage(clean_summary)
        assert result == clean_summary

    def test_strip_cot_leakage_detects_various_patterns(self) -> None:
        """多种元话语模式都能被检测截断。"""
        from core.summarizer import _strip_cot_leakage

        # 「需要扩充」
        assert "需要扩充" not in _strip_cot_leakage("真正答案。需要扩充:再来一点")
        # 「不如忽略」
        assert "不如忽略" not in _strip_cot_leakage("要点 A。不如忽略这个话题")
        # 「总字数」
        assert "总字数" not in _strip_cot_leakage("要点。总字数还差 100")

    def test_strip_cot_leakage_keeps_original_if_truncated_empty(self) -> None:
        """当元话语在 summary 起始处,截断后为空,且无其他 pattern 命中时,保留原样让上游 warning 可见。

        helper 逻辑:遇到「截断后为空」的 pattern 会跳过该 pattern 继续尝试下一个;
        若所有命中 pattern 都只能截断到空(或无 pattern 命中)则原样返回。
        """
        from core.summarizer import _strip_cot_leakage

        # 以「我重新组织」开头(第一个 pattern),截断后为空;后续无其他 pattern 命中
        pure_cot_at_start = "我重新组织一下内容"
        result = _strip_cot_leakage(pure_cot_at_start)
        assert result == pure_cot_at_start

    def test_strip_cot_leakage_clean_text_unchanged(self) -> None:
        """不含任何元话语的 summary 原样返回。"""
        from core.summarizer import _strip_cot_leakage

        clean = "1. 要点。2. 另一个要点。"
        assert _strip_cot_leakage(clean) == clean

    def test_parse_markdown_analysis_applies_cot_strip(self) -> None:
        """parse_markdown_analysis 解析出的 summary 已经过 CoT 截断。"""
        raw = """## 摘要
1. 真正的要点。

我重新组织:这里开始是思考过程,总字数不够。

## 一句话总结
一句话

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "我重新组织" not in result.summary
        assert "真正的要点" in result.summary

    def test_cot_patterns_covers_online_leakage_sample(self) -> None:
        """回归保护: _COT_PATTERNS 必须覆盖线上泄露样本的关键元话语。"""
        from core.summarizer import _COT_PATTERNS

        # 线上泄露样本观察到的关键短语
        must_cover = ["我重新组织", "总字数", "需要扩充"]
        for phrase in must_cover:
            assert phrase in _COT_PATTERNS, f"_COT_PATTERNS 必须覆盖线上泄露短语 {phrase!r}"


class TestMaxTokensConfigurable:
    """max_tokens 可配: AnalysisConfig.max_tokens → OpenAIProvider payload。"""

    def test_analysis_config_has_max_tokens_default_8192(self) -> None:
        """AnalysisConfig 新增 max_tokens 字段,默认 8192(防长内容截断)。"""
        cfg = AnalysisConfig()
        assert cfg.max_tokens == 8192

    def test_openai_provider_default_max_tokens_8192(self) -> None:
        """OpenAIProvider 不传 max_tokens 时默认 8192(不再是硬编码 4096)。"""
        provider = OpenAIProvider(api_base="https://x", api_key="k")
        assert provider._max_tokens == 8192

    def test_openai_provider_accepts_custom_max_tokens(self) -> None:
        """OpenAIProvider 构造函数接受 max_tokens 参数。"""
        provider = OpenAIProvider(api_base="https://x", api_key="k", max_tokens=16384)
        assert provider._max_tokens == 16384

    def test_max_tokens_configurable_via_analysis_config(self) -> None:
        """AnalysisConfig 设 max_tokens=16384 → create_provider 构建的 provider 用该值。"""
        config = AnalysisConfig(
            provider="openai",
            api_base="https://example.com/v1",
            api_key="k",
            model_name="gpt-4o-mini",
            max_tokens=16384,
        )
        chain = create_provider(config)
        # 主 provider 是 OpenAIProvider
        provider = chain._providers[0]
        assert isinstance(provider, OpenAIProvider)
        assert provider._max_tokens == 16384

    @pytest.mark.asyncio
    async def test_max_tokens_appears_in_payload(self) -> None:
        """generate 调用时 payload 的 max_tokens 字段使用配置值(非硬编码 4096)。"""
        import json

        provider = OpenAIProvider(
            api_base="https://example.com/v1", api_key="k", max_tokens=2048
        )
        captured_payload: dict = {}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value.__aenter__.return_value
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
            mock_response.status_code = 200
            mock_response.raise_for_status.return_value = None
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "## 摘要\nok"}}]
            }

            # 捕获 post 调用的 payload
            def capture_post(url, json=None, headers=None, timeout=None):  # type: ignore[no-untyped-def]
                captured_payload.update(json or {})
                # 返回一个 awaitable-ish 的 MagicMock(实际通过 return_value 配置)
                return mock_response

            mock_client.post.side_effect = capture_post

            await provider.generate("test prompt")

        assert captured_payload.get("max_tokens") == 2048, (
            f"payload max_tokens 应为 2048,实际 {captured_payload.get('max_tokens')!r}"
        )
        # 回归保护: 不再是旧的硬编码 4096
        _ = json  # silence unused import warning if any
        assert captured_payload.get("max_tokens") != 4096
