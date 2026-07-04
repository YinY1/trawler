"""Tests for /api/v1/subscriptions endpoints (T4).

学习 ``tests/test_api_check.py`` 的 ``authed_client`` fixture 风格 +
``tests/test_subscription_cli.py`` 的业务函数返回 ``tuple[bool, str]`` 习惯。

覆盖：
- ``GET /subscriptions``：空 / 有数据 / platform 过滤 / 401
- ``POST /subscriptions``：成功 / 重复（200 + success=False）/ 401
- ``DELETE /subscriptions/{platform}/{identifier}``：成功 / 未找到（200 + success=False）/ 401

业务函数全部 mock（``core.subscription_cli.list_subscriptions`` 等），
不触碰真 ``config/subscriptions.toml``。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def authed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """已配置 token 的 client（带 ``Authorization: Bearer`` header）。

    与 ``tests/test_api_check.py:authed_client`` 完全一致：
    - monkeypatch ``web.auth.AUTH_TOML_PATH`` 与 ``api.auth.AUTH_TOML_PATH`` 到 tmp
    - set_password 让 setup_complete=True（auth_guard 中间件需要）
    - create_token 写一个明文 token，挂到 client default header
    """
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token("test-bot")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        yield c


async def _make_no_token_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """无 Authorization header 的 client（用于 401 测试）。"""
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    c = AsyncClient(transport=transport, base_url="http://test")
    await c.__aenter__()
    return c


# ── GET /subscriptions ───────────────────────────────────────────────


class TestListSubscriptions:
    @patch("api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_list_subscriptions_empty(
        self,
        mock_list: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """list_subscriptions 返回 ``{}`` → 200 + ``{"platforms": {}}``。"""
        mock_list.return_value = {}
        resp = await authed_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        assert resp.json() == {"platforms": {}}
        mock_list.assert_awaited_once_with(platform=None)

    @patch("api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_list_subscriptions_with_data(
        self,
        mock_list: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """list_subscriptions 返回多平台 → 透传为 ``platforms`` 字段。"""
        mock_list.return_value = {
            "bilibili": [{"uid": 123, "name": "UP1"}],
            "xiaohongshu": [{"user_id": "abc", "name": "XHS1"}],
        }
        resp = await authed_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["platforms"]["bilibili"] == [{"uid": 123, "name": "UP1"}]
        assert data["platforms"]["xiaohongshu"] == [
            {"user_id": "abc", "name": "XHS1"}
        ]

    @patch("api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_list_subscriptions_filter_by_platform(
        self,
        mock_list: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """query ``?platform=bili`` → 透传给 list_subscriptions(platform="bili")。"""
        mock_list.return_value = {"bilibili": [{"uid": 123, "name": "UP1"}]}
        resp = await authed_client.get("/api/v1/subscriptions?platform=bili")
        assert resp.status_code == 200
        mock_list.assert_awaited_once_with(platform="bili")

    async def test_list_subscriptions_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 token → 401 + JSON detail（不是 302 redirect）。"""
        c = await _make_no_token_client(tmp_path, monkeypatch)
        try:
            resp = await c.get("/api/v1/subscriptions")
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid or missing token"}
        finally:
            await c.__aexit__(None, None, None)


# ── POST /subscriptions ──────────────────────────────────────────────


class TestAddSubscription:
    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_add_subscription_success(
        self,
        mock_add: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """add_subscription 返回 ``(True, "已添加: X")`` → 200 + success=True。"""
        mock_add.return_value = (True, "已添加: UP1")
        resp = await authed_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bili", "identifier": "123", "name": "UP1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["message"] == "已添加: UP1"
        mock_add.assert_awaited_once_with("bili", "123", "UP1")

    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_add_subscription_duplicate_returns_success_false(
        self,
        mock_add: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """重复添加 ``(False, "已存在: ...")`` → 200 + success=False（**不是** 4xx）。

        与 plan/spec 的 409 设计有意偏离：delegated prompt 明确要求业务失败
        映射成 200 + ``success=False``，调用方靠字段判断。
        """
        mock_add.return_value = (False, "已存在: UP1")
        resp = await authed_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bili", "identifier": "123", "name": "UP1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "已存在: UP1"

    async def test_add_subscription_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 token POST → 401。"""
        c = await _make_no_token_client(tmp_path, monkeypatch)
        try:
            resp = await c.post(
                "/api/v1/subscriptions",
                json={"platform": "bili", "identifier": "123", "name": "UP1"},
            )
            assert resp.status_code == 401
        finally:
            await c.__aexit__(None, None, None)


# ── DELETE /subscriptions/{platform}/{identifier} ────────────────────


class TestRemoveSubscription:
    @patch("api.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_remove_subscription_success(
        self,
        mock_remove: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """remove_subscription 返回 ``(True, "已删除: ...")`` → 200 + success=True。"""
        mock_remove.return_value = (True, "已删除: UP1")
        resp = await authed_client.delete("/api/v1/subscriptions/bili/123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["message"] == "已删除: UP1"
        mock_remove.assert_awaited_once_with("bili", "123")

    @patch("api.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_remove_subscription_not_found_returns_success_false(
        self,
        mock_remove: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """未找到 ``(False, "未找到: ...")`` → 200 + success=False（**不是** 404）。"""
        mock_remove.return_value = (False, "未找到: bili 平台未找到匹配的订阅")
        resp = await authed_client.delete("/api/v1/subscriptions/bili/999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "未找到" in data["message"]

    async def test_remove_subscription_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 token DELETE → 401。"""
        c = await _make_no_token_client(tmp_path, monkeypatch)
        try:
            resp = await c.delete("/api/v1/subscriptions/bili/123")
            assert resp.status_code == 401
        finally:
            await c.__aexit__(None, None, None)
