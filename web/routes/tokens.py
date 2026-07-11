"""API Token 管理路由（Web UI）。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api.auth import create_token, revoke_token
from web.app import TEMPLATES
from web.auth import load_auth_config

router = APIRouter()


@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request) -> HTMLResponse:
    cfg = load_auth_config()
    return TEMPLATES.TemplateResponse(
        request,
        "tokens.html",
        {
            "active_nav": "tokens",
            "tokens": cfg.api_tokens,
            "plaintext_name": request.session.pop("created_token_name", None),
            "plaintext_value": request.session.pop("created_token_plaintext", None),
        },
    )


@router.post("/tokens/create")
async def tokens_create(
    request: Request,
    name: str | None = Form(None),
    scopes: list[str] | None = Form(None),
) -> RedirectResponse:
    if not name or not name.strip():
        return RedirectResponse(
            url="/tokens?toast_key=token.name_invalid&type=error",
            status_code=303,
        )
    scope_list = scopes or []
    plaintext = create_token(name.strip(), scope_list)
    request.session["created_token_name"] = name.strip()
    request.session["created_token_plaintext"] = plaintext
    return RedirectResponse(url="/tokens?toast_key=token.created&type=success", status_code=303)


@router.post("/tokens/revoke")
async def tokens_revoke(token_name: str = Form(...)) -> RedirectResponse:
    ok = revoke_token(token_name)
    if ok:
        return RedirectResponse(
            url="/tokens?toast_key=token.revoked&type=success",
            status_code=303,
        )
    return RedirectResponse(
        url="/tokens?toast_key=token.not_found&type=error",
        status_code=303,
    )
