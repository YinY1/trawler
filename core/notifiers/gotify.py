"""Gotify Provider — 跨 endpoint fan-out。"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from core.notifiers.base import render_markdown
from shared.config import EndpointConfig
from shared.constants import GOTIFY_MAX_RETRIES, GOTIFY_TIMEOUT
from shared.protocols import NotificationContent, SendResult

logger = logging.getLogger(__name__)


class GotifyNotifier:
    """Gotify 单 endpoint 发送器。

    Note: 这是一个 endpoint 一个实例。fan-out 由上层 get_notifiers_for_subscription
    返回的多个 Notifier 实例实现，每个独立 send。
    """

    def __init__(self, endpoint: EndpointConfig) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name

    async def send(self, content: NotificationContent) -> SendResult:
        ep = self.endpoint
        if not ep.enabled:
            logger.debug("[%s] 端点已禁用", ep.name)
            return SendResult(endpoint_name=ep.name, success=False, error="disabled")

        if not ep.url or not ep.token:
            logger.warning("[%s] Gotify 配置不完整", ep.name)
            return SendResult(endpoint_name=ep.name, success=False, error="missing url/token")

        title, message = render_markdown(content)
        url = f"{ep.url.rstrip('/')}/message"
        params = {"token": ep.token}
        payload: dict[str, str | int] = {
            "title": title,
            "message": message,
            "priority": ep.priority,
        }

        for attempt in range(1, GOTIFY_MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.post(
                        url,
                        params=params,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=GOTIFY_TIMEOUT),
                    ) as resp:
                        resp.raise_for_status()
                    logger.info("[%s] Gotify 发送成功: %s", ep.name, title)
                    return SendResult(endpoint_name=ep.name, success=True)
            except asyncio.TimeoutError:
                logger.warning("[%s] Gotify 超时 (%s/%s)", ep.name, attempt, GOTIFY_MAX_RETRIES)
            except aiohttp.ClientConnectionError:
                logger.warning("[%s] Gotify 连接失败 (%s/%s)", ep.name, attempt, GOTIFY_MAX_RETRIES)
            except aiohttp.ClientResponseError as e:
                logger.warning("[%s] Gotify HTTP 错误 (%s/%s): %s", ep.name, attempt, GOTIFY_MAX_RETRIES, e)
            except Exception as e:
                logger.warning("[%s] Gotify 异常 (%s/%s): %s", ep.name, attempt, GOTIFY_MAX_RETRIES, e)

            if attempt < GOTIFY_MAX_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1))

        err = f"failed after {GOTIFY_MAX_RETRIES} retries"
        logger.error("[%s] Gotify 失败: %s", ep.name, title)
        return SendResult(endpoint_name=ep.name, success=False, error=err)
