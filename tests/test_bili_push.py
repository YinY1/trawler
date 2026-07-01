"""Tests for bili_push — URL 渲染 / 通知 type 按前缀分流 / TEXT 正文 fallback。

Covers plan D5: 改造后 bili_dyn:{id} 消息(纯文字动态,TEXT 类型)仍走
t.bilibili.com/{id} URL 和 type='dynamic' 通知模板。

issue #80: TEXT 类型动态不走 SUMMARIZED 阶段, ctx.summary_text 恒空,
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
    # 先弹出可能被其他测试 import 缓存的模块, 否则下面的 import 不会重新触发
    # @PipelineEngine.register 装饰器,_handlers 会保持为空。
    sys.modules.pop("platforms.bilibili.handlers", None)
    try:
        import platforms.bilibili.handlers  # noqa: F401

        msg = store.add_new(
            "bili_dyn:dynX",
            "bili",
            ContentType.TEXT,
            2000000000,
            "T",
            "A",
            subscription_ref="100",
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
    # 先弹出可能被其他测试 import 缓存的模块, 否则下面的 import 不会重新触发
    # @PipelineEngine.register 装饰器,_handlers 会保持为空。
    sys.modules.pop("platforms.bilibili.handlers", None)
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


@pytest.mark.asyncio
async def test_bili_dyn_text_push_summary_falls_back_to_content_text(
    config: Config, store: MessageStore
) -> None:
    """issue #80: TEXT 类型纯文字动态 summary 为空时 fallback 到 content_text 正文。

    TEXT 类型 PHASE_FLOW 不含 SUMMARIZED, ctx.summary_text 恒空;
    push handler 应复用 summary 字段承载正文原文, 让通知正文不丢失。
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
    sys.modules.pop("platforms.bilibili.handlers", None)
    try:
        import platforms.bilibili.handlers  # noqa: F401

        msg = store.add_new(
            "bili_dyn:dynY",
            "bili",
            ContentType.TEXT,
            2000000000,
            "T",
            "A",
            subscription_ref="100",
        )
        assert msg is not None

        ctx = PhaseContext(msg=msg, config=config)
        # TEXT 类型:summary 空, content_text 是正文原文(detector/download 已写入)
        ctx.summary_text = ""
        ctx.content_text = "比特币最低到达 57000+, 这一波震荡..."

        handler = PipelineEngine._handlers.get(("bili", Phase.PUSHED))
        assert handler is not None

        with patch("platforms.bilibili.handlers.send_to_subscription", new=fake_send):
            result = await handler(ctx)

        assert result is True
        assert len(captured_content) == 1
        c = captured_content[0]
        # 关键:summary 复用承载正文原文, 非空
        assert c.summary == "比特币最低到达 57000+, 这一波震荡..."
        assert "比特币" in c.summary
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)


@pytest.mark.asyncio
async def test_bili_video_push_summary_prefers_summary_text(
    config: Config, store: MessageStore
) -> None:
    """VIDEO 类型回归保护:summary_text 非空时优先用摘要, 不被 content_text 覆盖。"""
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
    sys.modules.pop("platforms.bilibili.handlers", None)
    try:
        import platforms.bilibili.handlers  # noqa: F401

        msg = store.add_new(
            "bili:BV1zz9999",
            "bili",
            ContentType.VIDEO,
            2000000000,
            "T",
            "A",
            subscription_ref="100",
        )
        assert msg is not None

        ctx = PhaseContext(msg=msg, config=config)
        # VIDEO 类型:summary_text 是 AI 摘要, content_text 是视频描述
        ctx.summary_text = "AI 生成的视频摘要内容"
        ctx.content_text = "视频原始描述文本"

        handler = PipelineEngine._handlers.get(("bili", Phase.PUSHED))
        assert handler is not None

        with patch("platforms.bilibili.handlers.send_to_subscription", new=fake_send):
            result = await handler(ctx)

        assert result is True
        assert len(captured_content) == 1
        c = captured_content[0]
        # 关键:优先用 summary_text (AI 摘要), 不被 content_text 覆盖
        assert c.summary == "AI 生成的视频摘要内容"
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)
