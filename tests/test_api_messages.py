"""Tests for /api/v1/messages* endpoints (T3).

学习 tests/test_api_check.py 的 fixture 风格：mock auth.toml 含一个有效 token；
httpx AsyncClient + ASGITransport；mock ``MessageStore`` /
``PipelineEngine.run_specific_messages`` 避免真跑 pipeline。

覆盖：
- ``GET /messages``：空列表 / 过滤 / since 多格式 / phase 非法 / since 非法 / 无 token 401
- ``GET /messages/{msg_id}``：存在 / 不存在 404
- ``POST /messages/rerun``：202 + task_id / msg_ids 空 422 / from_phase 非法 422 /
  冲突 409 / 无 token 401 / 锁释放
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from shared.protocols import ContentType, MessageRecord, Phase
from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"


def _make_record(
    msg_id: str = "bili:BV1xx",
    platform: str = "bili",
    content_type: ContentType = ContentType.VIDEO,
    phase: Phase = Phase.SUMMARIZED,
    pubdate: int | None = None,
    title: str = "Test Title",
    author: str = "Test Author",
) -> MessageRecord:
    """构造一条 MessageRecord（_record_to_out 测试数据源）。"""
    now = time.time()
    return MessageRecord(
        msg_id=msg_id,
        platform=platform,
        content_type=content_type,
        phase=phase,
        pubdate=pubdate if pubdate is not None else int(now),
        title=title,
        author=author,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
async def authed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """已配置 token 的 client（带 ``Authorization: Bearer`` header）。

    与 test_api_check.py 的 fixture 完全一致，确保 token 鉴权链路统一。
    ``c._app`` 暴露 app 实例供测试直接读/写 ``app.state``。
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
        c._app = app  # type: ignore[attr-defined]
        yield c


# ═══════════════════════════════════════════════════════════
# GET /messages
# ═══════════════════════════════════════════════════════════


class TestListMessages:
    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_list_empty(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """空 store → ``{"messages": [], "count": 0}``。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.query_messages.return_value = []
        resp = await authed_client.get("/api/v1/messages")
        assert resp.status_code == 200
        assert resp.json() == {"messages": [], "count": 0}

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_list_with_filters_passes_kwargs(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """query 参数 title/platform/phase 正确透传给 query_messages。

        注入一条 record，断言：（1）响应序列化为 MessageOut；（2）query_messages
        收到正确的 phase 枚举与过滤参数。
        """
        mock_load.return_value.general.data_dir = "/tmp"
        rec = _make_record(msg_id="bili:BV1", phase=Phase.SUMMARIZED)
        mock_store_cls.return_value.query_messages.return_value = [rec]

        resp = await authed_client.get(
            "/api/v1/messages",
            params={"title": "test", "author": "au", "platform": "bili", "phase": "summarized"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        msg = data["messages"][0]
        assert msg["msg_id"] == "bili:BV1"
        assert msg["phase"] == "SUMMARIZED"  # .name，不是 .value
        assert msg["content_type"] == "VIDEO"
        # query_messages 被调用时 phase 应为枚举
        kwargs = mock_store_cls.return_value.query_messages.call_args.kwargs
        assert kwargs["title"] == "test"
        assert kwargs["author"] == "au"
        assert kwargs["platform"] == "bili"
        assert kwargs["phase"] is Phase.SUMMARIZED

    async def test_list_invalid_phase_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """phase="garbage" → 422。"""
        resp = await authed_client.get("/api/v1/messages", params={"phase": "garbage"})
        assert resp.status_code == 422

    async def test_list_invalid_since_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """since="garbage"（既非 unix ts，也非 parse_since 格式）→ 422。"""
        resp = await authed_client.get("/api/v1/messages", params={"since": "garbage"})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "无法解析" in detail or "解析" in detail

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_list_since_accepts_unix_timestamp(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """since="1700000000"（纯数字）→ 直接当 unix ts，不报 422。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.query_messages.return_value = []
        resp = await authed_client.get(
            "/api/v1/messages", params={"since": "1700000000"}
        )
        assert resp.status_code == 200
        kwargs = mock_store_cls.return_value.query_messages.call_args.kwargs
        assert kwargs["since"] == 1700000000

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_list_since_accepts_relative_format(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """since="24h"（parse_since 相对格式）→ 解析为 unix ts。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.query_messages.return_value = []
        before = int(time.time())
        resp = await authed_client.get("/api/v1/messages", params={"since": "24h"})
        assert resp.status_code == 200
        kwargs = mock_store_cls.return_value.query_messages.call_args.kwargs
        since_ts = kwargs["since"]
        assert isinstance(since_ts, int)
        # 应是「现在 - 24h」附近的值（允许 5s 漂移）
        assert before - 24 * 3600 - 5 <= since_ts <= before - 24 * 3600 + 5

    async def test_list_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /messages 无 token → 401 JSON。"""
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/messages")
            assert resp.status_code == 401
            assert resp.json() == {"detail": "invalid or missing token"}


# ═══════════════════════════════════════════════════════════
# GET /messages/{msg_id}
# ═══════════════════════════════════════════════════════════


class TestGetMessage:
    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_get_found(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """存在的 msg_id → MessageOut（phase/content_type 为 .name 字符串）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        rec = _make_record(msg_id="bili:BV1", phase=Phase.PUSHED)
        mock_store_cls.return_value.get_message.return_value = rec
        resp = await authed_client.get("/api/v1/messages/bili:BV1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["msg_id"] == "bili:BV1"
        assert data["phase"] == "PUSHED"
        assert data["content_type"] == "VIDEO"

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_get_not_found_returns_404(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """不存在的 msg_id → 404 ``{"detail": "message not found"}``。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.get_message.return_value = None
        resp = await authed_client.get("/api/v1/messages/bili:nope")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "message not found"

    async def test_get_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get("/api/v1/messages/bili:BV1")
            assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════
# POST /messages/rerun
# ═══════════════════════════════════════════════════════════


class TestRerunMessages:
    @patch("api.routes.messages.PipelineEngine")
    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_rerun_returns_202_with_task_id(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        mock_engine: Any,
        authed_client: AsyncClient,
    ) -> None:
        """合法请求 → 202 + status=started + 32 位 task_id。"""
        mock_load.return_value.general.data_dir = "/tmp"
        # store.get_message 返回非 None（占锁前的「存在」检查）
        mock_store_cls.return_value.get_message.return_value = _make_record()
        mock_engine.run_specific_messages = AsyncMock()
        resp = await authed_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": ["bili:BV1"]},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "started"
        task_id = data["task_id"]
        assert isinstance(task_id, str) and len(task_id) == 32
        # 等后台 task 触发 run_specific_messages
        await asyncio.sleep(0.05)
        mock_engine.run_specific_messages.assert_called_once()
        # 清理锁
        app = authed_client._app  # type: ignore[attr-defined]
        app.state.check_running = False

    async def test_rerun_empty_msg_ids_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """msg_ids=[] → 422（min_items=1）。"""
        resp = await authed_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": []},
        )
        assert resp.status_code == 422

    async def test_rerun_invalid_from_phase_returns_422(
        self, authed_client: AsyncClient
    ) -> None:
        """from_phase="garbage" → 422。"""
        resp = await authed_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": ["bili:BV1"], "from_phase": "garbage"},
        )
        assert resp.status_code == 422

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_rerun_conflict_returns_409(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """state.check_running=True → 409 扁平 shape（与 /check/run 一致）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        app = authed_client._app  # type: ignore[attr-defined]
        app.state.check_running = True
        app.state.api_task_id = "existing-task-id"  # type: ignore[attr-defined]
        try:
            resp = await authed_client.post(
                "/api/v1/messages/rerun",
                json={"msg_ids": ["bili:BV1"]},
            )
            assert resp.status_code == 409
            data = resp.json()
            assert data == {"status": "already_running", "task_id": "existing-task-id"}
        finally:
            app.state.check_running = False
            app.state.api_task_id = None  # type: ignore[attr-defined]

    async def test_rerun_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/messages/rerun",
                json={"msg_ids": ["bili:BV1"]},
            )
            assert resp.status_code == 401
            assert resp.json()["detail"] == "invalid or missing token"

    @patch("api.routes.messages.PipelineEngine")
    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_rerun_releases_lock_on_completion(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        mock_engine: Any,
        authed_client: AsyncClient,
    ) -> None:
        """rerun 完成后 state.check_running 应回到 False（finally 块释放）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.get_message.return_value = _make_record()
        mock_engine.run_specific_messages = AsyncMock()
        app = authed_client._app  # type: ignore[attr-defined]
        resp = await authed_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": ["bili:BV1"]},
        )
        assert resp.status_code == 202
        # 轮询直到锁释放（超时 5s）
        deadline = time.time() + 5
        while app.state.check_running and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert app.state.check_running is False, "rerun 未释放 check_running 锁"
        assert app.state.api_task_id is None


# ═══════════════════════════════════════════════════════════
# scope 校验（spec §10.2）
# ═══════════════════════════════════════════════════════════


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
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestMessagesScopes:
    """messages 路由 scope 校验。"""

    @pytest.mark.parametrize(
        "scoped_client", [["check:read"]], indirect=True
    )
    async def test_list_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.get("/api/v1/messages")
        assert resp.status_code == 403
        assert "messages:read" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["check:read"]], indirect=True
    )
    async def test_get_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        resp = await scoped_client.get("/api/v1/messages/some-id")
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_rerun_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """messages:read 不隐含 messages:write（spec §4.3）。"""
        resp = await scoped_client.post(
            "/api/v1/messages/rerun",
            json={"mode": "all"},
        )
        assert resp.status_code == 403
        assert "messages:write" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "scoped_client", [["messages:read"]], indirect=True
    )
    async def test_fetch_insufficient_scope_returns_403(
        self, scoped_client: AsyncClient
    ) -> None:
        """messages:read 不隐含 messages:write（spec §4.3）。

        fetch 路由在 messages.py:271，也要求 messages:write。
        """
        resp = await scoped_client.post(
            "/api/v1/messages/fetch",
            json={"platform": "bilibili", "msg_ids": ["123"]},
        )
        assert resp.status_code == 403
