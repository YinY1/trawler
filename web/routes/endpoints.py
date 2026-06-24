"""推送端点 CRUD 路由（简化版，不做双向关联 UI）。"""

from __future__ import annotations

from pathlib import Path

import tomlkit
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from tomlkit.items import AoT, Table

from shared.config import EndpointConfig, load_config
from web.app import TEMPLATES

router = APIRouter()
CONFIG_PATH = "config/config.toml"


async def _load_endpoints() -> list[EndpointConfig]:
    cfg = await load_config()
    return cfg.endpoints


def _save_endpoints(endpoints: list[EndpointConfig]) -> None:
    """Rewrite the ``[endpoints]`` AoT in config.toml, preserving other sections.

    tomlkit.parse retains comments / formatting of untouched sections; we only
    replace the ``doc["endpoints"]`` reference with a freshly built AoT.
    """
    p = Path(CONFIG_PATH)
    doc = tomlkit.parse(p.read_text(encoding="utf-8")) if p.exists() else tomlkit.document()
    tables: list[Table] = []
    for ep in endpoints:
        t = tomlkit.table()
        t["name"] = ep.name
        t["url"] = ep.url
        t["token"] = ep.token
        t["priority"] = ep.priority
        t["enabled"] = ep.enabled
        t["kind"] = ep.kind
        tables.append(t)
    aot = AoT(tables)
    doc["endpoints"] = aot
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")


@router.get("/endpoints", response_class=HTMLResponse)
async def endpoints_page(request: Request) -> HTMLResponse:
    endpoints = await _load_endpoints()
    return TEMPLATES.TemplateResponse(
        request,
        "endpoints.html",
        {"active_nav": "endpoints", "endpoints": endpoints},
    )


@router.post("/endpoints/add")
async def endpoint_add(
    name: str = Form(...),
    url: str = Form(...),
    token: str = Form(...),
    priority: int = Form(5),
    kind: str = Form("gotify"),
) -> RedirectResponse:
    endpoints = await _load_endpoints()
    if any(ep.name == name for ep in endpoints):
        return RedirectResponse(
            url="/endpoints?toast_key=endpoint.name_exists&type=error",
            status_code=303,
        )
    endpoints.append(EndpointConfig(name=name, url=url, token=token, priority=priority, kind=kind))
    _save_endpoints(endpoints)
    return RedirectResponse(
        url="/endpoints?toast_key=endpoint.saved&type=success",
        status_code=303,
    )


@router.post("/endpoints/{name}/edit")
async def endpoint_edit(
    name: str,
    url: str = Form(...),
    token: str = Form(...),
    priority: int = Form(5),
    enabled: bool = Form(False),
) -> RedirectResponse:
    endpoints = await _load_endpoints()
    for ep in endpoints:
        if ep.name == name:
            ep.url = url
            ep.token = token
            ep.priority = priority
            ep.enabled = enabled
            break
    else:
        return RedirectResponse(
            url="/endpoints?toast_key=endpoint.not_found&type=error",
            status_code=303,
        )
    _save_endpoints(endpoints)
    return RedirectResponse(
        url="/endpoints?toast_key=endpoint.saved&type=success",
        status_code=303,
    )


@router.post("/endpoints/{name}/delete")
async def endpoint_delete(name: str) -> RedirectResponse:
    endpoints = await _load_endpoints()
    endpoints = [ep for ep in endpoints if ep.name != name]
    _save_endpoints(endpoints)
    return RedirectResponse(
        url="/endpoints?toast_key=endpoint.deleted&type=success",
        status_code=303,
    )
