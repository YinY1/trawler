"""Tests for web/routes/{endpoints,subscriptions}.py — HX-Trigger toast headers.

Regression: 中文 toast 消息直接塞进 ``HX-Trigger`` HTTP header 会让 starlette
的 ``init_headers`` 用 ``latin-1`` 编码时炸 ``UnicodeEncodeError``。修复后的契约是
后端只发 ASCII ``key``，前端 ``TOAST_KEY_MAP``（base.html）映射回本地化文本。

这里锁定两条契约：
1. 任意 toast 响应的 ``HX-Trigger`` header 必须是纯 ASCII（无 UnicodeEncodeError）
2. 携带的 toast payload 用 ``key`` 字段（前端期望的契约），不是 ``msg``
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


class TestEndpointAddToastHeader:
    """Bug regression: endpoint_add with duplicate name must return a
    proper ASCII-only HX-Trigger, not raise UnicodeEncodeError."""

    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_duplicate_name_returns_ascii_key_toast(self, mock_save, mock_load, client: AsyncClient) -> None:
        # Simulate an existing endpoint with the same name
        from shared.config import EndpointConfig

        mock_load.return_value = [EndpointConfig(name="ops", url="https://g", token="t")]
        resp = await client.post(
            "/endpoints/add",
            data={"name": "ops", "url": "https://x", "token": "tok"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 400
        trigger = resp.headers.get("HX-Trigger", "")
        # Header must be present and pure-ASCII (no UnicodeEncodeError in starlette)
        assert trigger, "HX-Trigger header missing"
        assert "endpoint.name_exists" in trigger
        assert '"key"' in trigger
        # Must NOT carry the legacy Chinese "msg" field
        assert "端点名称已存在" not in trigger
        mock_save.assert_not_called()


class TestEndpointEditToastHeader:
    @patch("web.routes.endpoints._load_endpoints", new_callable=AsyncMock)
    @patch("web.routes.endpoints._save_endpoints")
    async def test_missing_endpoint_returns_ascii_key_toast(self, mock_save, mock_load, client: AsyncClient) -> None:
        mock_load.return_value = []  # endpoint "ghost" not found
        resp = await client.post(
            "/endpoints/ghost/edit",
            data={"url": "https://x", "token": "tok"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 404
        trigger = resp.headers.get("HX-Trigger", "")
        assert trigger, "HX-Trigger header missing"
        assert "endpoint.not_found" in trigger
        assert '"key"' in trigger
        assert "端点不存在" not in trigger
        mock_save.assert_not_called()


class TestSubscriptionEndpointAddToastHeader:
    """Bug regression: the original report. Adding an endpoint to a
    subscription returned success (data was written) but starlette
    raised UnicodeEncodeError on the Chinese HX-Trigger → user saw
    'network error' / blank screen but refresh showed it worked."""

    @patch("web.routes.subscriptions.Path")
    async def test_add_endpoint_success_returns_ascii_key_toast(self, mock_path_cls, client: AsyncClient) -> None:
        """Subscriptions file write happens before the response is built.
        Mock the file I/O so we focus on the header contract."""
        import tomlkit

        # Fake existing subscriptions.toml content
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
        )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert trigger, "HX-Trigger header missing"
        assert "subscription.endpoint_added" in trigger
        assert '"key"' in trigger
        assert "端点已添加" not in trigger

    @patch("web.routes.subscriptions.Path")
    async def test_add_endpoint_subscription_not_found_returns_ascii_key_toast(
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
        )
        assert resp.status_code == 404
        trigger = resp.headers.get("HX-Trigger", "")
        assert trigger, "HX-Trigger header missing"
        assert "subscription.not_found" in trigger
        assert '"key"' in trigger
        assert "订阅不存在" not in trigger
