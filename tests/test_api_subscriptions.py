"""Tests for /api/v1/subscriptions endpoints (T4).

学习 ``tests/test_api_check.py`` 的 ``superuser_client`` fixture 风格 +
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
async def superuser_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """持 tokens:manage 的 superuser client（#108 后空 scopes 无权）。

    附带 ``subscriptions:read`` / ``subscriptions:write`` scope：路由入口 scope 校验
    在 ownership 层之前，tokens:manage 本身不隐含 subscriptions:*。
    """
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token(
        "super-bot",
        scopes=[
            "tokens:manage",
            "subscriptions:read",
            "subscriptions:write",
        ],
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
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
        superuser_client: AsyncClient,
    ) -> None:
        """list_subscriptions 返回 ``{}`` → 200 + ``{"platforms": {}}``。"""
        mock_list.return_value = {}
        resp = await superuser_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        assert resp.json() == {"platforms": {}}
        mock_list.assert_awaited_once_with(platform=None)

    @patch("api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_list_subscriptions_with_data(
        self,
        mock_list: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """list_subscriptions 返回多平台 → 透传为 ``platforms`` 字段。"""
        mock_list.return_value = {
            "bilibili": [{"uid": 123, "name": "UP1"}],
            "xiaohongshu": [{"user_id": "abc", "name": "XHS1"}],
        }
        resp = await superuser_client.get("/api/v1/subscriptions")
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
        superuser_client: AsyncClient,
    ) -> None:
        """query ``?platform=bili`` → 透传给 list_subscriptions(platform="bili")。"""
        mock_list.return_value = {"bilibili": [{"uid": 123, "name": "UP1"}]}
        resp = await superuser_client.get("/api/v1/subscriptions?platform=bili")
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
        superuser_client: AsyncClient,
    ) -> None:
        """add_subscription 返回 ``(True, "已添加: X")`` → 200 + success=True。"""
        mock_add.return_value = (True, "已添加: UP1")
        resp = await superuser_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bili", "identifier": "123", "name": "UP1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["message"] == "已添加: UP1"
        mock_add.assert_awaited_once_with(
            "bili", "123", "UP1",
            default_notify_endpoint=None,
            owner_token="super-bot",
        )

    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_add_subscription_duplicate_returns_success_false(
        self,
        mock_add: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """重复添加 ``(False, "已存在: ...")`` → 200 + success=False（**不是** 4xx）。

        与 plan/spec 的 409 设计有意偏离：delegated prompt 明确要求业务失败
        映射成 200 + ``success=False``，调用方靠字段判断。
        """
        mock_add.return_value = (False, "已存在: UP1")
        resp = await superuser_client.post(
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
        superuser_client: AsyncClient,
    ) -> None:
        """remove_subscription 返回 ``(True, "已删除: ...")`` → 200 + success=True。"""
        mock_remove.return_value = (True, "已删除: UP1")
        resp = await superuser_client.delete("/api/v1/subscriptions/bili/123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["message"] == "已删除: UP1"
        mock_remove.assert_awaited_once_with("bili", "123")

    @patch("api.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_remove_subscription_not_found_returns_success_false(
        self,
        mock_remove: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """未找到 ``(False, "未找到: ...")`` → 200 + success=False（**不是** 404）。"""
        mock_remove.return_value = (False, "未找到: bili 平台未找到匹配的订阅")
        resp = await superuser_client.delete("/api/v1/subscriptions/bili/999")
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


# ── POST /subscriptions/{platform}/{identifier}/endpoints ─────────────


class TestBindEndpoint:
    @patch("api.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_api_bind_endpoint_ok(
        self,
        mock_bind: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """add_endpoint_to_subscription 返回 (True, "已绑定: ...") → 200 success=True。"""
        mock_bind.return_value = (True, "已绑定: gotify-main")
        resp = await superuser_client.post(
            "/api/v1/subscriptions/bilibili/123/endpoints",
            json={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "已绑定" in data["message"]
        mock_bind.assert_awaited_once_with("bili", "123", "gotify-main")

    @patch("api.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_api_bind_endpoint_unknown(
        self,
        mock_bind: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """未知 endpoint → 200 success=False（不映射 4xx）。"""
        mock_bind.return_value = (False, "未知 endpoint: bad-ep")
        resp = await superuser_client.post(
            "/api/v1/subscriptions/bilibili/123/endpoints",
            json={"endpoint_name": "bad-ep"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "未知 endpoint" in data["message"]

    @patch("api.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_api_bind_endpoint_no_sub(
        self,
        mock_bind: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """订阅不存在 → 200 success=False。"""
        mock_bind.return_value = (False, "未找到订阅")
        resp = await superuser_client.post(
            "/api/v1/subscriptions/bilibili/9999/endpoints",
            json={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "未找到订阅"

    async def test_api_bind_endpoint_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 token → 401。"""
        c = await _make_no_token_client(tmp_path, monkeypatch)
        try:
            resp = await c.post(
                "/api/v1/subscriptions/bilibili/123/endpoints",
                json={"endpoint_name": "gotify-main"},
            )
            assert resp.status_code == 401
        finally:
            await c.__aexit__(None, None, None)


# ── DELETE /subscriptions/{platform}/{identifier}/endpoints/{name} ────


class TestUnbindEndpoint:
    @patch("api.routes.subscriptions.remove_endpoint_from_subscription", new_callable=AsyncMock)
    async def test_api_unbind_endpoint_ok(
        self,
        mock_unbind: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """remove 返回 (True, "已解绑: ...") → 200 success=True。"""
        mock_unbind.return_value = (True, "已解绑: gotify-main")
        resp = await superuser_client.delete(
            "/api/v1/subscriptions/bilibili/123/endpoints/gotify-main"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "已解绑" in data["message"]
        mock_unbind.assert_awaited_once_with("bili", "123", "gotify-main")


# ── POST /subscriptions with default_notify_endpoint ─────────────────


class TestAddSubscriptionWithDefault:
    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_api_add_subscription_with_default(
        self,
        mock_add: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """请求体含 default_notify_endpoint → 透传给 add_subscription。"""
        mock_add.return_value = (True, "已添加: UP1")
        resp = await superuser_client.post(
            "/api/v1/subscriptions",
            json={
                "platform": "bili",
                "identifier": "123",
                "name": "UP1",
                "default_notify_endpoint": "gotify-main",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        # 关键：default_notify_endpoint 透传到 core
        mock_add.assert_awaited_once_with(
            "bili", "123", "UP1",
            default_notify_endpoint="gotify-main",
            owner_token="super-bot",
        )

    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_api_add_subscription_without_default_omits_kwarg(
        self,
        mock_add: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """请求体不含 default_notify_endpoint → pydantic 默认 None，
        add_subscription 仍被以关键字参数形式调用（值为 None）。"""
        mock_add.return_value = (True, "已添加: UP1")
        resp = await superuser_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bili", "identifier": "123", "name": "UP1"},
        )
        assert resp.status_code == 200
        mock_add.assert_awaited_once_with(
            "bili", "123", "UP1",
            default_notify_endpoint=None,
            owner_token="super-bot",
        )


# ── scope 校验（spec §10.2）──────────────────────────────────────────


@pytest.fixture
async def scoped_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncClient:
    """带指定 scope 的 client（与 ``tests/test_api_check.py::scoped_client`` 同模式）。"""
    scopes = request.param
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token("scoped-bot", scopes=scopes)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        yield c


class TestSubscriptionsScopes:
    """subscriptions 路由 scope 校验。"""

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_list_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.get("/api/v1/subscriptions")
        assert resp.status_code == 403
        assert "subscriptions:read" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_add_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """read 不隐含 write（spec §4.3）。"""
        resp = await scoped_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bilibili", "identifier": "123", "name": "UP"},
        )
        assert resp.status_code == 403
        assert "subscriptions:write" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_remove_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.delete(
            "/api/v1/subscriptions/bilibili/123"
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_bind_endpoint_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.post(
            "/api/v1/subscriptions/bilibili/123/endpoints",
            json={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "scoped_client", [["subscriptions:read"]], indirect=True
    )
    async def test_unbind_endpoint_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.delete(
            "/api/v1/subscriptions/bilibili/123/endpoints/gotify-main"
        )
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════
# ownership 矩阵（issue #108）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def tmp_config_with_owned_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写盘 subscriptions.toml + auth.toml + mock load_config，sub 路由共用。

    Subscriptions:
      - bili uid=100, owner_token='owner-bot', assigned_tokens=['assigned-bot']
      - bili uid=200, owner_token='' (孤儿)
      - xhs user_id='u456', owner_token='owner-bot'
    """
    from shared.config import (
        BilibiliConfig,
        BiliSubscription,
        Config,
        GeneralConfig,
        UserSubscription,
        WeiboConfig,
        XhsConfig,
    )

    fake_cfg = Config(
        general=GeneralConfig(data_dir=str(tmp_path)),
        bilibili=BilibiliConfig(subscriptions=[
            BiliSubscription(uid=100, name="UP100", owner_token="owner-bot",
                             assigned_tokens=["assigned-bot"]),
            BiliSubscription(uid=200, name="OrphanUP"),
        ]),
        xiaohongshu=XhsConfig(subscriptions=[
            UserSubscription(user_id="u456", name="XHS1", owner_token="owner-bot"),
        ]),
        weibo=WeiboConfig(),
    )
    mock_load = AsyncMock(return_value=fake_cfg)
    monkeypatch.setattr("api.routes.subscriptions.load_config", mock_load)

    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token
    create_token("super-bot", scopes=["tokens:manage", "subscriptions:read", "subscriptions:write"])
    create_token("owner-bot", scopes=["subscriptions:write", "subscriptions:read"])
    create_token("assigned-bot", scopes=["subscriptions:read", "subscriptions:write"])
    create_token("outsider-bot", scopes=["subscriptions:read", "subscriptions:write"])
    return tmp_path


@pytest.fixture
async def owner_client(tmp_config_with_owned_sub: Path) -> AsyncClient:
    """owner-bot client（重新 create 拿明文，name 不变 ownership 关系成立）。"""
    from api.auth import create_token
    plain = create_token(
        "owner-bot", scopes=["subscriptions:write", "subscriptions:read"]
    )
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def assigned_client(tmp_config_with_owned_sub: Path) -> AsyncClient:
    """assigned-bot：scope 全开（read+write），但 ownership 层 assigned 只读。

    scope 全开是为绕过 FastAPI Security scope 检查，让请求到达 ownership 层
    验证 assigned 不能写（require_write=True → has_sub_write=False）。
    """
    from api.auth import create_token
    plain = create_token("assigned-bot", scopes=["subscriptions:read", "subscriptions:write"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def outsider_client(tmp_config_with_owned_sub: Path) -> AsyncClient:
    """outsider-bot：scope 全开，但 ownership 层无任何 sub 关系。"""
    from api.auth import create_token
    plain = create_token("outsider-bot", scopes=["subscriptions:read", "subscriptions:write"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


async def _make_superuser_client_owned() -> AsyncClient:
    """在 ``tmp_config_with_owned_sub`` 上下文中构造 superuser client。

    复用 fixture 已 monkeypatch 的 AUTH_TOML_PATH，重新 create_token 拿明文。
    """
    from api.auth import create_token
    plain = create_token(
        "super-bot",
        scopes=["tokens:manage", "subscriptions:read", "subscriptions:write"],
    )
    app = create_app()
    transport = ASGITransport(app=app)
    c = AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    )
    await c.__aenter__()
    c._app = app  # type: ignore[attr-defined]
    return c


_MOCK_LIST_RETURN = {
    "bilibili": [
        {"uid": 100, "name": "UP100", "owner_token": "owner-bot",
         "assigned_tokens": ["assigned-bot"]},
        {"uid": 200, "name": "OrphanUP"},
    ],
    "xiaohongshu": [
        {"user_id": "u456", "name": "XHS1", "owner_token": "owner-bot"},
    ],
}


class TestOwnershipListSubs:
    """GET /subscriptions ownership 矩阵（issue #108）。"""

    async def test_superuser_sees_all(
        self, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 看全部 sub（含孤儿 bili/200）。"""
        c = await _make_superuser_client_owned()
        try:
            with patch(
                "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
            ) as mock_list:
                mock_list.return_value = _MOCK_LIST_RETURN
                resp = await c.get("/api/v1/subscriptions")
        finally:
            await c.__aexit__(None, None, None)
        assert resp.status_code == 200
        data = resp.json()["platforms"]
        assert len(data["bilibili"]) == 2
        assert len(data["xiaohongshu"]) == 1

    async def test_owner_sees_own(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 看 bili/100 + xhs/u456（自己 own 的），不看 bili/200 孤儿。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await owner_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()["platforms"]
        bili_uids = {s["uid"] for s in data["bilibili"]}
        assert bili_uids == {100}
        assert len(data["xiaohongshu"]) == 1

    async def test_assigned_sees_assigned_only(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """assigned-bot 只看 bili/100。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await assigned_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()["platforms"]
        bili_uids = {s["uid"] for s in data["bilibili"]}
        assert bili_uids == {100}
        assert "xiaohongshu" not in data

    async def test_outsider_sees_empty(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider-bot 看不到任何 sub → platforms={} 空 dict。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await outsider_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        assert resp.json()["platforms"] == {}


class TestOwnershipDeleteSub:
    """DELETE /subscriptions/{p}/{id} ownership 矩阵（require_write=True）。"""

    async def test_owner_deletes_own(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            mock_remove.return_value = (True, "已删除: UP100")
            resp = await owner_client.delete("/api/v1/subscriptions/bili/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_remove.assert_awaited_once()

    async def test_assigned_cannot_delete(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await assigned_client.delete("/api/v1/subscriptions/bili/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "未找到" in resp.json()["message"]
        mock_remove.assert_not_awaited()

    async def test_outsider_cannot_delete(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await outsider_client.delete("/api/v1/subscriptions/bili/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_remove.assert_not_awaited()

    async def test_owner_cannot_delete_orphan(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 删 bili/200（孤儿 sub，非自己 own）→ success=False。"""
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await owner_client.delete("/api/v1/subscriptions/bili/200")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_remove.assert_not_awaited()

    async def test_invalid_platform_returns_not_found(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """C1 修订：无效平台 → success=False（合并「未找到」语义）。"""
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await owner_client.delete("/api/v1/subscriptions/ghost/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_remove.assert_not_awaited()


class TestOwnershipAssignRoutes:
    """assign/unassign 路由 superuser 专用（issue #108 §7.6）。"""

    async def test_owner_cannot_assign_403(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner 调 assign 路由 → 403（缺 tokens:manage scope）。"""
        resp = await owner_client.post(
            "/api/v1/subscriptions/bili/100/assign",
            json={"token_name": "outsider-bot"},
        )
        assert resp.status_code == 403
        assert "scope" in resp.json()["detail"].lower()

    async def test_assigned_cannot_assign_403(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        resp = await assigned_client.post(
            "/api/v1/subscriptions/bili/100/assign",
            json={"token_name": "outsider-bot"},
        )
        assert resp.status_code == 403

    async def test_superuser_assigns_successfully(
        self, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 调 assign → 200 + success=True。"""
        c = await _make_superuser_client_owned()
        try:
            with patch(
                "api.routes.subscriptions.assign_token_to_subscription",
                new_callable=AsyncMock,
            ) as mock_assign:
                mock_assign.return_value = (True, "已分配: outsider-bot")
                resp = await c.post(
                    "/api/v1/subscriptions/bili/100/assign",
                    json={"token_name": "outsider-bot"},
                )
        finally:
            await c.__aexit__(None, None, None)
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_assign.assert_awaited_once()

    async def test_superuser_unassign_successfully(
        self, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 调 unassign → 200 + success=True（幂等）。"""
        c = await _make_superuser_client_owned()
        try:
            with patch(
                "api.routes.subscriptions.unassign_token_from_subscription",
                new_callable=AsyncMock,
            ) as mock_unassign:
                mock_unassign.return_value = (True, "已解绑: assigned-bot")
                resp = await c.delete(
                    "/api/v1/subscriptions/bili/100/assign/assigned-bot"
                )
        finally:
            await c.__aexit__(None, None, None)
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_unassign.assert_awaited_once()


class TestOwnershipBindEndpoint:
    """POST /subscriptions/{p}/{id}/endpoints ownership 矩阵（require_write=True）。

    I1 修订（issue #108 review）：原 ``TestBindEndpoint`` 只用 superuser_client，
    绕过 ownership 层。本 class mirror ``TestOwnershipDeleteSub`` 模式，覆盖
    owner / assigned（被 require_write=True 拒）/ outsider 三角色。
    """

    async def test_owner_binds_own(
        self,
        owner_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        with patch(
            "api.routes.subscriptions.add_endpoint_to_subscription",
            new_callable=AsyncMock,
        ) as mock_bind:
            mock_bind.return_value = (True, "已绑定: gotify-main")
            resp = await owner_client.post(
                "/api/v1/subscriptions/bili/100/endpoints",
                json={"endpoint_name": "gotify-main"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_bind.assert_awaited_once_with("bili", "100", "gotify-main")

    async def test_assigned_cannot_bind(
        self,
        assigned_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        """assigned 调 bind → success=False（require_write=True，assigned 只读）。"""
        with patch(
            "api.routes.subscriptions.add_endpoint_to_subscription",
            new_callable=AsyncMock,
        ) as mock_bind:
            resp = await assigned_client.post(
                "/api/v1/subscriptions/bili/100/endpoints",
                json={"endpoint_name": "gotify-main"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "未找到" in resp.json()["message"]
        mock_bind.assert_not_awaited()

    async def test_outsider_cannot_bind(
        self,
        outsider_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        with patch(
            "api.routes.subscriptions.add_endpoint_to_subscription",
            new_callable=AsyncMock,
        ) as mock_bind:
            resp = await outsider_client.post(
                "/api/v1/subscriptions/bili/100/endpoints",
                json={"endpoint_name": "gotify-main"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_bind.assert_not_awaited()


class TestOwnershipUnbindEndpoint:
    """DELETE /subscriptions/{p}/{id}/endpoints/{name} ownership 矩阵（require_write=True）。

    I1 修订（issue #108 review）：原 ``TestUnbindEndpoint`` 只用 superuser_client。
    本 class mirror ``TestOwnershipBindEndpoint`` / ``TestOwnershipDeleteSub`` 模式。
    """

    async def test_owner_unbinds_own(
        self,
        owner_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        with patch(
            "api.routes.subscriptions.remove_endpoint_from_subscription",
            new_callable=AsyncMock,
        ) as mock_unbind:
            mock_unbind.return_value = (True, "已解绑: gotify-main")
            resp = await owner_client.delete(
                "/api/v1/subscriptions/bili/100/endpoints/gotify-main"
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_unbind.assert_awaited_once_with("bili", "100", "gotify-main")

    async def test_assigned_cannot_unbind(
        self,
        assigned_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        """assigned 调 unbind → success=False（require_write=True）。"""
        with patch(
            "api.routes.subscriptions.remove_endpoint_from_subscription",
            new_callable=AsyncMock,
        ) as mock_unbind:
            resp = await assigned_client.delete(
                "/api/v1/subscriptions/bili/100/endpoints/gotify-main"
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "未找到" in resp.json()["message"]
        mock_unbind.assert_not_awaited()

    async def test_outsider_cannot_unbind(
        self,
        outsider_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        with patch(
            "api.routes.subscriptions.remove_endpoint_from_subscription",
            new_callable=AsyncMock,
        ) as mock_unbind:
            resp = await outsider_client.delete(
                "/api/v1/subscriptions/bili/100/endpoints/gotify-main"
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_unbind.assert_not_awaited()


# ── ID 校验：bili 非数字 identifier 不再 500（issue #108 e2e） ────────


class TestSubscriptionIdValidation:
    """非数字 identifier 传 bili（uid 是 int）触发 ValueError → 200 + success=False。

    历史 bug：路由层不捕获 ``_key_value`` 内部 ``int(identifier)`` 抛的
    ``ValueError``，FastAPI 兜底成 500。修复后路由层统一 try/except 返
    ``success=False, message="无效 identifier: ..."``，与其它业务失败风格一致
    （200 + success 字段，不是 RESTful 4xx）。
    """

    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_post_bili_non_numeric_id_returns_success_false(
        self,
        mock_add: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """POST /subscriptions bili + identifier="abc" → 200 success=False（不 500）。

        ``_key_value("bili", "abc")`` 内 ``int("abc")`` 抛 ValueError，
        路由层捕获后返 ``success=False, message="无效 identifier: ..."``。
        """
        mock_add.side_effect = ValueError("invalid literal for int() with base 10: 'abc'")
        resp = await superuser_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bili", "identifier": "abc", "name": "X"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "无效 identifier" in data["message"]

    @patch("api.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_delete_bili_non_numeric_id_returns_success_false(
        self,
        mock_remove: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """DELETE /subscriptions/bili/abc → 200 success=False（不 500）。

        superuser_client 是 superuser，``subscription_visible`` 直接放行；
        ``remove_subscription("bili", "abc")`` 内 ``int("abc")`` 抛 ValueError，
        路由层捕获后返 ``success=False, message="无效 identifier: ..."``。
        """
        mock_remove.side_effect = ValueError("invalid literal for int() with base 10: 'abc'")
        resp = await superuser_client.delete("/api/v1/subscriptions/bili/abc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "无效 identifier" in data["message"]


