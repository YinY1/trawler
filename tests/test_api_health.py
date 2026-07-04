"""Tests for GET /api/v1/health (T1).

无鉴权探活端点，挂载在 /api/v1 前缀下。与 web/routes/health.py 的
``GET /api/health``（issue #55）并存，不互相替代。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from shared.constants import VERSION_DISPLAY
from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """已 setup 的 client（API 不需要登录，但 setup guard 仍生效）。"""
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    async def test_health_no_auth_required(self, client: AsyncClient) -> None:
        """不带任何 token 访问 /api/v1/health 返回 200。"""
        resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": VERSION_DISPLAY}

    async def test_health_status_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/health")
        assert resp.json()["status"] == "ok"

    async def test_health_returns_version(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/health")
        assert resp.json()["version"] == VERSION_DISPLAY


class TestMiddlewareExemption:
    """验证 /api/* 整段被中间件豁免（auth_guard + csrf_guard），不会被 302 重定向。

    spec §风险与缓解：未带 token 打 /api/* 必须 401/404 JSON，**不是** 302 redirect
    到 /login。本类用 /api/v1/nonexistent（不存在的端点）验证 auth_guard 不拦截。
    """

    async def test_unknown_api_path_not_redirected_to_login(self, client: AsyncClient) -> None:
        """未登录请求 /api/v1/<不存在的端点> 应是 404，不是 302。"""
        resp = await client.get("/api/v1/nonexistent", follow_redirects=False)
        # 应是 404，不是 302 redirect 到 /login
        assert resp.status_code != 302
        assert resp.status_code == 404
