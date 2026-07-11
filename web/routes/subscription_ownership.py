"""Sub ownership 管理路由 — HTMX modal。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from core.subscription_cli import SUBSCRIPTION_KEY
from web.app import TEMPLATES
from web.auth import load_auth_config
from web.routes.subscriptions import _platform_key_to_name
from web.routes.subscriptions import list_subscriptions as load_subscriptions

router = APIRouter()


def _key_field(platform_key: str) -> str:
    return SUBSCRIPTION_KEY.get(platform_key, ("user_id",))[0]


@router.get("/subscriptions/{platform}/{identifier}/ownership")
async def ownership_modal(
    request: Request, platform: str, identifier: str
) -> HTMLResponse:
    plat_name = _platform_key_to_name(platform)
    key = _key_field(platform)
    subs = await load_subscriptions()
    items: list[dict[str, Any]] = subs.get(plat_name, [])
    sub = next((s for s in items if str(s.get(key, "")) == identifier), None)

    if sub is None:
        return HTMLResponse("<p class='text-sm text-red-500 p-4'>订阅不存在</p>")

    auth_cfg = load_auth_config()
    all_tokens = auth_cfg.api_tokens
    owner = sub.get("owner_token", "")
    assigned = sub.get("assigned_tokens", [])

    return TEMPLATES.TemplateResponse(
        request,
        "_token_modal.html",
        {
            "platform": platform,
            "identifier": identifier,
            "sub_name": sub.get("name", identifier),
            "owner": owner,
            "assigned": assigned,
            "all_tokens": [t.name for t in all_tokens],
        },
    )
