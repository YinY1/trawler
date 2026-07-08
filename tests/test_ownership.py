"""Tests for ``api.resource_filter.TokenOwnership`` (issue #108).

替换 tests/test_resource_filter.py（#106 的 TokenResourceFilter 已废弃）。

TokenOwnership 是 token ownership 的不可变视图：从 ApiTokenEntry 一次性构造，
路由层调 has_sub_access / has_sub_write 判断订阅可见性。

覆盖 spec §5.1 全部语义：
- superuser bypass（持 tokens:manage）
- owner 全权
- assigned 只读
- outsider 无权
- frozen dataclass 不可变
"""

from __future__ import annotations

import dataclasses

import pytest

from api.resource_filter import TokenOwnership
from shared.config import ApiTokenEntry, BiliSubscription, UserSubscription


def _bili_sub(owner: str = "", assigned: list[str] | None = None) -> BiliSubscription:
    """构造一条 bili sub，仅 owner_token / assigned_tokens 用于判断。"""
    return BiliSubscription(
        uid=100,
        name="UP1",
        owner_token=owner,
        assigned_tokens=assigned or [],
    )


def _xhs_sub(owner: str = "", assigned: list[str] | None = None) -> UserSubscription:
    return UserSubscription(
        user_id="u456",
        name="XHS1",
        owner_token=owner,
        assigned_tokens=assigned or [],
    )


class TestTokenOwnershipFromToken:
    def test_superuser_token_detected(self) -> None:
        """持 tokens:manage 的 token → is_superuser=True。"""
        token = ApiTokenEntry(name="admin", token_hash="h", scopes=["tokens:manage"])
        o = TokenOwnership.from_token(token)
        assert o.is_superuser is True
        assert o.token_name == "admin"

    def test_non_superuser_token_detected(self) -> None:
        """持 messages:read 但无 tokens:manage → is_superuser=False。"""
        token = ApiTokenEntry(
            name="reader", token_hash="h", scopes=["messages:read"]
        )
        o = TokenOwnership.from_token(token)
        assert o.is_superuser is False
        assert o.token_name == "reader"

    def test_empty_scopes_not_superuser(self) -> None:
        """空 scopes → is_superuser=False（#108 破坏性变更）。"""
        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        o = TokenOwnership.from_token(token)
        assert o.is_superuser is False


class TestTokenOwnershipHasSubAccess:
    """has_sub_access 读权限四态（spec §5.1）。"""

    def test_superuser_accesses_any_sub(self) -> None:
        o = TokenOwnership(is_superuser=True, token_name="admin")
        sub = _bili_sub(owner="someone-else", assigned=[])
        assert o.has_sub_access(sub) is True

    def test_owner_accesses_own_sub(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="owner-bot")
        sub = _bili_sub(owner="owner-bot")
        assert o.has_sub_access(sub) is True

    def test_assigned_accesses_assigned_sub(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="reader-bot")
        sub = _bili_sub(owner="owner-bot", assigned=["reader-bot"])
        assert o.has_sub_access(sub) is True

    def test_outsider_denied(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="stranger")
        sub = _bili_sub(owner="owner-bot", assigned=["reader-bot"])
        assert o.has_sub_access(sub) is False

    def test_orphan_sub_only_superuser(self) -> None:
        """owner_token='' 孤儿 sub，只 superuser 能 access。"""
        o = TokenOwnership(is_superuser=False, token_name="reader-bot")
        sub = _bili_sub(owner="", assigned=["reader-bot"])
        # assigned 仍能读（assigned_tokens 非空）
        assert o.has_sub_access(sub) is True

        o2 = TokenOwnership(is_superuser=False, token_name="stranger")
        assert o2.has_sub_access(sub) is False


class TestTokenOwnershipHasSubWrite:
    """has_sub_write 写权限四态（spec §5.1）。

    关键不对称：assigned 不能写（只 owner / superuser 能写）。
    """

    def test_superuser_writes_any_sub(self) -> None:
        o = TokenOwnership(is_superuser=True, token_name="admin")
        sub = _bili_sub(owner="someone-else")
        assert o.has_sub_write(sub) is True

    def test_owner_writes_own_sub(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="owner-bot")
        sub = _bili_sub(owner="owner-bot")
        assert o.has_sub_write(sub) is True

    def test_assigned_cannot_write(self) -> None:
        """assigned 被分配只读，写权限拒绝（spec §5.2 关键不对称）。"""
        o = TokenOwnership(is_superuser=False, token_name="reader-bot")
        sub = _bili_sub(owner="owner-bot", assigned=["reader-bot"])
        assert o.has_sub_write(sub) is False

    def test_outsider_cannot_write(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="stranger")
        sub = _bili_sub(owner="owner-bot")
        assert o.has_sub_write(sub) is False

    def test_assigned_cannot_write_orphan_even_if_assigned(self) -> None:
        """孤儿 sub（owner=''），assigned 仍不能写（assigned 永远不能写）。"""
        o = TokenOwnership(is_superuser=False, token_name="reader-bot")
        sub = _bili_sub(owner="", assigned=["reader-bot"])
        assert o.has_sub_write(sub) is False


class TestTokenOwnershipCanManageAssign:
    """can_manage_assign — 仅 superuser 能分配（spec §5.2）。"""

    def test_superuser_can_manage_assign(self) -> None:
        o = TokenOwnership(is_superuser=True, token_name="admin")
        assert o.can_manage_assign() is True

    def test_owner_cannot_manage_assign(self) -> None:
        """owner 也不能分配自己的 sub 给别的 token（决策 #10）。"""
        o = TokenOwnership(is_superuser=False, token_name="owner-bot")
        assert o.can_manage_assign() is False

    def test_assigned_cannot_manage_assign(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="reader-bot")
        assert o.can_manage_assign() is False


class TestTokenOwnershipFactory:
    def test_unrestricted_factory_for_test(self) -> None:
        """unrestricted() 工厂供测试 / Web session 等价场景用。"""
        o = TokenOwnership.unrestricted(token_name="test-bot")
        assert o.is_superuser is True
        assert o.token_name == "test-bot"


class TestTokenOwnershipFrozen:
    def test_frozen_dataclass(self) -> None:
        """TokenOwnership 不可变（防止路由层意外修改）。"""
        o = TokenOwnership(is_superuser=False, token_name="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            o.is_superuser = True  # type: ignore[misc]


class TestTokenOwnershipXhsSub:
    """UserSubscription（xhs/weibo 共用）也支持 ownership 判断。"""

    def test_owner_accesses_xhs_sub(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="xhs-owner")
        sub = _xhs_sub(owner="xhs-owner")
        assert o.has_sub_access(sub) is True
        assert o.has_sub_write(sub) is True

    def test_assigned_accesses_xhs_sub(self) -> None:
        o = TokenOwnership(is_superuser=False, token_name="reader")
        sub = _xhs_sub(owner="xhs-owner", assigned=["reader"])
        assert o.has_sub_access(sub) is True
        assert o.has_sub_write(sub) is False
