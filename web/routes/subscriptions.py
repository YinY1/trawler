from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import tomlkit
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
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
) -> HTMLResponse:
    """Add an endpoint reference to a subscription."""
    p = Path("config/subscriptions.toml")
    if not p.exists():
        return HTMLResponse(content="", status_code=404)
    plat_name = _platform_key_to_name(platform)
    doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    doc_dict = cast(dict[str, Any], doc)
    plat_section_raw = doc_dict.get(plat_name, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    plat_section = cast(dict[str, Any], plat_section_raw)
    subs = plat_section.get("subscriptions", [])
    id_field = "uid" if plat_name == "bilibili" else "user_id"
    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_dict = cast(dict[str, Any], sub)
        sub_id = str(sub_dict.get(id_field, ""))
        if sub_id == identifier:
            eps_arr = sub_dict.get("notify_endpoints", [])
            eps_list = [str(e) for e in eps_arr]
            if endpoint_name not in eps_list:
                eps_list.append(endpoint_name)
                sub_dict["notify_endpoints"] = eps_list
            found = True
            break
    if not found:
        return HTMLResponse(
            content="", status_code=404,
            headers={"HX-Trigger": '{"toast":{"msg":"订阅不存在","type":"error"}}'},
        )
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"msg":"端点已添加","type":"success"}}'},
    )


@router.post("/subscriptions/{platform}/{identifier}/endpoints/remove")
async def subscription_endpoint_remove(
    platform: str,
    identifier: str,
    endpoint_name: str = Form(...),
) -> HTMLResponse:
    """Remove an endpoint reference from a subscription."""
    p = Path("config/subscriptions.toml")
    if not p.exists():
        return HTMLResponse(content="", status_code=404)
    plat_name = _platform_key_to_name(platform)
    doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    doc_dict = cast(dict[str, Any], doc)
    plat_section_raw = doc_dict.get(plat_name, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    plat_section = cast(dict[str, Any], plat_section_raw)
    subs = plat_section.get("subscriptions", [])
    id_field = "uid" if plat_name == "bilibili" else "user_id"
    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_dict = cast(dict[str, Any], sub)
        sub_id = str(sub_dict.get(id_field, ""))
        if sub_id == identifier:
            eps_arr = sub_dict.get("notify_endpoints", [])
            eps = [str(e) for e in eps_arr if str(e) != endpoint_name]
            sub_dict["notify_endpoints"] = eps
            found = True
            break
    if not found:
        return HTMLResponse(
            content="", status_code=404,
            headers={"HX-Trigger": '{"toast":{"msg":"订阅不存在","type":"error"}}'},
        )
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return HTMLResponse(
        content="",
        headers={"HX-Trigger": '{"toast":{"msg":"端点已移除","type":"success"}}'},
    )
