from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.subscription_cli import (
    add_subscription,
    list_subscriptions,
    remove_subscription,
    search_by_name,
)

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def subs_file() -> Path:
    """Create a temporary subscriptions.toml with known content."""
    content = """# ═══════════════════════════════════════════════════════════════════
# Trawler 订阅列表
# ═══════════════════════════════════════════════════════════════════

# ── B 站（Bilibili） ────────────────────────────────────────────

[[bilibili.subscriptions]]
uid = 2137589551
name = "李大霄"

# ── 小红书（Xiaohongshu） ───────────────────────────────────────

[[xiaohongshu.subscriptions]]
user_id = "5a7d3ed311be106d0306e7d6"
name = "Angelababy"

# ── 微博（Weibo） ───────────────────────────────────────────────

[[weibo.subscriptions]]
user_id = "2803301701"
name = "人民日报"
"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False)
    tmp.write(content)
    tmp.close()
    yield Path(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


# ── list ──────────────────────────────────────────────────────────────


class TestList:
    async def test_list_all(self, subs_file: Path) -> None:
        subs = await list_subscriptions(path=str(subs_file))
        assert "bilibili" in subs
        assert "xiaohongshu" in subs
        assert "weibo" in subs
        assert len(subs["bilibili"]) == 1
        assert subs["bilibili"][0]["name"] == "李大霄"
        assert subs["bilibili"][0]["uid"] == 2137589551
        assert len(subs["xiaohongshu"]) == 1
        assert subs["xiaohongshu"][0]["user_id"] == "5a7d3ed311be106d0306e7d6"
        assert len(subs["weibo"]) == 1
        assert subs["weibo"][0]["user_id"] == "2803301701"

    async def test_list_filter_bili(self, subs_file: Path) -> None:
        subs = await list_subscriptions(platform="bili", path=str(subs_file))
        assert "bilibili" in subs
        assert "xiaohongshu" not in subs
        assert "weibo" not in subs

    async def test_list_filter_xhs(self, subs_file: Path) -> None:
        subs = await list_subscriptions(platform="xhs", path=str(subs_file))
        assert "xiaohongshu" in subs
        assert "bilibili" not in subs

    async def test_list_filter_weibo(self, subs_file: Path) -> None:
        subs = await list_subscriptions(platform="weibo", path=str(subs_file))
        assert "weibo" in subs
        assert "bilibili" not in subs

    async def test_list_empty_file(self) -> None:
        """Empty TOML file returns empty dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("# empty\n")
            tmp = Path(f.name)
        try:
            subs = await list_subscriptions(path=str(tmp))
            assert subs == {}
        finally:
            tmp.unlink(missing_ok=True)

    async def test_list_nonexistent_file(self) -> None:
        """Non-existent file returns empty dict without crashing."""
        subs = await list_subscriptions(path="/tmp/nonexistent_subscriptions.toml")
        assert subs == {}


# ── add ────────────────────────────────────────────────────────────────


class TestAdd:
    async def test_add_bili(self, subs_file: Path) -> None:
        ok, msg = await add_subscription(platform="bili", identifier=12345, name="新UP主", path=str(subs_file))
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        names = [s["name"] for s in subs["bilibili"]]
        assert "新UP主" in names
        assert 12345 in [s["uid"] for s in subs["bilibili"]]

    async def test_add_xhs(self, subs_file: Path) -> None:
        ok, msg = await add_subscription(platform="xhs", identifier="user_abc", name="小红书用户", path=str(subs_file))
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        assert "小红书用户" in [s["name"] for s in subs["xiaohongshu"]]

    async def test_add_weibo(self, subs_file: Path) -> None:
        ok, msg = await add_subscription(
            platform="weibo",
            identifier="weibo_user",
            name="微博用户",
            path=str(subs_file),
        )
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        assert "微博用户" in [s["name"] for s in subs["weibo"]]

    async def test_add_duplicate_bili(self, subs_file: Path) -> None:
        """Adding same uid should fail silently with message."""
        ok, msg = await add_subscription(platform="bili", identifier=2137589551, name="李大霄", path=str(subs_file))
        assert not ok
        assert "已存在" in msg

    async def test_add_duplicate_xhs(self, subs_file: Path) -> None:
        ok, msg = await add_subscription(
            platform="xhs", identifier="5a7d3ed311be106d0306e7d6", name="Angelababy", path=str(subs_file)
        )
        assert not ok
        assert "已存在" in msg

    async def test_add_creates_file_if_not_exists(self) -> None:
        """Adding to a non-existent file should create it."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "subs.toml"
            ok, msg = await add_subscription(platform="bili", identifier=999, name="New", path=str(p))
            assert ok
            assert p.exists()
            subs = await list_subscriptions(path=str(p))
            assert len(subs["bilibili"]) == 1
            assert subs["bilibili"][0]["uid"] == 999

    async def test_add_invalid_platform(self, subs_file: Path) -> None:
        ok, msg = await add_subscription(platform="invalid", identifier=1, name="x", path=str(subs_file))
        assert not ok
        assert "无效平台" in msg


# ── remove ─────────────────────────────────────────────────────────────


class TestRemove:
    async def test_remove_bili(self, subs_file: Path) -> None:
        ok, msg = await remove_subscription(platform="bili", identifier=2137589551, path=str(subs_file))
        assert ok
        assert "已删除" in msg
        subs = await list_subscriptions(path=str(subs_file))
        assert "bilibili" not in subs  # platform removed when no subs left

    async def test_remove_xhs(self, subs_file: Path) -> None:
        ok, msg = await remove_subscription(platform="xhs", identifier="5a7d3ed311be106d0306e7d6", path=str(subs_file))
        assert ok
        subs = await list_subscriptions(path=str(subs_file))
        assert "xiaohongshu" not in subs

    async def test_remove_weibo(self, subs_file: Path) -> None:
        ok, msg = await remove_subscription(platform="weibo", identifier="2803301701", path=str(subs_file))
        assert ok
        subs = await list_subscriptions(path=str(subs_file))
        assert "weibo" not in subs

    async def test_remove_nonexistent(self, subs_file: Path) -> None:
        ok, msg = await remove_subscription(platform="bili", identifier=99999, path=str(subs_file))
        assert not ok
        assert "未找到" in msg

    async def test_remove_invalid_platform(self, subs_file: Path) -> None:
        ok, msg = await remove_subscription(platform="invalid", identifier=1, path=str(subs_file))
        assert not ok
        assert "无效平台" in msg


# ── search_by_name ────────────────────────────────────────────────────


class TestSearchByName:
    async def test_weibo_no_cookie(self) -> None:
        """Without cookie in config, weibo search should return login-needed."""
        ok, msg, _c = await search_by_name(platform="weibo", name="test", config_path="/tmp/nonexistent_config.toml")
        assert not ok
        assert "需要先登录" in msg

    async def test_xhs_no_cookie(self) -> None:
        """Without cookie in config, xhs search should return login-needed."""
        ok, msg, _c = await search_by_name(platform="xhs", name="test", config_path="/tmp/nonexistent_config.toml")
        assert not ok
        assert "需要先登录" in msg

    async def test_bili_no_auth(self) -> None:
        """Without sessdata in config, search should return login-needed."""
        ok, msg, _c = await search_by_name(platform="bili", name="李大霄", config_path="/tmp/nonexistent_config.toml")
        assert not ok
        assert "需要先登录" in msg


# ── add_endpoint_to_subscription ──────────────────────────────────────


class TestAddEndpoint:
    """add_endpoint_to_subscription 用例。load_config 必须 monkeypatch。"""

    @pytest.fixture
    def mock_known_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """让 load_config 返回含 'gotify-main' 的 endpoints 列表。"""
        from core import subscription_cli
        from shared.config import EndpointConfig

        async def _fake_load(*_a, **_kw):
            from shared.config import Config
            cfg = Config()
            cfg.endpoints = [EndpointConfig(name="gotify-main", url="http://x", token="t")]
            return cfg

        monkeypatch.setattr(subscription_cli, "load_config", _fake_load)

    async def test_add_endpoint_to_subscription_ok(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok
        assert "已绑定" in msg
        # 验证落盘
        subs = await list_subscriptions(path=str(subs_file))
        assert "gotify-main" in subs["bilibili"][0]["notify_endpoints"]

    async def test_add_endpoint_to_subscription_idempotent(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        # 先加一次
        ok1, _ = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok1
        # 再加一次 — 幂等
        ok2, msg2 = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok2
        assert "已绑定" in msg2
        subs = await list_subscriptions(path=str(subs_file))
        # 不应重复
        eps = subs["bilibili"][0]["notify_endpoints"]
        assert eps.count("gotify-main") == 1

    async def test_add_endpoint_to_subscription_unknown_ep(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="nonexistent", path=str(subs_file),
        )
        assert not ok
        assert "未知 endpoint" in msg

    async def test_add_endpoint_to_subscription_no_sub(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=99999999,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert not ok
        assert "未找到订阅" in msg

    async def test_add_endpoint_to_subscription_invalid_platform(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """无效平台返回 (False, "无效平台: ...")，与 add_subscription 一致。"""
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="invalid", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert not ok
        assert "无效平台" in msg

    async def test_add_endpoint_to_subscription_file_not_found(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """订阅文件不存在 → (False, "未找到订阅")，覆盖 _load_doc 返回 None 分支。"""
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file.parent / "nonexistent.toml"),
        )
        assert not ok
        assert "未找到订阅" in msg


# ── remove_endpoint_from_subscription ─────────────────────────────────


class TestRemoveEndpoint:
    @pytest.fixture
    def mock_known_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core import subscription_cli
        from shared.config import EndpointConfig

        async def _fake_load(*_a, **_kw):
            from shared.config import Config
            cfg = Config()
            cfg.endpoints = [EndpointConfig(name="gotify-main", url="http://x", token="t")]
            return cfg

        monkeypatch.setattr(subscription_cli, "load_config", _fake_load)

    async def test_remove_endpoint_from_subscription_ok(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """先 add 再 remove，验证落盘后列表为空。"""
        from core.subscription_cli import (
            add_endpoint_to_subscription,
            remove_endpoint_from_subscription,
        )
        await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        ok, msg = await remove_endpoint_from_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok
        assert "已解绑" in msg
        subs = await list_subscriptions(path=str(subs_file))
        eps = subs["bilibili"][0].get("notify_endpoints", [])
        assert "gotify-main" not in eps

    async def test_remove_endpoint_from_subscription_idempotent(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """remove 不存在的 endpoint 也返回 True（幂等）。"""
        from core.subscription_cli import remove_endpoint_from_subscription
        ok, msg = await remove_endpoint_from_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="never-bound", path=str(subs_file),
        )
        assert ok
        assert "已解绑" in msg

    async def test_remove_endpoint_from_subscription_no_sub(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import remove_endpoint_from_subscription
        ok, msg = await remove_endpoint_from_subscription(
            platform="bili", identifier=99999999,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert not ok
        assert "未找到订阅" in msg

    async def test_remove_endpoint_from_subscription_invalid_platform(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """无效平台返回 (False, "无效平台: ...")。"""
        from core.subscription_cli import remove_endpoint_from_subscription
        ok, msg = await remove_endpoint_from_subscription(
            platform="invalid", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert not ok
        assert "无效平台" in msg


# ── add_subscription with default_notify_endpoint ─────────────────────


class TestAddSubscriptionDefaultEndpoint:
    """语法糖 + 回滚用例。"""

    @pytest.fixture
    def mock_known_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from core import subscription_cli
        from shared.config import Config, EndpointConfig

        async def _fake_load(*_a, **_kw):
            cfg = Config()
            cfg.endpoints = [EndpointConfig(name="gotify-main", url="http://x", token="t")]
            return cfg

        monkeypatch.setattr(subscription_cli, "load_config", _fake_load)

    async def test_add_subscription_with_default_endpoint(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """default_notify_endpoint 合法 → 订阅被加 + endpoint 被绑定。"""
        ok, msg = await add_subscription(
            platform="bili", identifier=88888, name="新UP",
            path=str(subs_file), default_notify_endpoint="gotify-main",
        )
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        names = [s["name"] for s in subs["bilibili"]]
        assert "新UP" in names
        # endpoint 被绑定
        target = next(s for s in subs["bilibili"] if s["uid"] == 88888)
        assert "gotify-main" in target["notify_endpoints"]

    async def test_add_subscription_with_bad_default_endpoint(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """default_notify_endpoint 不存在 → 回滚，订阅不应被加入文件。"""
        ok, msg = await add_subscription(
            platform="bili", identifier=77777, name="回滚UP",
            path=str(subs_file), default_notify_endpoint="bad-ep",
        )
        assert not ok
        assert "默认 endpoint 绑定失败" in msg
        # 关键断言：订阅被回滚删除
        subs = await list_subscriptions(path=str(subs_file))
        uids = [s["uid"] for s in subs.get("bilibili", [])]
        assert 77777 not in uids

    async def test_add_subscription_without_default_endpoint(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """不传 default_notify_endpoint → 行为完全不变（向后兼容）。"""
        ok, msg = await add_subscription(
            platform="bili", identifier=66666, name="纯加",
            path=str(subs_file),
        )
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        target = next(s for s in subs["bilibili"] if s["uid"] == 66666)
        # 不应有 notify_endpoints 字段（保持现有行为）
        assert "notify_endpoints" not in target or target["notify_endpoints"] == []
