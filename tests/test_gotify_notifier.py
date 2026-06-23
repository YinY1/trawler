"""Tests for core/notifiers/gotify.py — GotifyNotifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from core.notifiers.gotify import GotifyNotifier
from shared.config import EndpointConfig
from shared.protocols import NotificationContent


def _content() -> NotificationContent:
    return NotificationContent(platform="bili", source_id="BV1xx", title="t", author="A", summary="s")


@pytest.mark.asyncio
async def test_send_disabled_endpoint():
    ep = EndpointConfig(name="e", url="u", token="t", enabled=False)
    n = GotifyNotifier(ep)
    r = await n.send(_content())
    assert r.success is False
    assert r.error == "disabled"


@pytest.mark.asyncio
async def test_send_missing_token():
    ep = EndpointConfig(name="e", url="u", token="")
    n = GotifyNotifier(ep)
    r = await n.send(_content())
    assert r.success is False
    assert "missing" in r.error


@pytest.mark.asyncio
async def test_send_success():
    ep = EndpointConfig(name="e", url="https://g.example.com", token="tk")
    n = GotifyNotifier(ep)

    # mock aiohttp.ClientSession.post
    fake_resp = MagicMock()
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=None)
    fake_resp.raise_for_status = MagicMock()

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=fake_resp)

    with patch("core.notifiers.gotify.aiohttp.ClientSession", return_value=fake_session):
        r = await n.send(_content())
    assert r.success is True
    assert r.endpoint_name == "e"
    # 验证 URL 和 payload
    fake_session.post.assert_called_once()
    args, kwargs = fake_session.post.call_args
    assert args[0] == "https://g.example.com/message"
    assert kwargs["params"] == {"token": "tk"}
    assert "📹 t" == kwargs["json"]["title"]


@pytest.mark.asyncio
async def test_send_returns_error_on_failure():
    """测试失败时返回 SendResult(success=False)，不抛异常。"""
    ep = EndpointConfig(name="e", url="https://g.example.com", token="tk", priority=1)
    n = GotifyNotifier(ep)

    # GOTIFY_MAX_RETRIES 通常为 3，patch sleep 加速
    with (
        patch("core.notifiers.gotify.aiohttp.ClientSession") as ms,
        patch("core.notifiers.gotify.asyncio.sleep", new=AsyncMock()),
    ):
        ms.side_effect = aiohttp.ClientConnectionError("conn refused")
        r = await n.send(_content())
    assert r.success is False
    assert "failed" in r.error
