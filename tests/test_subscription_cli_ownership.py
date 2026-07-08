"""Tests for core.subscription_cli ownership helpers (issue #108).

覆盖 assign_token_to_subscription / unassign_token_from_subscription /
set_subscription_owner 三个新函数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.subscription_cli import (
    assign_token_to_subscription,
    set_subscription_owner,
    unassign_token_from_subscription,
)


@pytest.fixture
def tmp_subs_with_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写盘 config/subscriptions.toml 含 bili uid=100 owner='owner-bot'。

    同时 mock auth.toml 含 'owner-bot' 和 'reader-bot' 两个 token。
    """
    subs_path = tmp_path / "subscriptions.toml"
    subs_path.write_text(
        '[[bilibili.subscriptions]]\n'
        'uid = 100\n'
        'name = "UP100"\n'
        'owner_token = "owner-bot"\n',
        encoding="utf-8",
    )
    # mock auth.toml
    auth_path = tmp_path / "auth.toml"
    auth_path.write_text(
        '[[api_tokens]]\n'
        'name = "owner-bot"\n'
        f'token_hash = "{"a" * 64}"\n'
        'created_at = 0.0\n\n'
        '[[api_tokens]]\n'
        'name = "reader-bot"\n'
        f'token_hash = "{"b" * 64}"\n'
        'created_at = 0.0\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    return subs_path


class TestAssignTokenToSubscription:
    async def test_assign_success(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """成功分配 reader-bot 到 bili/100。"""
        ok, msg = await assign_token_to_subscription(
            platform="bili",
            identifier="100",
            token_name="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is True
        assert "已分配" in msg
        # 落盘验证
        content = tmp_subs_with_owner.read_text(encoding="utf-8")
        assert "reader-bot" in content
        assert "assigned_tokens" in content

    async def test_assign_idempotent(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """重复分配同一 token 幂等（成功）。"""
        await assign_token_to_subscription(
            platform="bili", identifier="100", token_name="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        ok, msg = await assign_token_to_subscription(
            platform="bili", identifier="100", token_name="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is True

    async def test_assign_unknown_token_fails(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """分配不存在的 token → 失败。"""
        ok, msg = await assign_token_to_subscription(
            platform="bili", identifier="100", token_name="ghost-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is False
        assert "未知 token" in msg

    async def test_assign_unknown_subscription_fails(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """分配到不存在的 sub → 失败。"""
        ok, msg = await assign_token_to_subscription(
            platform="bili", identifier="999", token_name="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is False
        assert "未找到订阅" in msg


class TestUnassignTokenFromSubscription:
    async def test_unassign_success(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """先 assign 再 unassign，落盘 assigned_tokens 应消失。"""
        await assign_token_to_subscription(
            platform="bili", identifier="100", token_name="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        ok, msg = await unassign_token_from_subscription(
            platform="bili", identifier="100", token_name="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is True
        content = tmp_subs_with_owner.read_text(encoding="utf-8")
        # unassign 后空列表，字段应被移除
        assert "reader-bot" not in content

    async def test_unassign_idempotent(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """unassign 不存在的 token 幂等。"""
        ok, msg = await unassign_token_from_subscription(
            platform="bili", identifier="100", token_name="never-assigned",
            path=str(tmp_subs_with_owner),
        )
        assert ok is True


class TestSetSubscriptionOwner:
    async def test_set_owner_success(
        self, tmp_subs_with_owner: Path
    ) -> None:
        """给 bili/100 改 owner 为 reader-bot。"""
        ok, msg = await set_subscription_owner(
            platform="bili", identifier="100", owner_token="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is True
        content = tmp_subs_with_owner.read_text(encoding="utf-8")
        assert 'owner_token = "reader-bot"' in content

    async def test_set_owner_unknown_token_fails(
        self, tmp_subs_with_owner: Path
    ) -> None:
        ok, msg = await set_subscription_owner(
            platform="bili", identifier="100", owner_token="ghost-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is False
        assert "未知 token" in msg

    async def test_set_owner_unknown_sub_fails(
        self, tmp_subs_with_owner: Path
    ) -> None:
        ok, msg = await set_subscription_owner(
            platform="bili", identifier="999", owner_token="reader-bot",
            path=str(tmp_subs_with_owner),
        )
        assert ok is False
        assert "未找到订阅" in msg


class TestFixturePathsConsistency:
    """I2 修订（issue #108 review）：``tmp_subs_with_owner`` fixture 内
    ``monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)`` 和
    ``subs_path`` 必须在同一 ``tmp_path`` 下，否则 ``assign`` /
    ``set_subscription_owner`` 读 ``auth.toml`` 校验 token 存在时跨目录找不到。

    本 sanity 测试防未来 fixture 演化时两个路径跑偏。
    """

    def test_auth_and_subs_paths_share_tmp_dir(
        self, tmp_subs_with_owner: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import web.auth as web_auth_mod

        subs_dir = tmp_subs_with_owner.parent
        auth_path = web_auth_mod.AUTH_TOML_PATH
        # 两路径必须在同一 tmp 目录（parent 一致），否则跨目录读 auth.toml 失败
        assert auth_path.parent == subs_dir
        assert auth_path.exists(), (
            f"auth.toml 未写盘到 tmp_path（实际路径 {auth_path}），"
            "assign/set_owner 会因找不到 token 误判"
        )
