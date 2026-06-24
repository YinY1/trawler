"""Tests for web/routes/{endpoints,subscriptions}.py — POST 写操作走 303 重定向。

契约变更: 原 HX-Trigger toast header 模式在 ``hx-target="body"`` + 空 body 下导致
整页白屏（HTMX 把 body 替换为空字符串）。修复后端点/订阅端点 5 个 POST 路由改为
``RedirectResponse(url="/...?toast_key=<key>&type=<success|error>", status_code=303)``。
HTMX 跟随 303 整页刷新, 由前端 ``base.html`` 的 URL-flash JS 解析 query 并显示 toast。

本测试锁定新契约:
1. 写操作返回 ``303 See Other``
2. ``Location`` header 指向对应列表页 (``/endpoints`` 或 ``/subscriptions``)
3. Location query 携带 ``toast_key=<key>`` + ``type=<success|error>``
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
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestEndpointAddRedirect:
    """After fix: endpoint_add redirects to /endpoints with a toast_key
    query param so HTMX does a full-page refresh (no white screen)."""

    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_duplicate_name_redirects_with_error_toast_key(
        self, mock_save, mock_load, client: AsyncClient
    ) -> None:
        from shared.config import EndpointConfig

        mock_load.return_value = [EndpointConfig(name="ops", url="https://g", token="t")]
        resp = await client.post(
            "/endpoints/add",
            data={"name": "ops", "url": "https://x", "token": "tok"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/endpoints")
        assert "toast_key=endpoint.name_exists" in loc
        assert "type=error" in loc
        mock_save.assert_not_called()

    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_success_redirects_with_saved_toast_key(
        self, mock_save, mock_load, client: AsyncClient
    ) -> None:
        mock_load.return_value = []
        resp = await client.post(
            "/endpoints/add",
            data={"name": "new", "url": "https://x", "token": "tok"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/endpoints")
        assert "toast_key=endpoint.saved" in loc
        assert "type=success" in loc
        mock_save.assert_called_once()


class TestEndpointEditRedirect:
    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_missing_endpoint_redirects_with_error_toast_key(
        self, mock_save, mock_load, client: AsyncClient
    ) -> None:
        mock_load.return_value = []  # endpoint "ghost" not found
        resp = await client.post(
            "/endpoints/ghost/edit",
            data={"url": "https://x", "token": "tok"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/endpoints")
        assert "toast_key=endpoint.not_found" in loc
        assert "type=error" in loc
        mock_save.assert_not_called()


class TestSubscriptionEndpointAddRedirect:
    """After fix: subscription_endpoint_add redirects to /subscriptions
    with toast_key so HTMX does a full-page refresh (no white screen)."""

    @patch("web.routes.subscriptions.Path")
    async def test_add_endpoint_success_redirects_with_toast_key(
        self, mock_path_cls, client: AsyncClient
    ) -> None:
        import tomlkit

        initial_doc = tomlkit.document()
        bilibili = tomlkit.table()
        subs_aot = tomlkit.aot()
        sub_table = tomlkit.table()
        sub_table["uid"] = "25270495"
        sub_table["name"] = "测试UP"
        sub_table["notify_endpoints"] = []
        subs_aot.append(sub_table)
        bilibili["subscriptions"] = subs_aot
        initial_doc["bilibili"] = bilibili
        initial_toml = tomlkit.dumps(initial_doc)

        fake_path = mock_path_cls.return_value
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = initial_toml

        resp = await client.post(
            "/subscriptions/bili/25270495/endpoints/add",
            data={"endpoint_name": "ops"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/subscriptions")
        assert "toast_key=subscription.endpoint_added" in loc
        assert "type=success" in loc
        fake_path.write_text.assert_called_once()

    @patch("web.routes.subscriptions.Path")
    async def test_add_endpoint_subscription_not_found_redirects_with_error_toast_key(
        self, mock_path_cls, client: AsyncClient
    ) -> None:
        import tomlkit

        # Subscription list exists but the queried uid is absent
        initial_doc = tomlkit.document()
        bilibili = tomlkit.table()
        subs_aot = tomlkit.aot()
        sub_table = tomlkit.table()
        sub_table["uid"] = "99999999"  # not the queried one
        sub_table["name"] = "其他UP"
        subs_aot.append(sub_table)
        bilibili["subscriptions"] = subs_aot
        initial_doc["bilibili"] = bilibili
        initial_toml = tomlkit.dumps(initial_doc)

        fake_path = mock_path_cls.return_value
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = initial_toml

        resp = await client.post(
            "/subscriptions/bili/25270495/endpoints/add",
            data={"endpoint_name": "ops"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/subscriptions")
        assert "toast_key=subscription.not_found" in loc
        assert "type=error" in loc

    @patch("web.routes.subscriptions.Path")
    async def test_add_endpoint_file_missing_redirects_with_error_toast_key(
        self, mock_path_cls, client: AsyncClient
    ) -> None:
        """When config/subscriptions.toml does not exist, surface the
        "subscription not found" error toast (same UX as identifier mismatch)."""
        fake_path = mock_path_cls.return_value
        fake_path.exists.return_value = False
        # read_text / write_text should never be touched when the file is absent.
        fake_path.read_text.return_value = ""
        fake_path.write_text.return_value = None

        resp = await client.post(
            "/subscriptions/bili/25270495/endpoints/add",
            data={"endpoint_name": "ops"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/subscriptions")
        assert "toast_key=subscription.not_found" in loc
        assert "type=error" in loc
        fake_path.read_text.assert_not_called()
        fake_path.write_text.assert_not_called()


class TestEndpointDeleteRedirect:
    """After fix: endpoint_delete redirects to /endpoints with a toast_key
    query param so HTMX does a full-page refresh (no white screen)."""

    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_delete_redirects_with_deleted_toast_key(
        self, mock_save, mock_load, client: AsyncClient
    ) -> None:
        mock_load.return_value = []
        resp = await client.post(
            "/endpoints/ops/delete",
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/endpoints")
        assert "toast_key=endpoint.deleted" in loc
        assert "type=success" in loc
        mock_save.assert_called_once()


class TestSubscriptionEndpointRemoveRedirect:
    """After fix: subscription_endpoint_remove redirects to /subscriptions
    with a toast_key query param so HTMX does a full-page refresh (no white screen)."""

    @patch("web.routes.subscriptions.Path")
    async def test_remove_endpoint_success_redirects_with_toast_key(
        self, mock_path_cls, client: AsyncClient
    ) -> None:
        import tomlkit

        initial_doc = tomlkit.document()
        bilibili = tomlkit.table()
        subs_aot = tomlkit.aot()
        sub_table = tomlkit.table()
        sub_table["uid"] = "25270495"
        sub_table["name"] = "测试UP"
        sub_table["notify_endpoints"] = ["ops"]  # pre-assigned, will be removed
        subs_aot.append(sub_table)
        bilibili["subscriptions"] = subs_aot
        initial_doc["bilibili"] = bilibili
        initial_toml = tomlkit.dumps(initial_doc)

        fake_path = mock_path_cls.return_value
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = initial_toml

        resp = await client.post(
            "/subscriptions/bili/25270495/endpoints/remove",
            data={"endpoint_name": "ops"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/subscriptions")
        assert "toast_key=subscription.endpoint_removed" in loc
        assert "type=success" in loc
        fake_path.write_text.assert_called_once()

    @patch("web.routes.subscriptions.Path")
    async def test_remove_endpoint_subscription_not_found_redirects_with_error_toast_key(
        self, mock_path_cls, client: AsyncClient
    ) -> None:
        import tomlkit

        # Subscription list exists but the queried uid is absent
        initial_doc = tomlkit.document()
        bilibili = tomlkit.table()
        subs_aot = tomlkit.aot()
        sub_table = tomlkit.table()
        sub_table["uid"] = "99999999"  # not the queried one
        sub_table["name"] = "其他UP"
        sub_table["notify_endpoints"] = ["ops"]
        subs_aot.append(sub_table)
        bilibili["subscriptions"] = subs_aot
        initial_doc["bilibili"] = bilibili
        initial_toml = tomlkit.dumps(initial_doc)

        fake_path = mock_path_cls.return_value
        fake_path.exists.return_value = True
        fake_path.read_text.return_value = initial_toml

        resp = await client.post(
            "/subscriptions/bili/25270495/endpoints/remove",
            data={"endpoint_name": "ops"},
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert loc.startswith("/subscriptions")
        assert "toast_key=subscription.not_found" in loc
        assert "type=error" in loc
        fake_path.write_text.assert_not_called()
