from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


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
            "/subscriptions/add", data={"platform": "bili", "identifier": "123", "name": "test"}
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/subscriptions"

    @patch("web.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_remove_redirects(self, mock_remove, client: AsyncClient) -> None:
        mock_remove.return_value = (True, "已删除")
        resp = await client.post(
            "/subscriptions/remove", data={"platform": "bili", "identifier": "123"}
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/subscriptions"

    @patch("web.routes.subscriptions.search_by_name", new_callable=AsyncMock)
    async def test_search_returns_html(self, mock_search, client: AsyncClient) -> None:
        mock_search.return_value = (True, "找到 1 个匹配", [{"uid": 123, "name": "UP主"}])
        resp = await client.post("/subscriptions/search", data={"platform": "bili", "name": "UP"})
        assert resp.status_code == 200
        assert "UP主" in resp.text

    @patch("web.routes.subscriptions.search_by_name", new_callable=AsyncMock)
    async def test_search_empty(self, mock_search, client: AsyncClient) -> None:
        mock_search.return_value = (True, "未找到匹配", [])
        resp = await client.post(
            "/subscriptions/search", data={"platform": "bili", "name": "不存在"}
        )
        assert resp.status_code == 200
        assert "未找到" in resp.text
