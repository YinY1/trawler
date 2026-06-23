"""Tests for CSRF middleware.

策略：已登录用户的写操作（POST/PUT/PATCH/DELETE）必须通过：
- HTMX header ``X-Requested-With: XMLHttpRequest``，或
- 同源 Referer

豁免：``/login``、``/setup``（未登录用户的 POST，无 session 可盗）、``/static/*``。
``/logout`` 不豁免（防 CSRF 强制登出）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def logged_in_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """已登录 client。"""
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


@pytest.fixture
async def anon_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """已 setup、未登录 client（用于 /login /setup POST 测试）。"""
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestCSRF:
    async def test_post_without_header_blocked_when_logged_in(
        self, logged_in_client: AsyncClient
    ) -> None:
        """登录后 POST 不带 HTMX 头 / 同源 referer → 403。"""
        resp = await logged_in_client.post(
            "/settings/account",
            data={
                "current_password": PASSWORD,
                "new_password": "newpass12345",
                "new_password_confirm": "newpass12345",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_post_with_htmx_header_passes(
        self, logged_in_client: AsyncClient
    ) -> None:
        """带 X-Requested-With: XMLHttpRequest → 通过 CSRF（业务层 303/400 均可）。"""
        resp = await logged_in_client.post(
            "/settings/account",
            data={
                "current_password": PASSWORD,
                "new_password": "newpass12345",
                "new_password_confirm": "newpass12345",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code != 403, "HTMX 头应通过 CSRF"

    async def test_post_with_same_origin_referer_passes(
        self, logged_in_client: AsyncClient
    ) -> None:
        """同源 Referer → 通过 CSRF。"""
        resp = await logged_in_client.post(
            "/logout",
            headers={"Referer": "http://test/settings/account"},
            follow_redirects=False,
        )
        assert resp.status_code != 403, "同源 referer 应通过 CSRF"

    async def test_post_with_cross_origin_referer_blocked(
        self, logged_in_client: AsyncClient
    ) -> None:
        """跨源 Referer → 403。"""
        resp = await logged_in_client.post(
            "/logout",
            headers={"Referer": "http://evil.com/steal"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_login_post_not_blocked_by_csrf(
        self, anon_client: AsyncClient
    ) -> None:
        """POST /login 未登录不带任何特殊头 → 不被 CSRF 拦（业务层 401 密码错）。"""
        resp = await anon_client.post(
            "/login", data={"password": "wrong"}, follow_redirects=False
        )
        # 应是 401（密码错），不是 403（CSRF）
        assert resp.status_code == 401

    async def test_setup_post_not_blocked_by_csrf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /setup 未登录不带任何特殊头 → 不被 CSRF 拦。"""
        # 未 setup 环境
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/setup",
                data={"password": "test12345", "password_confirm": "test12345"},
                follow_redirects=False,
            )
            # 应是 303（成功），不是 403
            assert resp.status_code == 303
