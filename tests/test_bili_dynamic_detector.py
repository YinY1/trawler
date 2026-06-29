"""Tests for bili_dynamic_detector — 纯文字/图文动态注册为 TEXT。

Covers spec §2 case 3 + plan D3: detector 注册消息时同步 mark_body 写入动态正文。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.engine import PipelineEngine
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, DynamicInfo, Phase


@pytest.fixture
def config() -> Config:
    cfg = Config()
    cfg.bilibili.monitor.watch_dynamic = True
    return cfg


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path)


def _make_text_dynamic(dynamic_id: str = "dyn1") -> DynamicInfo:
    """纯文字动态(has_video=False)。"""
    return DynamicInfo(
        dynamic_id=dynamic_id,
        title="文字动态标题",
        author="UP1",
        uid=100,
        pubdate=2000000000,
        link=f"https://t.bilibili.com/{dynamic_id}",
        content="这是动态正文内容",
        image_urls=[],
        linked_bvid="",
        has_video=False,
    )


@pytest.mark.asyncio
async def test_text_dynamic_registered_as_text_with_body(
    config: Config, store: MessageStore
) -> None:
    """纯文字动态:detector 注册为 bili_dyn:{id},content_type=TEXT,body 写入 dyn.content。"""
    # 准备一个订阅,fetch_new_dynamics 返回 1 条纯文字动态
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]
    dyn = _make_text_dynamic()

    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        # 触发 detector 注册
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 断言:消息已注册,类型 TEXT,phase DISCOVERED
    msg = store.get_message("bili_dyn:dyn1")
    assert msg is not None
    assert msg.content_type == ContentType.TEXT
    assert msg.phase == Phase.DISCOVERED
    assert msg.title == "文字动态标题"
    assert msg.author == "UP1"
    # 关键:body 已在 detector 阶段写入动态正文(plan D3)
    assert msg.body == "这是动态正文内容"
