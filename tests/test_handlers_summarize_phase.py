"""Tests for platforms.bilibili.handlers.summarize_phase — Issue #56 silent empty 观测。

独立文件：避免污染 tests/test_summarizer.py 的 458 行结构，
summarize_phase 是 cross-platform handler（@register('*', Phase.SUMMARIZED)）。

重点验证：解析成功但 summary 为空时，handler 必须打 warning 让运维可见，
同时仍返回 True（消息继续推进，避免重试爆炸）。

注意：``summarize_phase`` 在每个测试函数内部 import（而非模块顶部），
因为 tests/test_engine.py 的 ``clean_engine_state`` autouse fixture 会
``sys.modules.pop("platforms.bilibili.handlers")``，导致模块顶部 import 在
收集期绑定的函数对象与 patch 目标模块脱节。函数内 import 确保运行时拿到
sys.modules 当前版本，与 patch 目标一致。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.summarizer import AnalysisResult
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
    async def test_warns_on_empty_summary_but_returns_true(self, caplog: pytest.LogCaptureFixture) -> None:
        from platforms.bilibili.handlers import summarize_phase

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
                with caplog.at_level(logging.WARNING, logger="trawler.bilibili.handlers"):
                    result = await summarize_phase(ctx)

        # 接受空 summary，仍返回 True（避免重试爆炸）
        assert result is True
        # ctx.summary_text 为空字符串
        assert ctx.summary_text == ""
        # 必须打 warning 让运维可见，并验证 raw 长度被打入日志
        # raw="## 摘要\n\n## 关键词\n" 实际长度为 14（len() 计算结果，非猜测）
        warnings = [r.getMessage() for r in caplog.records if "AI 摘要解析为空" in r.getMessage()]
        assert warnings, (
            f"未找到 silent empty warning，实际日志: {[r.getMessage() for r in caplog.records]}"
        )
        assert "raw 长度=14" in warnings[0], f"warning 缺少 raw 长度: {warnings[0]}"

    @pytest.mark.asyncio
    async def test_no_warning_when_summary_non_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        """summary 非空时不应打 silent empty warning（回归保护）。"""
        from platforms.bilibili.handlers import summarize_phase

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
                with caplog.at_level(logging.WARNING, logger="trawler.bilibili.handlers"):
                    result = await summarize_phase(ctx)

        assert result is True
        assert ctx.summary_text == "这是有效的摘要内容"
        assert not any("AI 摘要解析为空" in r.getMessage() for r in caplog.records), (
            "summary 非空时不应打 silent empty warning"
        )

    @pytest.mark.asyncio
    async def test_failed_analysis_still_returns_false(self) -> None:
        """failed=True 时 handler 返回 False（保持原有 retry 行为，warning 已有）。"""
        from platforms.bilibili.handlers import summarize_phase

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
