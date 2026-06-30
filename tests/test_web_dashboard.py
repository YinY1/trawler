from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from shared.protocols import ContentType, Phase
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


class TestDashboard:
    def _seed_message(
        self,
        data_dir: Path,
        *,
        msg_id: str = "bili:BVtest",
        platform: str = "bili",
        content_type: ContentType = ContentType.VIDEO,
        phase: Phase = Phase.SUMMARIZED,
        error: str = "",
        retry_count: int = 0,
        last_error: str = "",
        permanent_error: bool = False,
        title: str = "测试视频",
        author: str = "测试UP",
    ) -> None:
        """在 data_dir 下种入一条可定制状态的消息（绕过 add_new 直接写 store 内部 dict）。"""
        import time as _time

        from shared.message_store import MessageStore

        store = MessageStore(data_dir)
        store._messages[msg_id] = {
            "platform": platform,
            "content_type": content_type.value,
            "phase": phase.value,
            "pubdate": int(_time.time()),
            "title": title,
            "author": author,
            "created_at": 0.0,
            "updated_at": _time.time(),
            "error": error,
            "retry_count": retry_count,
            "last_error": last_error,
            "permanent_error": permanent_error,
        }
        store._dirty = True
        store.save()

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_returns_200(self, mock_load, mock_list, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "/tmp"
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {"bilibili": [{"uid": 1, "name": "test"}]}

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_chinese_phase_label(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G1: 表格 phase 单元格渲染中文阶段名（phase_label filter）。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        # 种入一条 SUMMARIZED 消息
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        self._seed_message(data_dir, phase=Phase.SUMMARIZED)

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        # 中文 phase label 出现，英文 enum name 不再以裸露形式出现在 phase 单元格
        assert "已摘要" in resp.text

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_content_type_tag(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G4: 详情面板 meta 行渲染 content_type tag (视频/图文)。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # VIDEO 和 TEXT 各一条
        self._seed_message(
            data_dir, msg_id="bili:V1", content_type=ContentType.VIDEO, phase=Phase.PUSHED
        )
        self._seed_message(
            data_dir, msg_id="xhs:N1", platform="xhs", content_type=ContentType.TEXT, phase=Phase.PUSHED
        )

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        assert "视频" in resp.text
        assert "图文" in resp.text

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_permanent_error_tag(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G2: 详情面板错误区显示 永久/可重试 tag (区分 permanent_error)。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # 永久失败 + 可重试失败 各一条
        self._seed_message(
            data_dir,
            msg_id="bili:perm",
            phase=Phase.SUMMARIZED,
            error="transcribe 模型超时",
            permanent_error=True,
            title="永久失败",
        )
        self._seed_message(
            data_dir,
            msg_id="bili:temp",
            phase=Phase.SUMMARIZED,
            error="临时网络错误",
            permanent_error=False,
            title="临时失败",
        )

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        assert "永久" in resp.text
        assert "可重试" in resp.text

    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    async def test_dashboard_renders_retry_progress(
        self, mock_load, mock_list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G3: 临时失败 (error 空, retry_count>0) 显示重试进度块。"""
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", tmp_path / "auth.toml")
        set_password(PASSWORD)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        self._seed_message(
            data_dir,
            msg_id="bili:retry",
            phase=Phase.SUMMARIZED,
            error="",  # 关键：未永久失败
            retry_count=2,
            last_error="API timeout",
            permanent_error=False,
            title="重试中",
        )

        from shared.config import GeneralConfig

        mock_general = GeneralConfig(data_dir=str(data_dir))
        mock_load.return_value.general = mock_general
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
            resp = await c.get("/")

        assert resp.status_code == 200
        assert "重试中" in resp.text  # 既是 title 也匹配 label
        assert "第 2 次" in resp.text
        assert "API timeout" in resp.text
