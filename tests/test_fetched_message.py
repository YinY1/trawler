from __future__ import annotations

from shared.protocols import ContentType, FetchedMessage


def test_fetched_message_required_fields():
    """必填字段构造成功。"""
    fm = FetchedMessage(
        msg_id="bili:BV1xx",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=1700000000,
        title="测试视频",
        author="UP主",
    )
    assert fm.msg_id == "bili:BV1xx"
    assert fm.platform == "bili"
    assert fm.content_type is ContentType.VIDEO
    assert fm.pubdate == 1700000000


def test_fetched_message_optional_fields_default_empty():
    """xsec_token / body 默认空字符串。"""
    fm = FetchedMessage(
        msg_id="xhs:abc",
        platform="xhs",
        content_type=ContentType.TEXT,
        pubdate=0,
        title="",
        author="",
    )
    assert fm.xsec_token == ""
    assert fm.body == ""
