"""``GET /api/v1/health`` — 无鉴权健康检查端点。

与 ``web/routes/health.py`` 的 ``GET /api/health``（issue #55 产品，监控探活）
并存：本端点是 API v1 namespace 内的健康检查，返回精简 JSON（仅 status + version）。
两者语义一致但响应 shape 略不同：API v1 端点不含 git_sha / build_date
（bot 关心服务存活，不关心构建元数据）。

无鉴权：``/api/*`` 整段在 ``web/app.py:_PUBLIC_PREFIXES`` 与 ``csrf_guard``
入口判断中被豁免（见 ``web/app.py``）；本路由也不挂 ``Depends(require_scopes)``，
所以无需任何凭证即可访问。
"""

from __future__ import annotations

from fastapi import APIRouter

from shared.constants import VERSION_DISPLAY

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """返回 ``{"status": "ok", "version": "<VERSION_DISPLAY>"}``。"""
    return {"status": "ok", "version": VERSION_DISPLAY}
