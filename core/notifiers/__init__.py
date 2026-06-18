"""Notifier 包 — 工厂 + fan-out 便捷函数。

设计要点：
- get_notifiers_for_subscription() 是 cron (handlers) 和 web (未来测试推送按钮)
  共享的唯一入口，确保两侧使用相同的 provider 解析逻辑。
- fan-out：每 endpoint 独立发送，单失败仅 warning，不阻塞其他。
"""

from __future__ import annotations

import logging
from typing import Iterable

from core.notifiers.email import EmailNotifier
from core.notifiers.gotify import GotifyNotifier
from core.notifiers.telegram import TelegramNotifier
from shared.config import Config, EndpointConfig
from shared.protocols import NotificationContent, Notifier, SendResult

logger = logging.getLogger(__name__)

__all__ = [
    "GotifyNotifier",
    "TelegramNotifier",
    "EmailNotifier",
    "get_notifiers_for_subscription",
    "send_to_subscription",
]


_KIND_MAP: dict[str, type] = {
    "gotify": GotifyNotifier,
    "telegram": TelegramNotifier,
    "email": EmailNotifier,
}


def _build_notifier(ep: EndpointConfig) -> Notifier | None:
    cls = _KIND_MAP.get(ep.kind)
    if cls is None:
        logger.warning("未知 endpoint kind %r，跳过", ep.kind)
        return None
    return cls(ep)  # type: ignore[arg-type]


def get_notifiers_for_subscription(
    config: Config, platform: str, endpoint_names: Iterable[str],
) -> list[Notifier]:
    """按订阅声明的 endpoint name 列表解析出 Notifier 实例列表。

    - endpoint name 找不到 → warning + skip
    - endpoint kind 未知 → 警告并跳过
    返回顺序保持声明顺序，便于日志追踪。
    """
    name_to_ep: dict[str, EndpointConfig] = {ep.name: ep for ep in config.endpoints}
    notifiers: list[Notifier] = []
    for name in endpoint_names:
        ep = name_to_ep.get(name)
        if ep is None:
            logger.warning("Endpoint %r not found (referenced by %s subscription)", name, platform)
            continue
        n = _build_notifier(ep)
        if n is not None:
            notifiers.append(n)
    return notifiers


async def send_to_subscription(
    config: Config, platform: str, endpoint_names: Iterable[str],
    content: NotificationContent,
) -> list[SendResult]:
    """Fan-out 发送：遍历订阅声明的 endpoints，每 endpoint 独立 send。

    单 endpoint 失败（含 NotImplementedError / 其他异常）只记 warning，
    不影响其他 endpoint。返回每个 endpoint 的 SendResult。
    """
    notifiers = get_notifiers_for_subscription(config, platform, endpoint_names)
    results: list[SendResult] = []
    for n in notifiers:
        try:
            r = await n.send(content)
        except NotImplementedError as e:
            logger.warning("[%s] Provider 未实现: %s", n.name, e)
            r = SendResult(endpoint_name=n.name, success=False, error=f"not implemented: {e}")
        except Exception as e:
            logger.warning("[%s] 发送异常: %s", n.name, e)
            r = SendResult(endpoint_name=n.name, success=False, error=str(e))
        results.append(r)
    return results
