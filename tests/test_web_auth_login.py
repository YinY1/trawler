"""Tests for /login /logout /settings/account (Web 用户登录流程).

注意：case 8（旧 session 失效）依赖 secret 轮转后旧 cookie 无法验签。
由于 SessionMiddleware 的 secret_key 在 app 创建时固定，case 8 用两个
独立 app 实例验证：第一个 app 登录拿 cookie，改密码后用第二个 app（新
secret）+ 旧 cookie 访问，应被 login_guard 拦下。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import load_auth_config, set_password

PASSWORD = "test12345"


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def auth_toml_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """AUTH_TOML_PATH 指向 tmp，并预置一个已知密码。

    返回 tmp 下的 auth.toml 路径。
    """
    target = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", target)
    # set_password 会写盘并生成 session_secret
    set_password(PASSWORD)
    return target


@pytest.fixture
def fresh_app(auth_toml_tmp: Path):
    """已 setup 的 fresh app（middleware secret 已从 auth.toml 读取）。"""
    return create_app()


@pytest.fixture
async def client(fresh_app) -> AsyncClient:
    """未登录 client（已 setup，访问受保护路由会被 login_guard 拦下）。"""
    transport = ASGITransport(app=fresh_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def logged_in_client(fresh_app) -> AsyncClient:
    """已登录 client：通过 HTTP /login 拿 session cookie。"""
    transport = ASGITransport(app=fresh_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/login",
            data={"password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"login setup failed: {resp.status_code} {resp.text}"
        yield c


# ── /login ──────────────────────────────────────────────────────


class TestLogin:
    async def test_login_page_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert "password" in resp.text.lower()

    async def test_login_success_sets_session(self, logged_in_client: AsyncClient) -> None:
        # logged_in_client fixture 已经登录成功；验证受保护路由可访问
        resp = await logged_in_client.get("/", follow_redirects=False)
        assert resp.status_code != 302 or "/login" not in resp.headers.get("location", "")

    async def test_login_wrong_password(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/login",
            data={"password": "wrong-password"},
            follow_redirects=False,
        )
        assert resp.status_code == 401
        assert "密码错误" in resp.text
        # 不应设 session cookie（响应不应含 set-cookie: trawler_session=非空）
        set_cookie = resp.headers.get("set-cookie", "")
        assert "trawler_session=" not in set_cookie or "trawler_session=;" in set_cookie


# ── /logout ─────────────────────────────────────────────────────


class TestLogout:
    async def test_logout_clears_session(self, logged_in_client: AsyncClient) -> None:
        resp = await logged_in_client.post(
            "/logout",
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        # 登出后再访问受保护路由应被拦
        # 注意：client 已持有登出响应的 set-cookie（清空 session），
        # 但 httpx 不会自动更新已有 cookie。手动清：
        logged_in_client.cookies.clear()
        resp2 = await logged_in_client.get("/", follow_redirects=False)
        assert resp2.status_code == 302
        assert resp2.headers["location"].startswith("/login")


# ── /settings/account ───────────────────────────────────────────


class TestAccount:
    async def test_account_page_requires_login(self, client: AsyncClient) -> None:
        resp = await client.get("/settings/account", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")

    async def test_account_change_password_success(self, logged_in_client: AsyncClient, auth_toml_tmp: Path) -> None:
        old_cfg = load_auth_config()
        old_hash = old_cfg.admin_password_hash
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
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        # hash 应已变化
        new_cfg = load_auth_config()
        assert new_cfg.admin_password_hash != old_hash
        # session_secret 应已轮转
        assert new_cfg.session_secret != old_cfg.session_secret

    async def test_account_change_password_wrong_current(self, logged_in_client: AsyncClient) -> None:
        resp = await logged_in_client.post(
            "/settings/account",
            data={
                "current_password": "wrong-current-pw",
                "new_password": "newpass12345",
                "new_password_confirm": "newpass12345",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "当前密码错误" in resp.text

    async def test_account_change_password_invalidates_old_session(
        self, fresh_app, auth_toml_tmp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1. 用 fresh_app 登录拿 cookie
        transport1 = ASGITransport(app=fresh_app)
        async with AsyncClient(transport=transport1, base_url="http://test") as c1:
            resp = await c1.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            assert resp.status_code == 303
            # 提取 session cookie
            session_cookie = c1.cookies.get("trawler_session")
            assert session_cookie, "login should set trawler_session cookie"

        # 2. 改密码（轮转 session_secret）— 用 set_password 直接调，绕过 HTTP
        set_password("newpass12345")

        # 3. 新建 app（新 secret_key），把旧 cookie 喂给它
        new_app = create_app()
        transport2 = ASGITransport(app=new_app)
        async with AsyncClient(transport=transport2, base_url="http://test") as c2:
            c2.cookies.set("trawler_session", session_cookie)
            resp2 = await c2.get("/", follow_redirects=False)
            # 旧 cookie 在新 secret 下无法验签 → session 为空 → login_guard 拦下
            assert resp2.status_code == 302
            assert resp2.headers["location"].startswith("/login")
