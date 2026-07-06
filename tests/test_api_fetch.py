"""Tests for ``POST /api/v1/messages/fetch`` (T9, issue #101).

⚠️ 偏离 plan：plan 写的测试用 ``fastapi.testclient.TestClient`` + monkeypatch
``api.auth.require_token``，但项目现有 API 测试（``tests/test_api_messages.py``、
``tests/test_api_check.py``）统一用 ``httpx.AsyncClient + ASGITransport`` + 真实
``auth.toml``（``set_password`` + ``create_token``）做鉴权。为保持测试栈一致、
减少 harness 分叉，本测试改用项目模式（与 ``TestRerunMessages`` 完全对称），
覆盖 plan 列出的三个 case：

- ``test_fetch_messages_success`` — 202 + status=started + task_id 非空 + fetch_count=None
- ``test_fetch_messages_empty_ids_returns_422`` — msg_ids=[] → 422
- ``test_fetch_messages_already_running_returns_409`` — check_running=True → 409 + status=already_running
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
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
    """已配置 token 的 client（与 ``tests/test_api_messages.py`` 完全一致的 fixture）。"""
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
        c._app = app  # type: ignore[attr-defined]
        yield c


# ═══════════════════════════════════════════════════════════
# POST /messages/fetch
# ═══════════════════════════════════════════════════════════


class TestFetchMessages:
    @patch("api.routes.messages.PipelineEngine")
    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_fetch_messages_success(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        mock_engine: Any,
        authed_client: AsyncClient,
    ) -> None:
        """合法请求 → 202 + status=started + 32 位 task_id + fetch_count=None。

        fetch_count 在 202 响应里恒为 None（抓取异步未跑完，实际数走 SSE done 事件）。
        """
        mock_load.return_value.general.data_dir = "/tmp"
        mock_engine.run_fetch_and_process = AsyncMock(return_value=2)
        resp = await authed_client.post(
            "/api/v1/messages/fetch",
            json={"msg_ids": ["bili:BV1xx", "xhs:note1"], "skip_push": False},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "started"
        task_id = data["task_id"]
        assert isinstance(task_id, str) and len(task_id) == 32
        # fetch_count 在 202 响应里为 None（抓取异步未跑完）
        assert data["fetch_count"] is None
        # 等后台 task 触发 run_fetch_and_process
        await asyncio.sleep(0.05)
        mock_engine.run_fetch_and_process.assert_called_once()
        # 清理锁（避免污染后续测试）
        app = authed_client._app  # type: ignore[attr-defined]
        app.state.check_running = False

    async def test_fetch_messages_empty_ids_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """msg_ids=[] → 422。"""
        resp = await authed_client.post(
            "/api/v1/messages/fetch",
            json={"msg_ids": []},
        )
        assert resp.status_code == 422

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_fetch_messages_already_running_returns_409(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """state.check_running=True → 409 扁平 shape（与 /messages/rerun 一致）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        app = authed_client._app  # type: ignore[attr-defined]
        app.state.check_running = True
        app.state.api_task_id = "existing-task-id"  # type: ignore[attr-defined]
        try:
            resp = await authed_client.post(
                "/api/v1/messages/fetch",
                json={"msg_ids": ["bili:BV1"]},
            )
            assert resp.status_code == 409
            data = resp.json()
            assert data == {"status": "already_running", "task_id": "existing-task-id"}
        finally:
            app.state.check_running = False
            app.state.api_task_id = None  # type: ignore[attr-defined]
