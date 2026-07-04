"""API v1 router 聚合层。

各业务子 router（check / messages / subscriptions）在后续 task 中追加 include，
T1 阶段只挂 health（无鉴权探活端点）。
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routes.check import router as check_router
from api.routes.health import router as health_router
from api.routes.messages import router as messages_router
from api.routes.subscriptions import router as subscriptions_router

router = APIRouter()
router.include_router(health_router)
router.include_router(check_router)
router.include_router(messages_router)
router.include_router(subscriptions_router)
