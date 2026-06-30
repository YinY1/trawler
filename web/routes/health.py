"""Health check endpoint (issue #55).

无需登录，供监控/告警系统探活与版本核对。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared.constants import BUILD_DATE, GIT_SHA, VERSION

router = APIRouter()


@router.get("/api/health")
async def health() -> JSONResponse:
    """返回服务状态 + 版本信息。

    Response: ``{"status": "ok", "version": str, "git_sha": str, "build_date": str}``
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "version": VERSION,
            "git_sha": GIT_SHA,
            "build_date": BUILD_DATE,
        },
    )
