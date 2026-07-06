from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import (
    add_endpoint_to_subscription,
    add_subscription,
    list_subscriptions,
    remove_endpoint_from_subscription,
    remove_subscription,
    search_by_name,
)
from shared.config import load_config
from web.app import TEMPLATES

router = APIRouter()
logger = logging.getLogger(__name__)
CONFIG_PATH = "config/config.toml"


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request) -> HTMLResponse:
    """Subscription list page."""
    subs = await list_subscriptions()
    cfg = await load_config()
    available_endpoints = [ep.name for ep in cfg.endpoints]

    # Build platform data with per-item unassigned endpoints
    platforms_data: list[dict[str, Any]] = []
    for key, name, plat_key in [
        ("bili", "B站", "bilibili"),
        ("xhs", "小红书", "xiaohongshu"),
        ("weibo", "微博", "weibo"),
    ]:
        items = subs.get(plat_key, [])
        for item in items:
            assigned = set(item.get("notify_endpoints", []))
            item["_unassigned_eps"] = [e for e in available_endpoints if e not in assigned]
        platforms_data.append({"key": key, "name": name, "items": items})

    flash_msg = request.query_params.get("msg", "")
    flash_type = request.query_params.get("type", "")
    return TEMPLATES.TemplateResponse(
        request,
        "subscriptions.html",
        {
            "active_nav": "subscriptions",
            "platforms": platforms_data,
            "flash_msg": flash_msg,
            "flash_type": flash_type,
            "available_endpoints": available_endpoints,
        },
    )


@router.post("/subscriptions/add")
async def subscriptions_add(
    platform: str = Form(...),
    identifier: str = Form(...),
    name: str = Form(...),
) -> RedirectResponse:
    """Add a subscription."""
    logger.info("📋 Web 添加订阅: %s/%s = %s", platform, identifier, name)
    ok, msg = await add_subscription(platform, identifier, name)
    t = "success" if ok else "error"
    return RedirectResponse(url=f"/subscriptions?msg={msg}&type={t}", status_code=303)


@router.post("/subscriptions/remove")
async def subscriptions_remove(
    platform: str = Form(...),
    identifier: str = Form(...),
) -> RedirectResponse:
    """Remove a subscription."""
    logger.info("📋 Web 删除订阅: %s/%s", platform, identifier)
    ok, msg = await remove_subscription(platform, identifier)
    t = "success" if ok else "error"
    return RedirectResponse(url=f"/subscriptions?msg={msg}&type={t}", status_code=303)


@router.post("/subscriptions/search")
async def subscriptions_search(
    request: Request,
    platform: str = Form(...),
    name: str = Form(...),
) -> HTMLResponse:
    """Search for a user by name and show candidates (HTMX target)."""
    logger.info("📋 Web 搜索: %s / %s", platform, name)
    _ok, msg, candidates = await search_by_name(platform, name)
    return TEMPLATES.TemplateResponse(
        request,
        "_candidates.html",
        {"search_platform": platform, "candidates": candidates, "search_msg": msg},
    )


def _platform_key_to_name(key: str) -> str:
    """Map short platform key (bili/xhs/weibo) to config section name."""
    return {"bili": "bilibili", "xhs": "xiaohongshu", "weibo": "weibo"}.get(key, key)


@router.post("/subscriptions/{platform}/{identifier}/endpoints/add")
async def subscription_endpoint_add(
    platform: str,
    identifier: str,
    endpoint_name: str = Form(...),
) -> RedirectResponse:
    """绑定 endpoint 到订阅（重构后调 core 函数）。

    Web 路径用短名 ``bili/xhs/weibo``，core 函数要全名，转换在调用前做。
    """
    plat_name = _platform_key_to_name(platform)
    ok, msg = await add_endpoint_to_subscription(plat_name, identifier, endpoint_name)
    if not ok and "未找到订阅" in msg:
        toast_key, t = "subscription.not_found", "error"
    elif not ok:  # 未知 endpoint
        toast_key, t = "subscription.endpoint_unknown", "error"
    else:
        toast_key, t = "subscription.endpoint_added", "success"
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}", status_code=303
    )


@router.post("/subscriptions/{platform}/{identifier}/endpoints/remove")
async def subscription_endpoint_remove(
    platform: str,
    identifier: str,
    endpoint_name: str = Form(...),
) -> RedirectResponse:
    """从订阅解绑 endpoint（重构后调 core 函数）。

    订阅不存在 → ``subscription.not_found``；其余（含幂等）→ success。
    """
    plat_name = _platform_key_to_name(platform)
    ok, msg = await remove_endpoint_from_subscription(plat_name, identifier, endpoint_name)
    if not ok and "未找到订阅" in msg:
        toast_key, t = "subscription.not_found", "error"
    else:
        toast_key, t = "subscription.endpoint_removed", "success"
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}", status_code=303
    )
