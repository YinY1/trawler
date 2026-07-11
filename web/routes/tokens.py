"""API Token 管理路由（Web UI）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.app import TEMPLATES
from web.auth import load_auth_config  # noqa: F401  (Task 2 将调用；Task 1 仅作为 mock 目标占位)

router = APIRouter()


@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "tokens.html",
        {"active_nav": "tokens"},
    )
