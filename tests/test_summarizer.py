"""Tests for core.summarizer — provider factory and LocalFallbackProvider"""

from __future__ import annotations

import pytest

from core.summarizer import LocalFallbackProvider, OpenAIProvider, create_provider
from shared.config import AnalysisConfig


class TestCreateProvider:
    """Tests for the create_provider factory function."""

    def testcreate_provider_openai(self) -> None:
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

    def testcreate_provider_openai_missing_api_base_raises(self) -> None:
        config = AnalysisConfig(
            provider="openai",
            api_base="",
            api_key="sk-x",
        )
        with pytest.raises(ValueError):
            create_provider(config)

    def testcreate_provider_ollama_default_api_base(self) -> None:
        config = AnalysisConfig(provider="ollama", api_base="")
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_base == "http://localhost:11434/v1"

    def testcreate_provider_ollama_custom_api_base(self) -> None:
        config = AnalysisConfig(
            provider="ollama",
            api_base="http://my-host:11434/v1",
        )
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.api_base == "http://my-host:11434/v1"

    def testcreate_provider_unknown_raises(self) -> None:
        config = AnalysisConfig(provider="foobar")
        with pytest.raises(ValueError, match="不支持的 provider"):
            create_provider(config)

    def testcreate_provider_codebuddy_removed(self) -> None:
        config = AnalysisConfig(provider="codebuddy")
        with pytest.raises(ValueError):
            create_provider(config)

    def testcreate_provider_case_insensitive(self) -> None:
        config = AnalysisConfig(
            provider="OpenAI",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
        )
        provider = create_provider(config)
        assert isinstance(provider, OpenAIProvider)


class TestLocalFallbackProvider:
    """Tests for the LocalFallbackProvider."""

    def test_local_fallback_provider_basic(self) -> None:
        provider = LocalFallbackProvider()
        prompt = (
            "标题：x\n作者：y\n正文：这是测试内容。"
            "需要足够长的句子，确保提取式摘要能够正常工作。"
            "再加一句有意义的句子来丰富内容。"
        )
        result = provider.generate(prompt)
        assert isinstance(result, str)
        assert len(result) > 0
