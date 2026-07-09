"""Tests for /api/v1/check/* endpoints (T2).

学习 tests/test_web_check.py 的 fixture 风格：mock auth.toml 含一个有效 token；
httpx AsyncClient + ASGITransport；mock ``run_check_once`` /
``PipelineEngine.run_specific_messages`` 避免真跑 pipeline。

覆盖：
- ``POST /check/run``：full / manual / 409 冲突 / 422 非法参数 / 401 无 token / 202 + task_id
- ``GET /check/status``：running 状态 + idle 状态 + ``_ts`` 被 strip
- ``GET /check/stream``：无 token 401 / 带正确 token 返回 SSE
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from shared.protocols import Phase
from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
async def superuser_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """持 tokens:manage + 全部消费 scope 的 superuser client（#108 后空 scopes 无权）。

    #108 的 superuser bypass 只作用于 **ownership 检查**（has_sub_access / has_sub_write），
    不 bypass **scope 检查**（require_scopes 仍按 scope 白名单匹配）。因此 fixture
    需显式给 tokens:manage + 6 个消费 scope，让所有路由都能通过 scope 校验，
    ownership 视图的 is_superuser=True 则由 tokens:manage 触发。
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
            "subscriptions:read", "subscriptions:write",
            "messages:read", "messages:write",
            "check:read", "check:run",
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


class TestCheckRun:
    @patch("api.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("api.routes.check.load_config", new_callable=AsyncMock)
    async def test_run_full_mode_returns_202_with_task_id(
        self,
        mock_load: AsyncMock,
        mock_run: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """mode=full + body={} → 202 + task_id（uuid4 hex 格式）+ mode=full。"""
        mock_load.return_value.general.data_dir = "/tmp"
        resp = await superuser_client.post("/api/v1/check/run", json={})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "started"
        assert data["mode"] == "full"
        # task_id 是 uuid4().hex（32 位 hex）
        task_id = data["task_id"]
        assert isinstance(task_id, str)
        assert len(task_id) == 32
        assert all(c in "0123456789abcdef" for c in task_id)
        # 清理 background task 状态（避免污染后续测试）
        await asyncio.sleep(0.05)
        superuser_client._app.state.check_running = False  # type: ignore[attr-defined]

    @patch("api.routes.check.PipelineEngine")
    @patch("api.routes.check.load_config", new_callable=AsyncMock)
    async def test_run_manual_mode_with_filters(
        self,
        mock_load: AsyncMock,
        mock_engine: Any,
        superuser_client: AsyncClient,
    ) -> None:
        """mode=manual + since + title → 202 + 调用 run_specific_messages。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_engine.run_specific_messages = AsyncMock()
        with patch("api.routes.check.MessageStore") as mock_store_cls:
            mock_store_cls.return_value.query_messages.return_value = [
                SimpleNamespace(msg_id="bili:test1")
            ]
            resp = await superuser_client.post(
                "/api/v1/check/run",
                json={"mode": "manual", "since": "7d", "title": "测试"},
            )
            assert resp.status_code == 202
            data = resp.json()
            assert data["status"] == "started"
            assert data["mode"] == "manual"
            # 等待 background task 触发 run_specific_messages
            await asyncio.sleep(0.05)
            mock_engine.run_specific_messages.assert_called_once()
            kwargs = mock_engine.run_specific_messages.call_args.kwargs
            assert kwargs["msg_ids"] == ["bili:test1"]
            assert kwargs["from_phase"] is Phase.SUMMARIZED  # 默认
        superuser_client._app.state.check_running = False  # type: ignore[attr-defined]

    async def test_run_conflict_returns_409(self, superuser_client: AsyncClient) -> None:
        """已有 run 在跑（state.check_running=True）→ 409 + already_running。"""
        app = superuser_client._app  # type: ignore[attr-defined]
        app.state.check_running = True
        app.state.api_task_id = "existing-task-id"  # type: ignore[attr-defined]
        try:
            resp = await superuser_client.post("/api/v1/check/run", json={})
            assert resp.status_code == 409
            data = resp.json()
            # 扁平 shape（与 task spec 一致，非嵌套 detail）
            assert data == {"status": "already_running", "task_id": "existing-task-id"}
        finally:
            app.state.check_running = False
            app.state.api_task_id = None  # type: ignore[attr-defined]

    async def test_run_invalid_reset_phase_returns_422(
        self, superuser_client: AsyncClient
    ) -> None:
        """reset_phase="garbage" → 422（消息含未知阶段名）。"""
        resp = await superuser_client.post(
            "/api/v1/check/run",
            json={"mode": "manual", "reset_phase": "garbage", "title": "x"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "garbage" in detail

    async def test_run_invalid_since_returns_422(
        self, superuser_client: AsyncClient
    ) -> None:
        """since="garbage" → 422（消息含「无法解析」）。"""
        resp = await superuser_client.post(
            "/api/v1/check/run",
            json={"mode": "manual", "since": "garbage"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "无法解析" in detail or "解析" in detail

    async def test_run_manual_mode_without_filters_returns_422(
        self, superuser_client: AsyncClient
    ) -> None:
        """mode=manual 但无筛选参数 → 422（无意义的全量 reset 重跑）。"""
        resp = await superuser_client.post("/api/v1/check/run", json={"mode": "manual"})
        assert resp.status_code == 422

    async def test_run_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 Authorization header → 401 JSON ``{"detail": "invalid or missing token"}``，
        不是 302 redirect（验证 /api/* 中间件豁免 + require_token 兜底鉴权）。"""
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/api/v1/check/run", json={"mode": "full"})
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid or missing token"}


class TestCheckStatus:
    async def test_status_returns_current_run_state(
        self, superuser_client: AsyncClient
    ) -> None:
        """预设 state.check_running=True + log_history 一条 → GET status 返回结构 + _ts 被 strip。"""
        app = superuser_client._app  # type: ignore[attr-defined]
        app.state.check_running = True
        app.state.check_processed_count = 7
        app.state.check_started_at = time.time()
        # log_history 含 _ts 内部字段（make_log_callback 写入的格式）
        item: dict[str, Any] = {
            "type": "log",
            "message": "processing",
            "time": "12:00:00",
            "_ts": time.time(),
        }
        app.state.log_history.clear()
        app.state.log_history.append(item)
        try:
            resp = await superuser_client.get("/api/v1/check/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["running"] is True
            assert data["processed_count"] == 7
            assert data["started_at"] is not None
            # _ts 内部字段必须被 strip 掉
            assert len(data["log_history"]) == 1
            assert "_ts" not in data["log_history"][0]
            assert data["log_history"][0]["message"] == "processing"
        finally:
            app.state.check_running = False
            app.state.check_started_at = None
            app.state.log_history.clear()

    async def test_status_no_run_returns_idle(
        self, superuser_client: AsyncClient
    ) -> None:
        """state.check_running=False + started_at=None → running=False + started_at=None。"""
        app = superuser_client._app  # type: ignore[attr-defined]
        app.state.check_running = False
        app.state.check_started_at = None
        app.state.log_history.clear()
        resp = await superuser_client.get("/api/v1/check/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["started_at"] is None
        assert data["log_history"] == []

    async def test_status_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET status 无 token → 401。"""
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/check/status")
            assert resp.status_code == 401


class TestCheckStream:
    async def test_stream_requires_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 token GET stream → 401（不是 SSE 流）。"""
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/check/stream")
            assert resp.status_code == 401
            assert resp.json()["detail"] == "invalid or missing token"

    async def test_stream_returns_sse_with_token(
        self, superuser_client: AsyncClient
    ) -> None:
        """带有效 token GET stream → content-type: text/event-stream。

        复用 test_web_check.py:53-76 的 producer 模式：连接建立后注入 EOF None
        让 generator 干净退出，避免测试挂死。
        """
        app = superuser_client._app  # type: ignore[attr-defined]

        async def producer() -> None:
            await asyncio.sleep(0)
            # 等待 SSE 连接的 sub_queue 注册
            for _ in range(50):
                if app.state.subscribers:
                    break
                await asyncio.sleep(0.01)
            for sub in list(app.state.subscribers):
                sub.put_nowait(None)

        producer_task = asyncio.create_task(producer())
        try:
            async with superuser_client.stream("GET", "/api/v1/check/stream") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")
        finally:
            await producer_task


# ═══════════════════════════════════════════════════════════
# scope 校验（spec §10.2）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
async def scoped_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncClient:
    """带指定 scope 的 client。

    用法: ``client = await scoped_client(["messages:read"])``
    通过 ``request.param`` 传 scope 列表（pytest indirect）。
    """
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
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestCheckScopes:
    """check 路由 scope 校验（spec §10.2）。"""

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_run_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """token 只有 messages:read，访问 /check/run → 403。"""
        resp = await scoped_client.post(
            "/api/v1/check/run", json={"mode": "full"}
        )
        assert resp.status_code == 403
        assert "scope" in resp.json()["detail"].lower()
        assert "check:run" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["check:run"]], indirect=True
    )
    async def test_status_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """token 只有 check:run（正交），访问 /check/status → 403。

        spec §4.4: check:run / check:read 正交，不互相隐含。
        """
        resp = await scoped_client.get("/api/v1/check/status")
        assert resp.status_code == 403
        assert "check:read" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["check:run"]], indirect=True
    )
    async def test_stream_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """token 只有 check:run，访问 /check/stream → 403。"""
        resp = await scoped_client.get("/api/v1/check/stream")
        assert resp.status_code == 403
