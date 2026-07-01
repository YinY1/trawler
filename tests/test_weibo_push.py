"""Tests for weibo_push — TEXT 正文 fallback + VIDEO 摘要回归保护。

issue #70: TEXT 类型微博(无视频)不走 SUMMARIZED 阶段, ctx.summary_text 恒空,
push handler fallback 到 ctx.content_text 填充 NotificationContent.summary。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
async def test_weibo_text_push_summary_falls_back_to_content_text(
    config: Config, store: MessageStore
) -> None:
    """issue #70: TEXT 类型微博 summary 为空时 fallback 到 content_text 正文。

    TEXT 类型 PHASE_FLOW 不含 SUMMARIZED, ctx.summary_text 恒空;
    push handler 应复用 summary 字段承载正文原文, 让通知正文不丢失。
    """
    from shared.config import UserSubscription

    config.weibo.subscriptions = [
        UserSubscription(user_id="u1", name="U1", notify_endpoints=["ep1"])
    ]

    captured_content: list[NotificationContent] = []

    async def fake_send(cfg, platform, endpoints, content):
        captured_content.append(content)
        from shared.protocols import SendResult

        return [SendResult(endpoint_name="ep1", success=True)]

    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    sys.modules.pop("platforms.weibo.handlers", None)
    try:
        import platforms.weibo.handlers  # noqa: F401

        msg = store.add_new(
            "weibo:W123",
            "weibo",
            ContentType.TEXT,
            2000000000,
            "T",
            "A",
            subscription_ref="u1",
        )
        assert msg is not None

        ctx = PhaseContext(msg=msg, config=config)
        # TEXT 类型:summary 空, content_text 是正文原文(download handler 已写入)
        ctx.summary_text = ""
        ctx.content_text = "今天的分析:比特币支撑位 57000..."

        handler = PipelineEngine._handlers.get(("weibo", Phase.PUSHED))
        assert handler is not None

        with patch("platforms.weibo.handlers.send_to_subscription", new=fake_send):
            result = await handler(ctx)

        assert result is True
        assert len(captured_content) == 1
        c = captured_content[0]
        # 关键:summary 复用承载正文原文, 非空
        assert c.summary == "今天的分析:比特币支撑位 57000..."
        assert "比特币" in c.summary
    finally:
        sys.modules.pop("platforms.weibo.handlers", None)


@pytest.mark.asyncio
async def test_weibo_video_push_summary_prefers_summary_text(
    config: Config, store: MessageStore
) -> None:
    """VIDEO 类型回归保护:summary_text 非空时优先用摘要, 不被 content_text 覆盖。"""
    from shared.config import UserSubscription

    config.weibo.subscriptions = [
        UserSubscription(user_id="u1", name="U1", notify_endpoints=["ep1"])
    ]

    captured_content: list[NotificationContent] = []

    async def fake_send(cfg, platform, endpoints, content):
        captured_content.append(content)
        from shared.protocols import SendResult

        return [SendResult(endpoint_name="ep1", success=True)]

    import sys

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}
    sys.modules.pop("platforms.weibo.handlers", None)
    try:
        import platforms.weibo.handlers  # noqa: F401

        msg = store.add_new(
            "weibo:W999",
            "weibo",
            ContentType.VIDEO,
            2000000000,
            "T",
            "A",
            subscription_ref="u1",
        )
        assert msg is not None

        ctx = PhaseContext(msg=msg, config=config)
        # VIDEO 类型:summary_text 是 AI 摘要, content_text 是视频描述
        ctx.summary_text = "AI 生成的视频摘要"
        ctx.content_text = "视频原始描述"

        handler = PipelineEngine._handlers.get(("weibo", Phase.PUSHED))
        assert handler is not None

        with patch("platforms.weibo.handlers.send_to_subscription", new=fake_send):
            result = await handler(ctx)

        assert result is True
        assert len(captured_content) == 1
        c = captured_content[0]
        # 关键:优先用 summary_text (AI 摘要), 不被 content_text 覆盖
        assert c.summary == "AI 生成的视频摘要"
    finally:
        sys.modules.pop("platforms.weibo.handlers", None)
