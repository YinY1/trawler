from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
from web.app import TEMPLATES

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request) -> HTMLResponse:
    """Subscription list page."""
    subs = await list_subscriptions()
    platforms_data = [
        {"key": "bili", "name": "B站", "items": subs.get("bilibili", [])},
        {"key": "xhs", "name": "小红书", "items": subs.get("xiaohongshu", [])},
        {"key": "weibo", "name": "微博", "items": subs.get("weibo", [])},
    ]
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
