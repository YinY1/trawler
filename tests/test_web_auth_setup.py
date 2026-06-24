"""Tests for /setup route (首次设密码流程).

TDD: 先写测试，路由在 Task 4 实现后应全部 pass。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app

# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
async def setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """fresh app + AUTH_TOML_PATH 指向 tmp。

    每个 test 独立 app 实例（per 方案 A），避免 module-level singleton 的
    SessionMiddleware secret_key 缓存问题。
    """
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── cases ───────────────────────────────────────────────────────


class TestSetup:
    async def test_setup_page_returns_200_when_not_setup(self, setup_client: AsyncClient) -> None:
        resp = await setup_client.get("/setup")
        assert resp.status_code == 200
        assert "首次设置密码" in resp.text or "创建管理员密码" in resp.text

    async def test_setup_post_success_writes_auth_toml(self, setup_client: AsyncClient, tmp_path: Path) -> None:
        resp = await setup_client.post(
            "/setup",
            data={"password": "test12345", "password_confirm": "test12345"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        auth_toml = tmp_path / "auth.toml"
        assert auth_toml.exists()
        content = auth_toml.read_text(encoding="utf-8")
        assert "$argon2id$" in content, "auth.toml 应含 argon2id hash"
        assert "session_secret" in content
        # session_secret 是 token_urlsafe(64)，长度 >= 86
        import tomllib

        with open(auth_toml, "rb") as f:
            data = tomllib.load(f)
        assert len(data["session_secret"]) >= 40

    async def test_setup_post_password_mismatch(self, setup_client: AsyncClient, tmp_path: Path) -> None:
        resp = await setup_client.post(
            "/setup",
            data={"password": "test12345", "password_confirm": "different12345"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "不一致" in resp.text
        assert not (tmp_path / "auth.toml").exists()

    async def test_setup_post_password_too_short(self, setup_client: AsyncClient, tmp_path: Path) -> None:
        resp = await setup_client.post(
            "/setup",
            data={"password": "short", "password_confirm": "short"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "至少" in resp.text
        assert not (tmp_path / "auth.toml").exists()

    async def test_setup_page_redirects_when_already_setup(self, setup_client: AsyncClient) -> None:
        # 先完成一次 setup
        await setup_client.post(
            "/setup",
            data={"password": "test12345", "password_confirm": "test12345"},
            follow_redirects=False,
        )
        # 再 GET /setup 应被重定向
        resp = await setup_client.get("/setup", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

    async def test_setup_post_redirects_when_already_setup(self, setup_client: AsyncClient) -> None:
        # 先 setup
        await setup_client.post(
            "/setup",
            data={"password": "test12345", "password_confirm": "test12345"},
            follow_redirects=False,
        )
        # 再 POST /setup 也应被重定向，不覆盖密码
        resp = await setup_client.post(
            "/setup",
            data={"password": "newpass12345", "password_confirm": "newpass12345"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
