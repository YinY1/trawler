"""Tests for platform credential routes (/auth, /auth/qr, /auth/poll).

Web 站点访问鉴权引入后，这些路由受 login_guard 保护，测试需先登录。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """已 setup + 已登录的 client（适配 login_guard）。"""
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestAuth:
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page(self, mock_load, client: AsyncClient) -> None:
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        resp = await client.get("/auth")
        assert resp.status_code == 200

    @patch("web.routes.auth.get_authenticator")
    async def test_auth_qr_returns_png(self, mock_get_auth, client: AsyncClient) -> None:
        mock_auth = MagicMock()
        mock_auth.generate_qr_code = AsyncMock(return_value=MagicMock(qr_url="https://example.com/qr", qr_key="key1"))
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth
        resp = await client.get("/auth/qr/bili")
        assert resp.status_code == 200
        assert "image/png" in resp.headers.get("content-type", "")

    @patch("web.routes.auth.get_authenticator")
    async def test_auth_poll(self, mock_get_auth, client: AsyncClient) -> None:
        from shared.auth.base import AuthStatus, QRStatus

        mock_auth = MagicMock()
        # generate_qr_code is called by the /auth/qr setup step (must be awaitable)
        mock_auth.generate_qr_code = AsyncMock(return_value=MagicMock(qr_url="https://example.com/qr", qr_key="key1"))
        mock_auth.poll_qr_status = AsyncMock(
            return_value=AuthStatus(success=False, status=QRStatus.WAITING, message="waiting")
        )
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth
        # Need a QR session first
        await client.get("/auth/qr/bili")
        resp = await client.get("/auth/poll/bili")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "waiting"


# ── Nickname 显示（plan §3.6）─────────────────────────────────────


class TestAuthNickname:
    """验证 /auth 页面在登录态下显示账号昵称、失败降级、登出清缓存。"""

    def _configure_logged_in_bili(self, mock_load: AsyncMock) -> None:
        """让 bili 处于登录有效态，xhs/weibo 未配置。"""
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.bilibili.auth.sessdata = "fake_sess"
        mock_load.return_value.bilibili.auth.bili_jct = "fake_jct"
        mock_load.return_value.bilibili.auth.dedeuserid = "12345"
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_shows_nickname_when_logged_in(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        self._configure_logged_in_bili(mock_load)

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="测试UP主")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth")
        assert resp.status_code == 200
        assert "测试UP主" in resp.text

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_hides_nickname_row_when_not_logged_in(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        # 所有平台未配置
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

        # get_authenticator 不应被调用（has_auth=False 时跳过 _fetch_nickname）
        mock_get_auth.return_value = MagicMock()

        resp = await client.get("/auth")
        assert resp.status_code == 200
        assert "账号:" not in resp.text

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_nickname_failure_falls_back_gracefully(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        self._configure_logged_in_bili(mock_load)

        # authenticator.get_user_nickname 抛异常 → _fetch_nickname 应捕获、降级 None
        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(side_effect=RuntimeError("boom"))
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth")
        assert resp.status_code == 200
        # 异常被吞，nickname 为 None → 不应出现 "账号:" 行
        assert "账号:" not in resp.text

    @patch("web.routes.auth.clear_auth_section", new_callable=AsyncMock)
    async def test_auth_logout_clears_nickname_cache(
        self,
        mock_clear: AsyncMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        # 预置缓存项，登出后应被清理
        _nickname_cache["bili"] = ("测试UP主", 0.0)
        mock_clear.return_value = True

        resp = await client.post(
            "/auth/logout/bili",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "message": "已注销"}
        assert "bili" not in _nickname_cache

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_nickname_cached_within_ttl(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """二次访问命中缓存，authenticator 不应被第二次调用。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        self._configure_logged_in_bili(mock_load)

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="测试UP主")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        await client.get("/auth")
        await client.get("/auth")

        # 缓存命中：get_user_nickname 只应被调用一次
        assert mock_auth.get_user_nickname.await_count == 1


# ── _fetch_nickname timeout 保护（plan Task 1）────────────────────


class TestFetchNicknameTimeout:
    """验证 _fetch_nickname 有 timeout 保护，慢 authenticator 不会阻塞调用方。"""

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_fetch_nickname_returns_none_on_slow_authenticator(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,  # noqa: ARG002
    ) -> None:
        import asyncio as _asyncio
        import time as _time

        from web.routes.auth import _fetch_nickname, _nickname_cache

        _nickname_cache.clear()
        # bili 登录有效
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.bilibili.auth.sessdata = "fake"
        mock_load.return_value.bilibili.auth.bili_jct = "fake"
        mock_load.return_value.bilibili.auth.dedeuserid = "12345"
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

        async def slow_nickname(_tokens):  # noqa: ANN001
            await _asyncio.sleep(10.0)
            return "should_not_reach"

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = slow_nickname
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        # 外层 wait_for 4s 保护测试本身不挂 10s。
        # 内部 _fetch_nickname: nickname 3s timeout 触发 → 返回 None，
        # close 是快速 AsyncMock 立即返回。总耗时约 3.0s，外层 4s 给 1s 余量。
        t0 = _time.monotonic()
        nick = await _asyncio.wait_for(
            _fetch_nickname(mock_load.return_value, "bili"), timeout=4.0
        )
        elapsed = _time.monotonic() - t0
        assert nick is None  # 内部 timeout=3 触发 → 返回 None
        # wall time 应在 [2.9, 3.5]：略大于 3s（timeout 触发 + close AsyncMock 开销）
        assert 2.9 <= elapsed <= 3.5, f"timeout 未在预期区间触发，实际 {elapsed:.2f}s"
