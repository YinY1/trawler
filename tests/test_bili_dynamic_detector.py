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


def _make_video_dynamic(dynamic_id: str = "vdyn1", bvid: str = "BV1xx8888") -> DynamicInfo:
    """视频型动态(has_video=True, linked_bvid 非空)。"""
    return DynamicInfo(
        dynamic_id=dynamic_id,
        title="视频动态标题",
        author="UP1",
        uid=100,
        pubdate=2000000000,
        link=f"https://t.bilibili.com/{dynamic_id}",
        content="视频动态的附加说明文字",
        image_urls=[],
        linked_bvid=bvid,
        has_video=True,
    )


@pytest.mark.asyncio
async def test_video_dynamic_with_existing_video_appends_dynamic_text(
    config: Config, store: MessageStore
) -> None:
    """case 1: 视频型动态,对应 bili:{bvid} 已注册 → 追加 dynamic_text, 不新增消息。"""
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]

    # 预先注册对应视频(bili_detector 会先于 bili_dynamic_detector 执行)
    store.add_new(
        msg_id="bili:BV1xx8888",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=2000000000,
        title="视频标题",
        author="UP1",
        subscription_ref="100",
    )

    dyn = _make_video_dynamic()
    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 断言:未新增 bili_dyn: 消息
    assert store.get_message("bili_dyn:vdyn1") is None
    # 断言:已注册视频的 dynamic_text 被追加
    video_msg = store.get_message("bili:BV1xx8888")
    assert video_msg is not None
    assert video_msg.dynamic_text == "视频动态的附加说明文字"


@pytest.mark.asyncio
async def test_video_dynamic_without_existing_video_registers_as_video(
    config: Config, store: MessageStore
) -> None:
    """case 2: 视频型动态,对应 bili:{bvid} 未注册 → 以 bili:{bvid} 注册为 VIDEO。

    spec §2 case 2 描述的「反查 bvid」在当前实现下不需要——linked_bvid 已在 dyn 上。
    """
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]

    dyn = _make_video_dynamic(bvid="BV1xx7777")
    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 关键断言:以 bili:{bvid} 注册为 VIDEO(不是 bili_dyn:{dynamic_id})
    assert store.get_message("bili_dyn:vdyn1") is None
    msg = store.get_message("bili:BV1xx7777")
    assert msg is not None
    assert msg.content_type == ContentType.VIDEO
    assert msg.phase == Phase.DISCOVERED
    assert msg.author == "UP1"
    # 动态正文作为 dynamic_text 附加(plan D7)
    assert msg.dynamic_text == "视频动态的附加说明文字"


@pytest.mark.asyncio
async def test_video_dynamic_without_content_does_not_append_empty_dynamic_text(
    config: Config, store: MessageStore
) -> None:
    """case 1 边缘:视频型动态无附加文字(content 为空) → 不追加空字符串到 dynamic_text。"""
    from shared.config import BiliSubscription

    config.bilibili.subscriptions = [BiliSubscription(uid=100, name="UP1", notify_endpoints=[])]
    store.add_new(
        msg_id="bili:BV1xx6666",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=2000000000,
        title="V",
        author="UP1",
        subscription_ref="100",
    )

    dyn = DynamicInfo(
        dynamic_id="vdyn2",
        title="t",
        author="UP1",
        uid=100,
        pubdate=2000000000,
        link="https://t.bilibili.com/vdyn2",
        content="   ",  # 空白
        linked_bvid="BV1xx6666",
        has_video=True,
    )
    with patch(
        "platforms.bilibili.dynamic.fetch_new_dynamics",
        new=AsyncMock(return_value=[dyn]),
    ):
        import platforms.bilibili.handlers  # noqa: F401

        detector = PipelineEngine._detectors.get("bili_dynamic")
        assert detector is not None
        await detector(config, store)

    # 断言:dynamic_text 保持空(未被空白污染)
    final_msg = store.get_message("bili:BV1xx6666")
    assert final_msg is not None
    assert final_msg.dynamic_text == ""
