from __future__ import annotations

from pathlib import Path

import tomlkit
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import load_config
from web.app import TEMPLATES

router = APIRouter()
CONFIG_PATH = "config/config.toml"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Settings page — view current config."""
    config = await load_config()
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {"active_nav": "settings", "config": config},
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    data_dir: str = Form(default="./data"),
    disable_ssl_verify: bool = Form(False),
    gotify_url: str = Form(default=""),
    gotify_token_bili: str = Form(default=""),
    gotify_token_xhs: str = Form(default=""),
    gotify_token_weibo: str = Form(default=""),
    xhs_enabled: bool = Form(False),
    weibo_enabled: bool = Form(False),
) -> RedirectResponse:
    """Save settings to config.toml."""
    p = Path(CONFIG_PATH)
    if p.exists():
        raw = tomlkit.parse(p.read_text(encoding="utf-8"))
    else:
        raw = tomlkit.document()

    # Update general
    raw.setdefault("general", tomlkit.table())["data_dir"] = data_dir
    raw["general"]["disable_ssl_verify"] = disable_ssl_verify

    # Update notifications — always write so fields can be cleared via the UI
    tokens = {
        "bilibili": gotify_token_bili,
        "xiaohongshu": gotify_token_xhs,
        "weibo": gotify_token_weibo,
    }
    for plat in ("bilibili", "xiaohongshu", "weibo"):
        raw.setdefault(plat, tomlkit.table()).setdefault(
            "notification", tomlkit.table()
        )["gotify_url"] = gotify_url
        raw[plat]["notification"]["gotify_token"] = tokens[plat]

    # Update platform enabled flags
    raw.setdefault("xiaohongshu", tomlkit.table())["enabled"] = xhs_enabled
    raw.setdefault("weibo", tomlkit.table())["enabled"] = weibo_enabled

    # Write back with tomlkit to preserve comments and formatting
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(raw), encoding="utf-8")

    return RedirectResponse(url="/settings", status_code=303)
