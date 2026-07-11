"""API Token 管理路由（Web UI）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

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
