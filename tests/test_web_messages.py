"""测试 POST /messages/{msg_id}/retry — 永久失败消息的重试入口 (issue #48)。

锁定契约：
1. msg_id 存在 → reset_specific 被调用，error/retry_count 清零，返回 200 + HTMX 片段。
2. msg_id 不存在 → 404。
3. 返回的 HTML 片段含可识别文本（成功/失败两种状态），HTMX 会用它替换按钮区。
4. HX-Trigger 头携带 toast key（ASCII，latin-1 安全）；中文文案由 base.html
   的 TOAST_KEY_MAP 翻译。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from shared.config import GeneralConfig
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase
from web.app import create_app
from web.auth import set_password

PASSWORD = "test12345"
MSG_ID = "bili:BV1test123"
ERROR_MSG = "transcribe 阶段失败：模型超时"


def _seed_failed_message(data_dir: Path) -> MessageStore:
    """在 tmp data_dir 下种入一条永久失败的消息。

    模拟 cron 多次重试后 mark_error 的场景：phase=SUMMARIZED, error 非空,
    retry_count 已达上限。
    """
    store = MessageStore(data_dir)
    store.add_new(
        msg_id=MSG_ID,
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=int(__import__("time").time()),
        title="测试视频",
        author="测试UP",
    )
    store.mark_phase(MSG_ID, Phase.SUMMARIZED)
    store.mark_retry_failure(MSG_ID, "first attempt failed")
    store.mark_retry_failure(MSG_ID, "second attempt failed")
    store.mark_error(MSG_ID, ERROR_MSG)
    store.save()
    return store


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, Path]]:
    """已登录 client + 已种入失败消息的 data_dir。

    返回 (client, data_dir) 元组，测试可继续读 store 验证 reset 效果。
    """
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
    set_password(PASSWORD)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _seed_failed_message(data_dir)

    # dashboard/messages 路由都从 load_config 读 data_dir
    mock_general = GeneralConfig(data_dir=str(data_dir))

    async def _fake_load() -> AsyncMock:
        cfg = AsyncMock()
        cfg.general = mock_general
        # dashboard 路由还会读 bili/xhs/weibo auth.expires_at
        cfg.bilibili.auth.expires_at = 0.0
        cfg.xiaohongshu.auth.expires_at = 0.0
        cfg.weibo.auth.expires_at = 0.0
        return cfg

    # patch 两处的 load_config（dashboard + messages）
    monkeypatch.setattr("web.routes.messages.load_config", _fake_load)
    monkeypatch.setattr("web.routes.dashboard.load_config", _fake_load)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
        assert resp.status_code == 303
        yield c, data_dir


class TestRetryMessageSuccess:
    """成功路径：reset_specific 被调用，返回确认片段，store 已清零。"""

    async def test_returns_200_with_confirmation_html(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        # URL 编码的 msg_id（含冒号），FastAPI 自动 decode
        resp = await c.post(
            f"/messages/{MSG_ID.replace(':', '%3A')}/retry",
            headers={"X-Requested-With": "XMLHttpRequest"},  # 过 CSRF
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # 确认片段含可识别文案
        assert "已重置" in resp.text
        assert "cron" in resp.text

    async def test_emits_success_toast_via_hx_trigger(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        resp = await c.post(
            f"/messages/{MSG_ID.replace(':', '%3A')}/retry",
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        trigger = resp.headers.get("HX-Trigger", "")
        # toast key 必须是 ASCII（HTTP header 是 latin-1），中文文案由 base.html
        # 的 TOAST_KEY_MAP 翻译
        assert '"type": "success"' in trigger
        assert '"key": "message.retry_success"' in trigger

    async def test_resets_store_error_and_retry_count(self, client: tuple[AsyncClient, Path]) -> None:
        c, data_dir = client
        resp = await c.post(
            f"/messages/{MSG_ID.replace(':', '%3A')}/retry",
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 200

        # 重新读 store 验证 reset 效果（reset_specific 内部 save）
        store = MessageStore(data_dir)
        msg = store.get_message(MSG_ID)
        assert msg is not None
        assert msg.error == ""
        assert msg.retry_count == 0
        assert msg.last_error == ""
        # phase 不变（reset 到当前 phase）
        assert msg.phase == Phase.SUMMARIZED


class TestRetryMessageNotFound:
    """404 路径：msg_id 在 store 中不存在。"""

    async def test_returns_404_for_unknown_msg_id(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        resp = await c.post(
            "/messages/bili:BVnonexistent/retry",
            headers={"X-Requested-With": "XMLHttpRequest"},
            follow_redirects=False,
        )
        assert resp.status_code == 404


class TestRetryMessageRequiresAuth:
    """未登录请求被 login_guard 拦截到 /login。"""

    async def test_unauthenticated_redirected_to_login(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 已 setup（有 admin_password_hash）但未登录
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/messages/bili:BV1/retry",
                headers={"X-Requested-With": "XMLHttpRequest"},
                follow_redirects=False,
            )
            # 未登录 → 302 到 /login?next=...
            assert resp.status_code == 302
            assert "/login" in resp.headers["location"]


class TestRetryButtonRenderedInDashboard:
    """Dashboard 详情面板在 msg.error 时渲染重试按钮（端到端 smoke）。"""

    async def test_dashboard_renders_retry_button_for_failed_msg(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        with patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = {"bilibili": [{"uid": 1, "name": "t"}]}
            resp = await c.get("/")
        assert resp.status_code == 200
        # 重试按钮文案 + HTMX 属性
        assert "重试此消息" in resp.text
        assert 'hx-post="/messages/bili%3ABV1test123/retry"' in resp.text
        assert "hx-target" in resp.text


class TestBatchReprocess:
    """POST /messages/batch-reprocess — 批量多选重跑 (issue #71)。

    锁定契约：
    1. 接收 msg_ids 列表 + 可选 reset_phase/skip_push，调 run_specific_messages。
    2. 返回 JSON {"status": "started"}（异步触发，前端轮询或 SSE 监听）。
    3. 无 msg_ids → 400。
    4. reset_phase 缺省 → SUMMARIZED。
    """

    async def test_batch_reprocess_calls_run_specific(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        # patch PipelineEngine.run_specific_messages 避免真实执行
        with patch("web.routes.messages.PipelineEngine") as mock_engine:
            mock_engine.run_specific_messages = AsyncMock()

            resp = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={
                    "msg_ids": "bili:BV1test123,xhs:N1",
                    "reset_phase": "summarized",
                    "skip_push": "on",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "started"

            # 等待 background task 执行
            await asyncio.sleep(0.1)
            mock_engine.run_specific_messages.assert_called_once()
            call_kwargs = mock_engine.run_specific_messages.call_args.kwargs
            assert call_kwargs["msg_ids"] == ["bili:BV1test123", "xhs:N1"]
            assert call_kwargs["from_phase"] == Phase.SUMMARIZED
            assert call_kwargs["skip_push"] is True

    async def test_batch_reprocess_default_reset_phase(self, client: tuple[AsyncClient, Path]) -> None:
        """不传 reset_phase 时默认 SUMMARIZED。"""
        c, _ = client
        with patch("web.routes.messages.PipelineEngine") as mock_engine:
            mock_engine.run_specific_messages = AsyncMock()

            resp = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"msg_ids": "bili:BV1test123"},
                follow_redirects=False,
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.1)
            call_kwargs = mock_engine.run_specific_messages.call_args.kwargs
            assert call_kwargs["from_phase"] == Phase.SUMMARIZED
            assert call_kwargs["skip_push"] is False  # 未勾选 → False（默认允许重推）

    async def test_batch_reprocess_empty_msg_ids_returns_400(self, client: tuple[AsyncClient, Path]) -> None:
        """空 msg_ids 列表 → 400。"""
        c, _ = client
        resp = await c.post(
            "/messages/batch-reprocess",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={"msg_ids": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_batch_reprocess_requires_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """未登录 → 302 到 /login。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"msg_ids": "bili:BV1"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert "/login" in resp.headers["location"]

    async def test_batch_reprocess_invalid_phase_returns_400(self, client: tuple[AsyncClient, Path]) -> None:
        """非法 reset_phase → 400（与 /check/run 同步 fast-fail 对齐）。"""
        c, _ = client
        resp = await c.post(
            "/messages/batch-reprocess",
            headers={"X-Requested-With": "XMLHttpRequest"},
            data={"msg_ids": "bili:BV1", "reset_phase": "NONEXISTENT"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "NONEXISTENT" in resp.text

    async def test_batch_reprocess_returns_409_when_already_running(self, client: tuple[AsyncClient, Path]) -> None:
        """已有检查在跑（state.check_running=True）→ 409。

        通过先发一次合法请求占锁（background task 持有锁），立即第二次发请求，
        应撞 409 互斥。
        """
        c, _ = client
        with patch("web.routes.messages.PipelineEngine") as mock_engine:
            # 让 background task 慢一点，确保第二次 POST 时锁还被持有
            async def _slow(*args: Any, **kwargs: Any) -> None:
                await asyncio.sleep(0.3)

            mock_engine.run_specific_messages = _slow

            first = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"msg_ids": "bili:BV1test123"},
                follow_redirects=False,
            )
            assert first.status_code == 200
            assert first.json()["status"] == "started"

            # 锁已被第一次请求占用，第二次应被 409 拒绝
            second = await c.post(
                "/messages/batch-reprocess",
                headers={"X-Requested-With": "XMLHttpRequest"},
                data={"msg_ids": "xhs:N1"},
                follow_redirects=False,
            )
            assert second.status_code == 409

            # 等待第一次的 background task 完成，释放锁
            await asyncio.sleep(0.5)


class TestBatchReprocessCheckboxRendered:
    """Dashboard 消息表格渲染 checkbox 多选 + 批量按钮（issue #71 smoke）。

    验证：
    1. 每行 checkbox name="msg_id" value="{msg_id}"。
    2. 表头全选 checkbox id="select-all"。
    3. 批量按钮"重跑选中"渲染。
    4. form action 指向 /messages/batch-reprocess。
    """

    async def test_dashboard_renders_batch_select_ui(self, client: tuple[AsyncClient, Path]) -> None:
        c, _ = client
        with patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = {"bilibili": [{"uid": 1, "name": "t"}]}
            resp = await c.get("/")
        assert resp.status_code == 200
        # 每行 checkbox name="msg_id" value="{MSG_ID}"
        assert 'name="msg_id"' in resp.text
        assert f'value="{MSG_ID}"' in resp.text
        # 表头全选 checkbox
        assert 'id="select-all"' in resp.text
        # 行 checkbox class
        assert "row-checkbox" in resp.text
        # 批量按钮 + form action
        assert "重跑选中" in resp.text
        assert "/messages/batch-reprocess" in resp.text
