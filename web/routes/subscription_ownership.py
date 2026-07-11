"""Sub ownership 管理路由 — HTMX modal。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import (
    SUBSCRIPTION_KEY,
    assign_token_to_subscription,
    unassign_token_from_subscription,
)
from web.app import TEMPLATES
from web.auth import load_auth_config
from web.routes.subscriptions import _platform_key_to_name
from web.routes.subscriptions import list_subscriptions as load_subscriptions

router = APIRouter()


def _key_field(platform_key: str) -> str:
    return SUBSCRIPTION_KEY.get(platform_key, ("user_id",))[0]


@router.get("/subscriptions/{platform}/{identifier}/ownership")
async def ownership_modal(request: Request, platform: str, identifier: str) -> HTMLResponse:
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


@router.post("/subscriptions/{platform}/{identifier}/assign")
async def ownership_assign(platform: str, identifier: str, token_name: str = Form(...)) -> RedirectResponse:
    # platform 来自 URL path，已经是短名（bili/xhs/weibo），直接传给 core；
    # core 内部用 PLATFORM_TO_SECTION 转换。不要再 _platform_key_to_name，
    # 否则 core 收到 "bilibili" 不在 VALID_PLATFORMS 里 → "无效平台: bilibili"。
    ok, _msg = await assign_token_to_subscription(platform, identifier, token_name)
    toast_key, t = ("token.assigned", "success") if ok else ("token.assign_failed", "error")
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}",
        status_code=303,
    )


@router.post("/subscriptions/{platform}/{identifier}/unassign")
async def ownership_unassign(platform: str, identifier: str, token_name: str = Form(...)) -> RedirectResponse:
    # 同上：platform 是短名，core 内部自己转换，web 层不要重复转。
    ok, _msg = await unassign_token_from_subscription(platform, identifier, token_name)
    toast_key, t = ("token.assigned", "success") if ok else ("token.assign_failed", "error")
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}",
        status_code=303,
    )
