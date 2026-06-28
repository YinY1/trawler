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
    """验证 /auth 页面骨架渲染不再阻塞于 nickname 拉取。

    新契约（D1+D3）：
    - GET /auth 不再调用任何 authenticator，nickname 字段恒为 None
    - nickname 由前端通过 GET /auth/nicknames 异步拉取（见 TestAuthNicknamesEndpoint）
    """

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
    async def test_auth_page_does_not_call_authenticator(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """骨架路由必须不触发任何 authenticator.get_user_nickname 调用。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        self._configure_logged_in_bili(mock_load)

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="测试UP主")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth")
        assert resp.status_code == 200
        # 骨架不拉 nickname
        assert mock_auth.get_user_nickname.await_count == 0
        # 但登录卡片的 nickname slot 应该存在（供前端填充）
        assert "nickname-slot" in resp.text or "加载中" in resp.text

    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_unconfigured_has_no_nickname_slot(
        self,
        mock_load: AsyncMock,
        client: AsyncClient,
    ) -> None:
        """未配置平台不应渲染 nickname slot（与 _auth_card.html has_auth 一致）。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

        resp = await client.get("/auth")
        assert resp.status_code == 200
        assert "账号:" not in resp.text  # 未配置不显示账号行

    @patch("web.routes.auth.clear_auth_section", new_callable=AsyncMock)
    async def test_auth_logout_clears_nickname_cache(
        self,
        mock_clear: AsyncMock,
        client: AsyncClient,
    ) -> None:
        """登出仍应清 nickname 缓存（避免下次拉到旧账号名）。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache["bili"] = ("测试UP主", 0.0)
        mock_clear.return_value = True

        resp = await client.post(
            "/auth/logout/bili",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "message": "已注销"}
        assert "bili" not in _nickname_cache


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


# ── /auth/nicknames 批量并行接口（plan Task 2）────────────────────


class TestAuthNicknamesEndpoint:
    """验证 GET /auth/nicknames 批量并行返回 nickname。"""

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_returns_dict_for_all_platforms(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        # 三个平台都登录
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"  # bili 需要
            cfg_section.auth.cookie = "SUB=fake"  # weibo 需要

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(
            side_effect=lambda _t: {"bilibili": "B站UP", "xhs": "小红书博主", "weibo": "微博用户"}[_t.platform]
        )
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth/nicknames")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"bili", "xhs", "weibo"}
        assert data["bili"] == "B站UP"
        assert data["xhs"] == "小红书博主"
        assert data["weibo"] == "微博用户"

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_parallel_not_sequential(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """三个平台每个 sleep 0.3s，并行应在 <0.7s 完成（串行需 0.9s+）。"""
        import asyncio as _asyncio
        import time as _time

        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        async def slow_nick(_t):  # noqa: ANN001
            await _asyncio.sleep(0.3)
            return "name"

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = slow_nick
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        start = _time.monotonic()
        resp = await client.get("/auth/nicknames")
        elapsed = _time.monotonic() - start

        assert resp.status_code == 200
        # 并行：~0.3s + overhead；串行会 0.9s+。给 0.7s 上限留 buffer。
        assert elapsed < 0.7, f"并行未生效，耗时 {elapsed:.2f}s"

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_unconfigured_platform_returns_none(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """全部未配置时走 _fetch_all_nicknames 早返回路径（无 gather）。

        与并行/timeout 测试分工：本测试不覆盖 gather 分支。
        """
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        # 全部未配置
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_get_auth.return_value = MagicMock()

        resp = await client.get("/auth/nicknames")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"bili": None, "xhs": None, "weibo": None}

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_slow_platform_does_not_block_others(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """AC4：bili 卡 10s 时 /auth/nicknames 仍能在可接受时间内返回。

        bili 走 _fetch_nickname 内部 3s nickname timeout + 2s close timeout 降级 None；
        xhs/weibo 立即返回真实 nickname。

        elapsed 上限 < 6.0s：覆盖 3s(nickname timeout) + 2s(close timeout) + 1s overhead。
        （真实环境 close 也可能卡，故取 6s 而非 plan 原描述的 5s 最坏情况。）
        """
        import asyncio as _asyncio
        import time as _time

        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        # 每个平台返回不同 nickname，便于断言哪个被降级。
        # bili sleep 10s → 必然触发 3s timeout；xhs/weibo 立即返回。
        # 注意 PlatformTokens.platform 值：bili→"bilibili", xhs→"xhs", weibo→"weibo"
        async def nickname_dispatch(_t):  # noqa: ANN001
            if _t.platform == "bilibili":
                await _asyncio.sleep(10.0)
                return "should_not_reach"
            return {"xhs": "小红书博主", "weibo": "微博用户"}[_t.platform]

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = nickname_dispatch
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        start = _time.monotonic()
        resp = await client.get("/auth/nicknames")
        elapsed = _time.monotonic() - start

        assert resp.status_code == 200
        # AC4：bili 卡 10s 时整体 < 6s（3s nickname timeout + 2s close timeout + buffer）
        assert elapsed < 6.0, f"慢平台未隔离，耗时 {elapsed:.2f}s"
        data = resp.json()
        # bili 被 wait_for 3s timeout 降级 None；其他正常
        assert data == {"bili": None, "xhs": "小红书博主", "weibo": "微博用户"}

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_returns_none_on_runtime_error(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """覆盖原 test_auth_page_nickname_failure_falls_back_gracefully 契约：
        authenticator.get_user_nickname 抛 RuntimeError 时，/auth/nicknames
        对该平台返回 None（不影响其他平台）。
        """
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        async def nickname_dispatch(_t):  # noqa: ANN001
            if _t.platform == "bilibili":
                raise RuntimeError("bili authenticator exploded")
            return {"xhs": "小红书博主", "weibo": "微博用户"}[_t.platform]

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = nickname_dispatch
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth/nicknames")
        assert resp.status_code == 200
        data = resp.json()
        # bili 异常被 _fetch_nickname 吞掉降级 None；其他正常
        assert data == {"bili": None, "xhs": "小红书博主", "weibo": "微博用户"}

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_caches_within_ttl(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """覆盖原 test_auth_page_nickname_cached_within_ttl 契约：
        TTL 窗口内二次调用 /auth/nicknames 时，get_user_nickname 只调一次（缓存命中）。
        """
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="缓存测试")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        # 第一次：拉取并写缓存
        resp1 = await client.get("/auth/nicknames")
        assert resp1.status_code == 200
        assert resp1.json() == {"bili": "缓存测试", "xhs": "缓存测试", "weibo": "缓存测试"}

        first_call_count = mock_auth.get_user_nickname.await_count
        assert first_call_count == 3  # 三平台各拉一次（并行）

        # 第二次：TTL 窗口内，应命中缓存，不再调 authenticator
        resp2 = await client.get("/auth/nicknames")
        assert resp2.status_code == 200
        assert resp2.json() == {"bili": "缓存测试", "xhs": "缓存测试", "weibo": "缓存测试"}
        assert mock_auth.get_user_nickname.await_count == first_call_count  # 无新增调用
