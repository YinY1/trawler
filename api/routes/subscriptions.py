"""API subscriptions 路由（T4）。

薄路由，全部业务复用 ``core.subscription_cli``：
- ``GET /subscriptions`` → ``list_subscriptions``
- ``POST /subscriptions`` → ``add_subscription``
- ``DELETE /subscriptions/{platform}/{identifier}`` → ``remove_subscription``

鉴权统一走 ``Depends(require_token)``。``add_subscription`` / ``remove_subscription``
返回 ``(False, ...)`` 视为业务正常响应（200 + ``success=False``），不抛 HTTPException
—— 重复 / 未找到是订阅管理的常见态，调用方靠 ``success`` 字段判断，不靠状态码。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request

from api.auth import require_token
from api.schemas import (
    SubscriptionAddRequest,
    SubscriptionAddResponse,
    SubscriptionListResponse,
    SubscriptionRemoveResponse,
)
from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/subscriptions", response_model=SubscriptionListResponse)
async def list_subs(
    request: Request,
    platform: str | None = Query(default=None, description="按平台过滤 (bili/xhs/weibo)"),
    _token_name: str = Depends(require_token),
) -> SubscriptionListResponse:
    """列出订阅，可选 platform 过滤。透传 ``list_subscriptions`` 原始 dict。"""
    result = await list_subscriptions(platform=platform)
    return SubscriptionListResponse(platforms=result)


@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    """添加订阅。

    ``add_subscription`` 返回 ``(False, "已存在: ...")`` 也是 200 正常响应
    （``success=False``），不映射成 4xx —— 重复 / 无效平台是业务可恢复态。
    """
    success, message = await add_subscription(body.platform, body.identifier, body.name)
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}", response_model=SubscriptionRemoveResponse
)
async def remove_sub(
    platform: str,
    identifier: str,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionRemoveResponse:
    """删除订阅。

    未找到返回 200 + ``success=False``（与 add 的"已存在"语义对称），不映射成 404。
    """
    success, message = await remove_subscription(platform, identifier)
    return SubscriptionRemoveResponse(success=success, message=message)
