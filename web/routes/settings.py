from __future__ import annotations

from pathlib import Path
from typing import Any

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
    import tomllib

    p = Path(CONFIG_PATH)
    raw: dict[str, Any] = {}
    if p.exists():
        with open(p, "rb") as f:
            raw = tomllib.load(f)

    # Update general
    raw.setdefault("general", {})["data_dir"] = data_dir
    raw["general"]["disable_ssl_verify"] = disable_ssl_verify

    # Update notifications
    if gotify_url:
        for plat in ("bilibili", "xiaohongshu", "weibo"):
            raw.setdefault(plat, {}).setdefault("notification", {})["gotify_url"] = gotify_url
    if gotify_token_bili:
        raw.setdefault("bilibili", {}).setdefault("notification", {})["gotify_token"] = gotify_token_bili
    if gotify_token_xhs:
        raw.setdefault("xiaohongshu", {}).setdefault("notification", {})["gotify_token"] = gotify_token_xhs
    if gotify_token_weibo:
        raw.setdefault("weibo", {}).setdefault("notification", {})["gotify_token"] = gotify_token_weibo

    # Update platform enabled flags
    raw.setdefault("xiaohongshu", {})["enabled"] = xhs_enabled
    raw.setdefault("weibo", {})["enabled"] = weibo_enabled

    # Write back with tomlkit to preserve formatting
    import tomlkit
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(raw), encoding="utf-8")

    return RedirectResponse(url="/settings", status_code=303)
