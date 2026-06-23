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
    """已登录 client（适配 login_guard + CSRF middleware）。"""
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c


class TestSubscriptions:
    @patch("web.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_list_page(self, mock_list, client: AsyncClient) -> None:
        mock_list.return_value = {"bilibili": [{"uid": 1, "name": "UP主"}]}
        resp = await client.get("/subscriptions")
        assert resp.status_code == 200

    @patch("web.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_add_redirects(self, mock_add, client: AsyncClient) -> None:
        mock_add.return_value = (True, "已添加")
        resp = await client.post(
            "/subscriptions/add",
            data={"platform": "bili", "identifier": "123", "name": "test"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/subscriptions?msg=")

    @patch("web.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_remove_redirects(self, mock_remove, client: AsyncClient) -> None:
        mock_remove.return_value = (True, "已删除")
        resp = await client.post(
            "/subscriptions/remove",
            data={"platform": "bili", "identifier": "123"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/subscriptions?msg=")

    @patch("web.routes.subscriptions.search_by_name", new_callable=AsyncMock)
    async def test_search_returns_html(self, mock_search, client: AsyncClient) -> None:
        mock_search.return_value = (True, "找到 1 个匹配", [{"uid": 123, "name": "UP主"}])
        resp = await client.post(
            "/subscriptions/search",
            data={"platform": "bili", "name": "UP"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert "UP主" in resp.text

    @patch("web.routes.subscriptions.search_by_name", new_callable=AsyncMock)
    async def test_search_empty(self, mock_search, client: AsyncClient) -> None:
        mock_search.return_value = (True, "未找到匹配", [])
        resp = await client.post(
            "/subscriptions/search",
            data={"platform": "bili", "name": "不存在"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert "未找到" in resp.text
