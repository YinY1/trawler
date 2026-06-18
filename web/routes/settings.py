from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.summarizer import create_provider
from shared.config import AnalysisConfig, load_config
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
    xhs_enabled: bool = Form(False),
    weibo_enabled: bool = Form(False),
) -> HTMLResponse:
    """Save settings to config.toml."""
    p = Path(CONFIG_PATH)
    if p.exists():
        raw = tomlkit.parse(p.read_text(encoding="utf-8"))
    else:
        raw = tomlkit.document()

    # Update general
    raw.setdefault("general", tomlkit.table())["data_dir"] = data_dir
    raw["general"]["disable_ssl_verify"] = disable_ssl_verify

    # Update platform enabled flags
    raw.setdefault("xiaohongshu", tomlkit.table())["enabled"] = xhs_enabled
    raw.setdefault("weibo", tomlkit.table())["enabled"] = weibo_enabled

    # Write back with tomlkit to preserve comments and formatting
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(raw), encoding="utf-8")

    # HX-Trigger header is latin-1 only; emit an ASCII-safe toast key and
    # let the client map it to a localized message (see base.html showToast).
    headers = {"HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"}}'}
    return HTMLResponse(content="", headers=headers, status_code=200)


# ── Analysis test probe ────────────────────────────────────────


async def _probe_provider(provider: str, api_base: str, api_key: str, model_name: str) -> dict[str, Any]:
    """Test connectivity with given provider config (does not persist)."""
    try:
        config = AnalysisConfig(
            enabled=True,
            provider=provider,
            api_base=api_base,
            api_key=api_key,
            model_name=model_name,
        )
        prov = create_provider(config)
        result = await prov.generate("ping")
        return {"ok": True, "message": f"连通正常，模型响应: {result[:50]}"}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/settings/analysis/test")
async def settings_analysis_test(
    provider: str = Form(...),
    api_base: str = Form(""),
    api_key: str = Form(""),
    model_name: str = Form(""),
) -> JSONResponse:
    """Test provider connectivity without persisting config."""
    result = await _probe_provider(provider, api_base, api_key, model_name)
    return JSONResponse(result)


# ── Analysis save ─────────────────────────────────────────────


@router.post("/settings/analysis/save")
async def settings_analysis_save(
    enabled: bool = Form(False),
    provider: str = Form(...),
    api_base: str = Form(""),
    api_key: str = Form(""),
    model_name: str = Form(""),
) -> HTMLResponse:
    """Save AI analysis settings to config.toml."""
    p = Path(CONFIG_PATH)
    if p.exists():
        raw = tomlkit.parse(p.read_text(encoding="utf-8"))
    else:
        raw = tomlkit.document()

    raw.setdefault("analysis", tomlkit.table())
    raw["analysis"]["enabled"] = enabled
    raw["analysis"]["provider"] = provider
    raw["analysis"]["api_base"] = api_base
    raw["analysis"]["api_key"] = api_key
    raw["analysis"]["model_name"] = model_name

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(raw), encoding="utf-8")

    headers = {"HX-Trigger": '{"toast":{"key":"settings.saved","type":"success"}}'}
    return HTMLResponse(content="", headers=headers, status_code=200)
