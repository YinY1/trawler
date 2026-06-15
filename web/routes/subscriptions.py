from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
from web.app import TEMPLATES

router = APIRouter()


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request) -> HTMLResponse:
    """Subscription list page."""
    subs = await list_subscriptions()
    platforms_data = [
        {"key": "bili", "name": "B站", "items": subs.get("bilibili", [])},
        {"key": "xhs", "name": "小红书", "items": subs.get("xiaohongshu", [])},
        {"key": "weibo", "name": "微博", "items": subs.get("weibo", [])},
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "subscriptions.html",
        {"active_nav": "subscriptions", "platforms": platforms_data},
    )


@router.post("/subscriptions/add")
async def subscriptions_add(
    platform: str = Form(...),
    identifier: str = Form(...),
    name: str = Form(...),
) -> RedirectResponse:
    """Add a subscription."""
    _ok, _msg = await add_subscription(platform, identifier, name)
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/remove")
async def subscriptions_remove(
    platform: str = Form(...),
    identifier: str = Form(...),
) -> RedirectResponse:
    """Remove a subscription."""
    _ok, _msg = await remove_subscription(platform, identifier)
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/search")
async def subscriptions_search(
    request: Request,
    platform: str = Form(...),
    name: str = Form(...),
) -> HTMLResponse:
    """Search for a user by name and show candidates (HTMX target)."""
    ok, msg, candidates = await search_by_name(platform, name)

    # Render minimal HTML fragment — Jinja2 doesn't support `#fragment` syntax
    items_html = ""
    if candidates:
        for c in candidates:
            cid = c.get("uid") or c.get("user_id", "")
            cname = c.get("name", "?")
            items_html += f"<li>{cname} (ID: {cid}) "
            items_html += f"""<form action="/subscriptions/add" method="post" style="display:inline;">
                <input type="hidden" name="platform" value="{platform}">
                <input type="hidden" name="identifier" value="{cid}">
                <input type="hidden" name="name" value="{cname}">
                <button type="submit" class="btn-primary btn-small">添加</button>
            </form></li>"""

    return HTMLResponse(
        f"<p>{msg}</p><ul>{items_html}</ul>"
        + ('<p style="color:#999;">未找到匹配</p>' if not candidates and ok else "")
    )
