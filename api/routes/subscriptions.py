"""API subscriptions 路由（T4）。

薄路由，全部业务复用 ``core.subscription_cli``：
- ``GET /subscriptions`` → ``list_subscriptions``
- ``POST /subscriptions`` → ``add_subscription``
- ``DELETE /subscriptions/{platform}/{identifier}`` → ``remove_subscription``

鉴权统一走 ``Security(require_scopes, scopes=[...])``。``add_subscription`` / ``remove_subscription``
返回 ``(False, ...)`` 视为业务正常响应（200 + ``success=False``），不抛 HTTPException
—— 重复 / 未找到是订阅管理的常见态，调用方靠 ``success`` 字段判断，不靠状态码。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, Security

from api.auth import get_resource_filter
from api.resource_filter import (
    TokenResourceFilter,
    filter_subscription_dict,
    subscription_visible,
)
from api.schemas import (
    EndpointBindRequest,
    SubscriptionAddRequest,
    SubscriptionAddResponse,
    SubscriptionListResponse,
    SubscriptionRemoveResponse,
)
from core.subscription_cli import (
    add_endpoint_to_subscription,
    add_subscription,
    list_subscriptions,
    remove_endpoint_from_subscription,
    remove_subscription,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/subscriptions", response_model=SubscriptionListResponse)
async def list_subs(
    request: Request,
    platform: str | None = Query(default=None, description="按平台过滤 (bili/xhs/weibo)"),
    filt: TokenResourceFilter = Security(
        get_resource_filter, scopes=["subscriptions:read"]
    ),
) -> SubscriptionListResponse:
    """列出订阅，可选 platform 过滤。透传 ``list_subscriptions`` 原始 dict。

    行级过滤（issue #106）：在 ``list_subscriptions`` 返回之上叠加 token 的
    ``resource_rules``（``filter_subscription_dict``），越权订阅不返回。
    """
    result = await list_subscriptions(platform=platform)
    result = filter_subscription_dict(result, filt)
    return SubscriptionListResponse(platforms=result)


@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    filt: TokenResourceFilter = Security(
        get_resource_filter, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """添加订阅。

    ``add_subscription`` 返回 ``(False, "已存在: ...")`` 也是 200 正常响应
    （``success=False``），不映射成 4xx —— 重复 / 无效平台是业务可恢复态。
    ``default_notify_endpoint`` 非空时，底层会尝试绑定，失败会回滚订阅添加
    并返回 ``(False, "默认 endpoint 绑定失败: ...")``。
    """
    success, message = await add_subscription(
        body.platform,
        body.identifier,
        body.name,
        default_notify_endpoint=body.default_notify_endpoint,
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}", response_model=SubscriptionRemoveResponse
)
async def remove_sub(
    platform: str,
    identifier: str,
    request: Request,
    filt: TokenResourceFilter = Security(
        get_resource_filter, scopes=["subscriptions:write"]
    ),
) -> SubscriptionRemoveResponse:
    """删除订阅。

    未找到返回 200 + ``success=False``（与 add 的"已存在"语义对称），不映射成 404。

    行级过滤（issue #106）：越权删除（token 不允许该平台/订阅）合并成「未找到」
    语义（spec §7 表 / §8.3），不暴露存在性。
    """
    if not subscription_visible(filt, platform, identifier):
        # 与「未找到」语义合并，不暴露存在性（spec §8.3）
        return SubscriptionRemoveResponse(
            success=False, message="未找到: 订阅不存在或无权访问"
        )
    success, message = await remove_subscription(platform, identifier)
    return SubscriptionRemoveResponse(success=success, message=message)


# ── endpoint 绑定/解绑（spec §4.5）──────────────────────────────────────
# 与 add/remove 订阅端点对称：业务可恢复态（未找到订阅 / 未知 endpoint / 幂等）
# 全部返回 200 + success 字段，不映射 4xx。


@router.post(
    "/subscriptions/{platform}/{identifier}/endpoints",
    response_model=SubscriptionAddResponse,
)
async def bind_endpoint(
    platform: str,
    identifier: str,
    body: EndpointBindRequest,
    request: Request,
    filt: TokenResourceFilter = Security(
        get_resource_filter, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """绑定 endpoint 到订阅。

    ``platform`` 使用全名（``bilibili`` / ``xiaohongshu`` / ``weibo``），
    与现有 ``DELETE /subscriptions/{platform}/{identifier}`` 一致。

    响应语义：
    - 成功（首次/幂等）→ ``success=True``
    - 订阅不存在 → ``success=False``，message="未找到订阅"
    - endpoint 不在 ``[[endpoints]]`` 中 → ``success=False``，message="未知 endpoint: ..."

    行级过滤（issue #106）：越权绑定合并成「未找到订阅」语义（spec §7 表）。
    """
    if not subscription_visible(filt, platform, identifier):
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await add_endpoint_to_subscription(
        platform, identifier, body.endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}/endpoints/{endpoint_name}",
    response_model=SubscriptionAddResponse,
)
async def unbind_endpoint(
    platform: str,
    identifier: str,
    endpoint_name: str,
    request: Request,
    filt: TokenResourceFilter = Security(
        get_resource_filter, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """解绑 endpoint。订阅不存在返回 ``success=False``，其余（含幂等）返回 True。

    不做 endpoint 存在性校验（解绑引用无害，能清理历史脏数据）。

    行级过滤（issue #106）：越权解绑合并成「未找到订阅」语义（spec §7 表）。
    """
    if not subscription_visible(filt, platform, identifier):
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await remove_endpoint_from_subscription(
        platform, identifier, endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)
