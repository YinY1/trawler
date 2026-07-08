"""API subscriptions 路由（T4 + issue #108 ownership）。

薄路由，全部业务复用 ``core.subscription_cli``：
- ``GET /subscriptions`` → ``list_subscriptions`` + ``filter_subscription_dict``
- ``POST /subscriptions`` → ``add_subscription``（注入 owner_token）
- ``DELETE /subscriptions/{platform}/{identifier}`` → ``remove_subscription``
- ``POST/DELETE /subscriptions/{p}/{id}/endpoints`` → bind/unbind
- ``POST/DELETE /subscriptions/{p}/{id}/assign`` → assign/unassign token（superuser）

鉴权走 ``Security(get_token_ownership, scopes=[...])``，ownership 校验在路由层
调 ``subscription_visible`` / ``ownership.has_sub_*``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, Security

from api.auth import get_token_ownership
from api.resource_filter import (
    TokenOwnership,
    filter_subscription_dict,
    subscription_visible,
)
from api.schemas import (
    AssignRequest,
    EndpointBindRequest,
    SubscriptionAddRequest,
    SubscriptionAddResponse,
    SubscriptionListResponse,
    SubscriptionRemoveResponse,
)
from core.subscription_cli import (
    PLATFORM_TO_SECTION,
    add_endpoint_to_subscription,
    add_subscription,
    assign_token_to_subscription,
    list_subscriptions,
    remove_endpoint_from_subscription,
    remove_subscription,
    unassign_token_from_subscription,
)
from shared.config import load_config

logger = logging.getLogger(__name__)
router = APIRouter()


# ═══════════════════════════════════════════════════════════
# URL {platform} 参数归一化（C1 修订，issue #108 review）
# ═══════════════════════════════════════════════════════════
# 原因：``core.subscription_cli.VALID_PLATFORMS = {"bili", "xhs", "weibo"}``
# 只接受短名，但现有路由（#106 之前）历史上两种形式都进过 URL。
# ``remove_subscription`` / ``assign_token_to_subscription`` 等业务函数
# 直接 ``platform in VALID_PLATFORMS`` 校验，传 ``bilibili`` 会被拒。
# 归一化在路由入口做一次，业务层永远拿到短名。


def _normalize_platform(url_platform: str) -> str | None:
    """URL ``{platform}`` 参数归一化为短名（``bili`` / ``xhs`` / ``weibo``）。

    接受短名（``bili``，直接命中 ``PLATFORM_TO_SECTION``）或 TOML section
    全名（``bilibili``，通过 ``SECTION_TO_SHORT`` 反查），统一返回短名。
    无效返回 ``None``，由调用方合并成「未找到」语义（不暴露平台是否有效）。
    """
    from api.resource_filter import SECTION_TO_SHORT

    if url_platform in PLATFORM_TO_SECTION:
        return url_platform
    return SECTION_TO_SHORT.get(url_platform)


@router.get("/subscriptions", response_model=SubscriptionListResponse)
async def list_subs(
    request: Request,
    platform: str | None = Query(default=None, description="按平台过滤 (bili/xhs/weibo)"),
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:read"]
    ),
) -> SubscriptionListResponse:
    """列出订阅，可选 platform 过滤。

    ownership 过滤（issue #108）：在 ``list_subscriptions`` 返回之上叠加 token 的
    ownership（``filter_subscription_dict``），越权订阅不返回。
    """
    result = await list_subscriptions(platform=platform)
    config = await load_config()
    result = filter_subscription_dict(result, ownership, config)
    return SubscriptionListResponse(platforms=result)


@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """添加订阅。

    issue #108: 注入 ``owner_token=ownership.token_name``，创建者自动成为 owner。
    任何持 ``subscriptions:write`` 的 token 都能创建 sub（决策 #7）。
    """
    success, message = await add_subscription(
        body.platform,
        body.identifier,
        body.name,
        default_notify_endpoint=body.default_notify_endpoint,
        owner_token=ownership.token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}", response_model=SubscriptionRemoveResponse
)
async def remove_sub(
    platform: str,
    identifier: str,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionRemoveResponse:
    """删除订阅。

    ownership 校验（issue #108）：越权删除（非 owner / 非 superuser）合并成
    「未找到」语义，不暴露存在性。assigned 不能删（require_write=True）。

    C1 修订：URL ``{platform}`` 经 ``_normalize_platform`` 归一化为短名，
    ``remove_subscription``（``VALID_PLATFORMS`` 只认短名）不再拒绝全名。
    """
    platform = _normalize_platform(platform)  # type: ignore[assignment]
    if platform is None:
        return SubscriptionRemoveResponse(
            success=False, message="未找到: 订阅不存在或无权访问"
        )
    config = await load_config()
    if not subscription_visible(ownership, config, platform, identifier, require_write=True):
        return SubscriptionRemoveResponse(
            success=False, message="未找到: 订阅不存在或无权访问"
        )
    success, message = await remove_subscription(platform, identifier)
    return SubscriptionRemoveResponse(success=success, message=message)


# ── endpoint 绑定/解绑 ──────────────────────────────────────────────


@router.post(
    "/subscriptions/{platform}/{identifier}/endpoints",
    response_model=SubscriptionAddResponse,
)
async def bind_endpoint(
    platform: str,
    identifier: str,
    body: EndpointBindRequest,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """绑定 endpoint 到订阅。

    ownership 校验（issue #108）：越权绑定（非 owner / 非 superuser）合并成
    「未找到订阅」。assigned 不能绑（require_write=True）。

    C1 修订：URL ``{platform}`` 归一化为短名（业务层只认短名）。
    """
    platform = _normalize_platform(platform)  # type: ignore[assignment]
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    config = await load_config()
    if not subscription_visible(ownership, config, platform, identifier, require_write=True):
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
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """解绑 endpoint。

    ownership 校验（issue #108）：越权解绑合并成「未找到订阅」。

    C1 修订：URL ``{platform}`` 归一化为短名（业务层只认短名）。
    """
    platform = _normalize_platform(platform)  # type: ignore[assignment]
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    config = await load_config()
    if not subscription_visible(ownership, config, platform, identifier, require_write=True):
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await remove_endpoint_from_subscription(
        platform, identifier, endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)


# ── assign / unassign token（superuser 专用，issue #108）────────────


@router.post(
    "/subscriptions/{platform}/{identifier}/assign",
    response_model=SubscriptionAddResponse,
)
async def assign_token(
    platform: str,
    identifier: str,
    body: AssignRequest,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["tokens:manage"]
    ),
) -> SubscriptionAddResponse:
    """把 token 分配到 sub（只 superuser，spec §5.2）。

    - 无效平台（既非短名也非全名）→ 200 + success=False, message="未找到订阅"
    - sub 不存在 → 200 + success=False, message="未找到订阅"
    - token 不存在（不在 auth.toml）→ 200 + success=False, message="未知 token"
    - 已分配（幂等）→ 200 + success=True
    - 成功 → 200 + success=True

    ``tokens:manage`` scope 校验已由 ``Security(scopes=["tokens:manage"])`` 拦截，
    路由内不再判 superuser。

    C1 修订：URL ``{platform}`` 归一化为短名。``assign_token_to_subscription``
    内部 ``platform in VALID_PLATFORMS`` 只认短名（``bili``），传全名
    （``bilibili``）会被拒并返回「无效平台」，路由层归一化后业务层不再误拒。
    测试统一用短名（``/bili/100/assign``）。
    """
    platform = _normalize_platform(platform)  # type: ignore[assignment]
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await assign_token_to_subscription(
        platform=platform,
        identifier=identifier,
        token_name=body.token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}/assign/{token_name}",
    response_model=SubscriptionAddResponse,
)
async def unassign_token(
    platform: str,
    identifier: str,
    token_name: str,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["tokens:manage"]
    ),
) -> SubscriptionAddResponse:
    """取消分配（只 superuser，幂等）。

    C1 修订：URL ``{platform}`` 归一化为短名（业务层只认短名）。
    """
    platform = _normalize_platform(platform)  # type: ignore[assignment]
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await unassign_token_from_subscription(
        platform=platform, identifier=identifier, token_name=token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)
