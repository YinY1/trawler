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
async def superuser_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """持 tokens:manage + messages:read/write 的 superuser client（#108 后空 scopes 无权）。

    与 test_api_check.py 的 fixture 风格一致，但 #108 后空 scopes 不再 = 全权限，
    需显式给 tokens:manage（superuser 标识）+ messages:read/write（满足路由 scope）。
    ``c._app`` 暴露 app 实例供测试直接读/写 ``app.state``。
    """
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token(
        "super-bot",
        scopes=["tokens:manage", "messages:read", "messages:write"],
    )
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
        superuser_client: AsyncClient,
    ) -> None:
        """空 store → ``{"messages": [], "count": 0}``。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.query_messages.return_value = []
        resp = await superuser_client.get("/api/v1/messages")
        assert resp.status_code == 200
        assert resp.json() == {"messages": [], "count": 0}

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_list_with_filters_passes_kwargs(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """query 参数 title/platform/phase 正确透传给 query_messages。

        注入一条 record，断言：（1）响应序列化为 MessageOut；（2）query_messages
        收到正确的 phase 枚举与过滤参数。
        """
        mock_load.return_value.general.data_dir = "/tmp"
        rec = _make_record(msg_id="bili:BV1", phase=Phase.SUMMARIZED)
        mock_store_cls.return_value.query_messages.return_value = [rec]

        resp = await superuser_client.get(
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
        self, superuser_client: AsyncClient
    ) -> None:
        """phase="garbage" → 422。"""
        resp = await superuser_client.get("/api/v1/messages", params={"phase": "garbage"})
        assert resp.status_code == 422

    async def test_list_invalid_since_returns_422(
        self, superuser_client: AsyncClient
    ) -> None:
        """since="garbage"（既非 unix ts，也非 parse_since 格式）→ 422。"""
        resp = await superuser_client.get("/api/v1/messages", params={"since": "garbage"})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "无法解析" in detail or "解析" in detail

    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_list_since_accepts_unix_timestamp(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        superuser_client: AsyncClient,
    ) -> None:
        """since="1700000000"（纯数字）→ 直接当 unix ts，不报 422。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.query_messages.return_value = []
        resp = await superuser_client.get(
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
        superuser_client: AsyncClient,
    ) -> None:
        """since="24h"（parse_since 相对格式）→ 解析为 unix ts。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.query_messages.return_value = []
        before = int(time.time())
        resp = await superuser_client.get("/api/v1/messages", params={"since": "24h"})
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
        superuser_client: AsyncClient,
    ) -> None:
        """存在的 msg_id → MessageOut（phase/content_type 为 .name 字符串）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        rec = _make_record(msg_id="bili:BV1", phase=Phase.PUSHED)
        mock_store_cls.return_value.get_message.return_value = rec
        resp = await superuser_client.get("/api/v1/messages/bili:BV1")
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
        superuser_client: AsyncClient,
    ) -> None:
        """不存在的 msg_id → 404 ``{"detail": "message not found"}``。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.get_message.return_value = None
        resp = await superuser_client.get("/api/v1/messages/bili:nope")
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
        superuser_client: AsyncClient,
    ) -> None:
        """合法请求 → 202 + status=started + 32 位 task_id。"""
        mock_load.return_value.general.data_dir = "/tmp"
        # store.get_message 返回非 None（占锁前的「存在」检查）
        mock_store_cls.return_value.get_message.return_value = _make_record()
        mock_engine.run_specific_messages = AsyncMock()
        resp = await superuser_client.post(
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
        app = superuser_client._app  # type: ignore[attr-defined]
        app.state.check_running = False

    async def test_rerun_empty_msg_ids_returns_422(
        self, superuser_client: AsyncClient
    ) -> None:
        """msg_ids=[] → 422（min_items=1）。"""
        resp = await superuser_client.post(
            "/api/v1/messages/rerun",
            json={"msg_ids": []},
        )
        assert resp.status_code == 422

    async def test_rerun_invalid_from_phase_returns_422(
        self, superuser_client: AsyncClient
    ) -> None:
        """from_phase="garbage" → 422。"""
        resp = await superuser_client.post(
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
        superuser_client: AsyncClient,
    ) -> None:
        """state.check_running=True → 409 扁平 shape（与 /check/run 一致）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        app = superuser_client._app  # type: ignore[attr-defined]
        app.state.check_running = True
        app.state.api_task_id = "existing-task-id"  # type: ignore[attr-defined]
        try:
            resp = await superuser_client.post(
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
        superuser_client: AsyncClient,
    ) -> None:
        """rerun 完成后 state.check_running 应回到 False（finally 块释放）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_store_cls.return_value.get_message.return_value = _make_record()
        mock_engine.run_specific_messages = AsyncMock()
        app = superuser_client._app  # type: ignore[attr-defined]
        resp = await superuser_client.post(
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


# ═══════════════════════════════════════════════════════════
# ownership 矩阵（issue #108）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def tmp_config_with_owned_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写盘 config/subscriptions.toml + auth.toml + messages.json，含 ownership 矩阵数据。

    Subscriptions:
      - bili uid=100, owner_token='owner-bot', assigned_tokens=['assigned-bot']
      - bili uid=200, owner_token='' (孤儿)
      - xhs user_id='u456', owner_token='owner-bot'

    Messages (messages.json): bili:100 / bili:200 / xhs:u456 / weibo:no_sub

    Auth tokens:
      - super-bot: scopes=['tokens:manage', 'messages:read', 'messages:write']
      - owner-bot: scopes=['messages:read', 'messages:write']
      - assigned-bot: scopes=['messages:read']
      - outsider-bot: scopes=['messages:read']
    """
    import json

    from shared.config import (
        BilibiliConfig,
        BiliSubscription,
        Config,
        GeneralConfig,
        UserSubscription,
        WeiboConfig,
        XhsConfig,
    )

    # mock load_config 让 messages 路由反查到这些 sub
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
    monkeypatch.setattr("api.routes.messages.load_config", mock_load)

    # 写 messages.json（MessageStore._load 读 {"messages": {msg_id: data}}）
    now = time.time()
    messages = {
        "bili:100": {
            "platform": "bili", "content_type": ContentType.VIDEO.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "bili-100", "author": "a", "subscription_ref": "100",
            "created_at": now, "updated_at": now,
        },
        "bili:200": {
            "platform": "bili", "content_type": ContentType.VIDEO.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "bili-200", "author": "a", "subscription_ref": "200",
            "created_at": now, "updated_at": now,
        },
        "xhs:u456": {
            "platform": "xhs", "content_type": ContentType.TEXT.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "xhs-u456", "author": "a", "subscription_ref": "u456",
            "created_at": now, "updated_at": now,
        },
        "weibo:no_sub": {
            "platform": "weibo", "content_type": ContentType.TEXT.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "no-sub", "author": "a", "subscription_ref": "",
            "created_at": now, "updated_at": now,
        },
    }
    (tmp_path / "messages.json").write_text(
        json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8"
    )

    # auth.toml + tokens
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token
    create_token("super-bot", scopes=["tokens:manage", "messages:read", "messages:write"])
    create_token("owner-bot", scopes=["messages:read", "messages:write"])
    create_token("assigned-bot", scopes=["messages:read"])
    create_token("outsider-bot", scopes=["messages:read"])

    return tmp_path


@pytest.fixture
async def owner_client(
    tmp_config_with_owned_sub: Path,
) -> AsyncClient:
    """owner-bot 的 client（拥有 bili/100 + xhs/u456，不拥有 bili/200 孤儿）。"""
    from api.auth import create_token

    # tmp_config_with_owned_sub 已 create_token("owner-bot")，明文没保留。
    # 重新 create 覆盖拿明文（破坏 hash 但 ownership 字段在 auth.toml 仍匹配 name）。
    plain = create_token(
        "owner-bot", scopes=["messages:read", "messages:write"],
    )
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def assigned_client(
    tmp_config_with_owned_sub: Path,
) -> AsyncClient:
    """assigned-bot 的 client（被分配只读 bili/100）。"""
    from api.auth import create_token

    plain = create_token("assigned-bot", scopes=["messages:read"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def outsider_client(
    tmp_config_with_owned_sub: Path,
) -> AsyncClient:
    """outsider-bot 的 client（无任何 sub 关系）。"""
    from api.auth import create_token

    plain = create_token("outsider-bot", scopes=["messages:read"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestOwnershipListMessages:
    """GET /messages ownership 矩阵（issue #108）。"""

    async def test_superuser_sees_all(
        self, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 看所有消息（含无主 weibo:no_sub）。

        本测试不用模块级 ``superuser_client`` fixture（它有自己的 tmp_path，
        与 ``tmp_config_with_owned_sub`` 的 auth.toml 路径冲突）。改为依赖
        ``tmp_config_with_owned_sub`` 后本地构造 client，复用其 auth.toml。
        """
        from api.auth import create_token

        plain = create_token(
            "super-bot",
            scopes=["tokens:manage", "messages:read", "messages:write"],
        )
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plain}"},
        ) as c:
            resp = await c.get("/api/v1/messages")
        assert resp.status_code == 200
        msg_ids = {m["msg_id"] for m in resp.json()["messages"]}
        assert msg_ids == {"bili:100", "bili:200", "xhs:u456", "weibo:no_sub"}

    async def test_owner_sees_own_subs(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 看 bili/100 + xhs/u456（自己 own 的），不看孤儿 bili/200。"""
        resp = await owner_client.get("/api/v1/messages")
        assert resp.status_code == 200
        msg_ids = {m["msg_id"] for m in resp.json()["messages"]}
        assert msg_ids == {"bili:100", "xhs:u456"}

    async def test_assigned_sees_only_assigned(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """assigned-bot 只看 bili/100（被分配的），不看 xhs/u456。"""
        resp = await assigned_client.get("/api/v1/messages")
        assert resp.status_code == 200
        msg_ids = {m["msg_id"] for m in resp.json()["messages"]}
        assert msg_ids == {"bili:100"}

    async def test_outsider_sees_nothing(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider-bot 看不到任何消息。"""
        resp = await outsider_client.get("/api/v1/messages")
        assert resp.status_code == 200
        assert resp.json()["messages"] == []
        assert resp.json()["count"] == 0


class TestOwnershipGetMessage:
    """GET /messages/{msg_id} ownership 矩阵（issue #108）。"""

    async def test_owner_gets_own_msg(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        resp = await owner_client.get("/api/v1/messages/bili:100")
        assert resp.status_code == 200

    async def test_assigned_gets_assigned_msg(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        resp = await assigned_client.get("/api/v1/messages/bili:100")
        assert resp.status_code == 200

    async def test_outsider_get_404(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider 看 bili:100 → 404（不暴露存在性）。"""
        resp = await outsider_client.get("/api/v1/messages/bili:100")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "message not found"

    async def test_owner_get_orphan_msg_404(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 看不到 bili:200（孤儿 sub，非 superuser 不可见）。"""
        resp = await owner_client.get("/api/v1/messages/bili:200")
        assert resp.status_code == 404

    async def test_ownerless_msg_only_superuser(
        self, owner_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        """weibo:no_sub 无 subscription_ref，只 superuser 可见。

        owner 看 → 404。superuser 看 → 200（本地构造 client 复用 fixture auth.toml）。
        """
        from api.auth import create_token

        resp_owner = await owner_client.get("/api/v1/messages/weibo:no_sub")
        assert resp_owner.status_code == 404

        plain = create_token(
            "super-bot",
            scopes=["tokens:manage", "messages:read", "messages:write"],
        )
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plain}"},
        ) as c:
            resp_super = await c.get("/api/v1/messages/weibo:no_sub")
        assert resp_super.status_code == 200


