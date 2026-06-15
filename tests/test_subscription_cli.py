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
    def test_list_all(self, subs_file: Path) -> None:
        subs = list_subscriptions(path=str(subs_file))
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

    def test_list_filter_bili(self, subs_file: Path) -> None:
        subs = list_subscriptions(platform="bili", path=str(subs_file))
        assert "bilibili" in subs
        assert "xiaohongshu" not in subs
        assert "weibo" not in subs

    def test_list_filter_xhs(self, subs_file: Path) -> None:
        subs = list_subscriptions(platform="xhs", path=str(subs_file))
        assert "xiaohongshu" in subs
        assert "bilibili" not in subs

    def test_list_filter_weibo(self, subs_file: Path) -> None:
        subs = list_subscriptions(platform="weibo", path=str(subs_file))
        assert "weibo" in subs
        assert "bilibili" not in subs

    def test_list_empty_file(self) -> None:
        """Empty TOML file returns empty dict."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("# empty\n")
            tmp = Path(f.name)
        try:
            subs = list_subscriptions(path=str(tmp))
            assert subs == {}
        finally:
            tmp.unlink(missing_ok=True)

    def test_list_nonexistent_file(self) -> None:
        """Non-existent file returns empty dict without crashing."""
        subs = list_subscriptions(path="/tmp/nonexistent_subscriptions.toml")
        assert subs == {}


# ── add ────────────────────────────────────────────────────────────────


class TestAdd:
    def test_add_bili(self, subs_file: Path) -> None:
        ok, msg = add_subscription(platform="bili", identifier=12345, name="新UP主", path=str(subs_file))
        assert ok
        assert "已添加" in msg
        subs = list_subscriptions(path=str(subs_file))
        names = [s["name"] for s in subs["bilibili"]]
        assert "新UP主" in names
        assert 12345 in [s["uid"] for s in subs["bilibili"]]

    def test_add_xhs(self, subs_file: Path) -> None:
        ok, msg = add_subscription(platform="xhs", identifier="user_abc", name="小红书用户", path=str(subs_file))
        assert ok
        assert "已添加" in msg
        subs = list_subscriptions(path=str(subs_file))
        assert "小红书用户" in [s["name"] for s in subs["xiaohongshu"]]

    def test_add_weibo(self, subs_file: Path) -> None:
        ok, msg = add_subscription(platform="weibo", identifier="weibo_user", name="微博用户", path=str(subs_file))
        assert ok
        assert "已添加" in msg
        subs = list_subscriptions(path=str(subs_file))
        assert "微博用户" in [s["name"] for s in subs["weibo"]]

    def test_add_duplicate_bili(self, subs_file: Path) -> None:
        """Adding same uid should fail silently with message."""
        ok, msg = add_subscription(platform="bili", identifier=2137589551, name="李大霄", path=str(subs_file))
        assert not ok
        assert "已存在" in msg

    def test_add_duplicate_xhs(self, subs_file: Path) -> None:
        ok, msg = add_subscription(
            platform="xhs", identifier="5a7d3ed311be106d0306e7d6", name="Angelababy", path=str(subs_file)
        )
        assert not ok
        assert "已存在" in msg

    def test_add_creates_file_if_not_exists(self) -> None:
        """Adding to a non-existent file should create it."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "subs.toml"
            ok, msg = add_subscription(platform="bili", identifier=999, name="New", path=str(p))
            assert ok
            assert p.exists()
            subs = list_subscriptions(path=str(p))
            assert len(subs["bilibili"]) == 1
            assert subs["bilibili"][0]["uid"] == 999

    def test_add_invalid_platform(self, subs_file: Path) -> None:
        ok, msg = add_subscription(platform="invalid", identifier=1, name="x", path=str(subs_file))
        assert not ok
        assert "无效平台" in msg


# ── remove ─────────────────────────────────────────────────────────────


class TestRemove:
    def test_remove_bili(self, subs_file: Path) -> None:
        ok, msg = remove_subscription(platform="bili", identifier=2137589551, path=str(subs_file))
        assert ok
        assert "已删除" in msg
        subs = list_subscriptions(path=str(subs_file))
        assert "bilibili" not in subs  # platform removed when no subs left

    def test_remove_xhs(self, subs_file: Path) -> None:
        ok, msg = remove_subscription(platform="xhs", identifier="5a7d3ed311be106d0306e7d6", path=str(subs_file))
        assert ok
        subs = list_subscriptions(path=str(subs_file))
        assert "xiaohongshu" not in subs

    def test_remove_weibo(self, subs_file: Path) -> None:
        ok, msg = remove_subscription(platform="weibo", identifier="2803301701", path=str(subs_file))
        assert ok
        subs = list_subscriptions(path=str(subs_file))
        assert "weibo" not in subs

    def test_remove_nonexistent(self, subs_file: Path) -> None:
        ok, msg = remove_subscription(platform="bili", identifier=99999, path=str(subs_file))
        assert not ok
        assert "未找到" in msg

    def test_remove_invalid_platform(self, subs_file: Path) -> None:
        ok, msg = remove_subscription(platform="invalid", identifier=1, path=str(subs_file))
        assert not ok
        assert "无效平台" in msg


# ── search_by_name ────────────────────────────────────────────────────


class TestSearchByName:
    def test_weibo_no_cookie(self) -> None:
        """Without cookie in config, weibo search should return login-needed."""
        ok, msg, _c = search_by_name(platform="weibo", name="test", config_path="/tmp/nonexistent_config.toml")
        assert not ok
        assert "需要先登录" in msg

    def test_xhs_no_cookie(self) -> None:
        """Without cookie in config, xhs search should return login-needed."""
        ok, msg, _c = search_by_name(platform="xhs", name="test", config_path="/tmp/nonexistent_config.toml")
        assert not ok
        assert "需要先登录" in msg

    def test_bili_no_auth(self) -> None:
        """Without sessdata in config, search should return login-needed."""
        ok, msg, _c = search_by_name(platform="bili", name="李大霄", config_path="/tmp/nonexistent_config.toml")
        assert not ok
        assert "需要先登录" in msg
