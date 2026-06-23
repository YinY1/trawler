"""Tests for setup/login guard middleware.

验证：
- 公开路径（/login /setup /static/*）不被拦
- 受保护路径在未登录时 302 → /login?next=<path>
- 未 setup 时 302 → /setup（不是 /login）
- 登录后受保护路径可访问
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def auth_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AUTH_TOML_PATH → tmp（不预置密码，让 setup 未完成）。"""
    target = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", target)
    return target


@pytest.fixture
async def not_setup_client(auth_tmp: Path) -> AsyncClient:
    """setup 未完成环境（auth.toml 不存在）。"""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def setup_not_logged_in_client(auth_tmp: Path) -> AsyncClient:
    """已 setup、未登录。"""
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def logged_in_client(auth_tmp: Path) -> AsyncClient:
    """已 setup、已登录。"""
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


# ── 公开路径 ────────────────────────────────────────────────────


class TestPublicPaths:
    async def test_unprotected_path_login_get(
        self, setup_not_logged_in_client: AsyncClient
    ) -> None:
        resp = await setup_not_logged_in_client.get("/login", follow_redirects=False)
        assert resp.status_code == 200

    async def test_unprotected_path_setup_get_when_not_setup(
        self, not_setup_client: AsyncClient
    ) -> None:
        resp = await not_setup_client.get("/setup", follow_redirects=False)
        assert resp.status_code == 200

    async def test_unprotected_path_static(
        self, setup_not_logged_in_client: AsyncClient
    ) -> None:
        # 静态资源不在 login guard 范围；404 也表示"未被 guard 拦"
        resp = await setup_not_logged_in_client.get(
            "/static/tokens.css", follow_redirects=False
        )
        assert resp.status_code != 302


# ── login guard（已 setup、未登录）──────────────────────────────


class TestLoginGuard:
    async def test_protected_path_dashboard_redirects_when_not_logged_in(
        self, setup_not_logged_in_client: AsyncClient
    ) -> None:
        resp = await setup_not_logged_in_client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?next=/"

    async def test_protected_path_redirects_to_next_param(
        self, setup_not_logged_in_client: AsyncClient
    ) -> None:
        resp = await setup_not_logged_in_client.get(
            "/subscriptions", follow_redirects=False
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?next=/subscriptions"

    async def test_protected_path_auth_redirects(
        self, setup_not_logged_in_client: AsyncClient
    ) -> None:
        resp = await setup_not_logged_in_client.get("/auth", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]

    async def test_protected_path_after_login_accessible(
        self, logged_in_client: AsyncClient
    ) -> None:
        resp = await logged_in_client.get("/", follow_redirects=False)
        # 登录后应不再被 login_guard 拦（dashboard 返回 200）
        assert resp.status_code == 200


# ── setup guard（未 setup）──────────────────────────────────────


class TestSetupGuard:
    async def test_force_setup_redirect_when_not_initialized(
        self, not_setup_client: AsyncClient
    ) -> None:
        resp = await not_setup_client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/setup"

    async def test_force_setup_redirect_for_protected_paths(
        self, not_setup_client: AsyncClient
    ) -> None:
        for path in ("/auth", "/settings", "/subscriptions", "/logs", "/endpoints"):
            resp = await not_setup_client.get(path, follow_redirects=False)
            assert resp.status_code == 302, f"{path} should redirect"
            assert resp.headers["location"] == "/setup", f"{path} → /setup"
