"""Email Provider — 占坑，未实现。"""

from __future__ import annotations

from shared.config import EndpointConfig
from shared.protocols import NotificationContent, SendResult


class EmailNotifier:
    """Email 推送。占坑。"""

    def __init__(self, endpoint: EndpointConfig) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name

    async def send(self, content: NotificationContent) -> SendResult:
        raise NotImplementedError("Email notifier not yet implemented")
