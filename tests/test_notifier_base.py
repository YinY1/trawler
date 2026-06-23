"""Tests for shared/protocols.py — Notifier Protocol + NotificationContent + SendResult."""

from __future__ import annotations

from shared.protocols import NotificationContent, SendResult


def test_notification_content_defaults():
    c = NotificationContent(platform="bili", source_id="BV1xx", title="t", author="a")
    assert c.summary == ""
    assert c.keywords == []
    assert c.comment_highlights == ""
    assert c.url == ""
    assert c.type == "content"


def test_notification_content_full():
    c = NotificationContent(
        platform="weibo",
        source_id="123",
        title="t",
        author="a",
        summary="s",
        keywords=["k1"],
        comment_highlights="ch",
        url="https://weibo.com/123",
        type="dynamic",
    )
    assert c.keywords == ["k1"]


def test_send_result_shape():
    r = SendResult(endpoint_name="default", success=True)
    assert r.success is True
    assert r.error == ""


# ═══════════════════════════════════════════════════════════
# Task 4 — render_markdown
# ═══════════════════════════════════════════════════════════

from core.notifiers.base import render_markdown  # noqa: E402


def test_render_bili_video():
    c = NotificationContent(
        platform="bili",
        source_id="BV1xx",
        title="t",
        author="UP",
        summary="s",
        keywords=["k1", "k2"],
    )
    title, msg = render_markdown(c)
    assert title.startswith("📹")
    assert "BV1xx" in msg
    assert "https://www.bilibili.com/video/BV1xx" in msg
    assert "UP主:** UP" in msg
    assert "k1；k2" in msg


def test_render_xhs_default_url():
    c = NotificationContent(platform="xhs", source_id="note1", title="t", author="A")
    _, msg = render_markdown(c)
    assert "https://www.xiaohongshu.com/explore/note1" in msg
    assert "作者:** A" in msg


def test_render_weibo_custom_url():
    c = NotificationContent(
        platform="weibo",
        source_id="p1",
        title="t",
        author="A",
        url="https://weibo.com/custom",
    )
    _, msg = render_markdown(c)
    assert "https://weibo.com/custom" in msg


def test_render_dynamic_short_format():
    c = NotificationContent(
        platform="bili",
        source_id="dyn123",
        title="t",
        author="UP",
        summary="动态正文",
        type="dynamic",
    )
    title, msg = render_markdown(c)
    assert title == "📢 UP 的动态"
    assert "动态正文" in msg
    assert "关键词" not in msg  # 动态无 keywords 段


def test_render_comment_highlights():
    c = NotificationContent(
        platform="weibo",
        source_id="p",
        title="t",
        author="A",
        comment_highlights="精选评论",
    )
    _, msg = render_markdown(c)
    assert "评论区补充" in msg
    assert "精选评论" in msg


def test_render_unknown_platform_uses_default_emoji():
    c = NotificationContent(platform="unknown", source_id="x", title="t", author="A")
    title, _ = render_markdown(c)
    assert title.startswith("📣")


# ═══════════════════════════════════════════════════════════
# Task 7 — 工厂 + fan-out
# ═══════════════════════════════════════════════════════════

import pytest  # noqa: E402

from core.notifiers import (  # noqa: E402
    GotifyNotifier,
    TelegramNotifier,
    get_notifiers_for_subscription,
    send_to_subscription,
)
from shared.config import Config, EndpointConfig  # noqa: E402


def _cfg(endpoints):
    c = Config()
    c.endpoints = endpoints
    return c


def test_get_notifiers_empty_when_no_match():
    cfg = _cfg([EndpointConfig(name="a", url="u", token="t")])
    ns = get_notifiers_for_subscription(cfg, "bili", ["nonexistent"])
    assert ns == []


def test_get_notifiers_returns_gotify_by_default():
    cfg = _cfg([EndpointConfig(name="a", url="u", token="t")])  # kind default = gotify
    ns = get_notifiers_for_subscription(cfg, "bili", ["a"])
    assert len(ns) == 1
    assert isinstance(ns[0], GotifyNotifier)


def test_get_notifiers_preserves_order():
    cfg = _cfg(
        [
            EndpointConfig(name="a", url="u1", token="t"),
            EndpointConfig(name="b", url="u2", token="t"),
        ]
    )
    ns = get_notifiers_for_subscription(cfg, "bili", ["b", "a"])
    assert [n.name for n in ns] == ["b", "a"]


def test_get_notifiers_telegram_kind():
    cfg = _cfg([EndpointConfig(name="tg", url="u", token="t", kind="telegram")])
    ns = get_notifiers_for_subscription(cfg, "bili", ["tg"])
    assert isinstance(ns[0], TelegramNotifier)


@pytest.mark.asyncio
async def test_send_to_subscription_fan_out_both_succeed(monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(
        [
            EndpointConfig(name="a", url="u1", token="t"),
            EndpointConfig(name="b", url="u2", token="t"),
        ]
    )
    content = NotificationContent(platform="bili", source_id="x", title="t", author="a")

    from core.notifiers import gotify as g_mod

    async def _stub_send(self, c):
        return SendResult(endpoint_name=self.name, success=True)

    monkeypatch.setattr(g_mod.GotifyNotifier, "send", _stub_send)

    results = await send_to_subscription(cfg, "bili", ["a", "b"], content)

    assert len(results) == 2
    assert all(r.success for r in results)
    assert {r.endpoint_name for r in results} == {"a", "b"}


@pytest.mark.asyncio
async def test_send_to_subscription_continues_after_failure(monkeypatch: pytest.MonkeyPatch):
    cfg = _cfg(
        [
            EndpointConfig(name="a", url="u1", token="t"),
            EndpointConfig(name="tg", url="u", token="t", kind="telegram"),  # NotImplementedError
        ]
    )
    content = NotificationContent(platform="bili", source_id="x", title="t", author="a")

    from core.notifiers import gotify as g_mod

    async def _stub_send(self, c):
        return SendResult(endpoint_name=self.name, success=True)

    monkeypatch.setattr(g_mod.GotifyNotifier, "send", _stub_send)

    results = await send_to_subscription(cfg, "bili", ["a", "tg"], content)

    assert len(results) == 2
    assert results[0].success is True  # a (gotify) 成功
    assert results[1].success is False  # tg NotImplementedError 被吞
    assert "not implemented" in results[1].error
