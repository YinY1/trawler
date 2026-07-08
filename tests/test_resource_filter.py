"""Tests for ``api.resource_filter`` (issue #106 — plan T2).

``TokenResourceFilter`` 是 token 行级规则的不可变视图：从 ``ApiTokenEntry``
的 ``ResourceRules`` 一次性构造，路由层调 ``allows_*`` 方法判断消息/订阅可见性。

覆盖 spec §6.2 / §5.2 全部语义：
- None = 不限制 / [] = 拒绝一切
- platform 维度过滤
- subscription_ref 维度过滤（复合 key ``<short>:<id>``）
- 两维 AND 组合（非 OR）
- frozen dataclass 不可变
"""

from __future__ import annotations

import dataclasses

import pytest

from api.resource_filter import TokenResourceFilter
from shared.config import ApiTokenEntry, ResourceRules
from shared.protocols import ContentType, MessageRecord, Phase


def _msg(platform: str, sub_ref: str) -> MessageRecord:
    """构造一条 ``MessageRecord``，仅 platform / subscription_ref 用于过滤断言。"""
    return MessageRecord(
        msg_id=f"{platform}:x",
        platform=platform,
        content_type=ContentType.TEXT,
        phase=Phase.DISCOVERED,
        pubdate=0,
        title="",
        author="",
        subscription_ref=sub_ref,
    )


class TestTokenResourceFilter:
    def test_from_token_no_rules_is_unrestricted(self) -> None:
        token = ApiTokenEntry(name="x", token_hash="h")
        f = TokenResourceFilter.from_token(token)
        assert f.platforms is None
        assert f.subscription_refs is None
        assert f.allows_platform("bili") is True
        assert f.allows_message(_msg("xhs", "u1")) is True

    def test_platforms_filter_restricts(self) -> None:
        token = ApiTokenEntry(
            name="x",
            token_hash="h",
            resource_rules=ResourceRules(platforms=["bili"]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_platform("bili") is True
        assert f.allows_platform("xhs") is False

    def test_subscription_refs_uses_composite_key(self) -> None:
        token = ApiTokenEntry(
            name="x",
            token_hash="h",
            resource_rules=ResourceRules(subscription_refs=["bili:100"]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_subscription("bili", "100") is True
        assert f.allows_subscription("bili", "200") is False
        assert f.allows_subscription("xhs", "100") is False  # 跨平台

    def test_allows_message_and_combination(self) -> None:
        """``platforms=[bili]`` + ``subs=[bili:100]`` 是 AND 组合（spec §5.2）。"""
        token = ApiTokenEntry(
            name="x",
            token_hash="h",
            resource_rules=ResourceRules(
                platforms=["bili"], subscription_refs=["bili:100"]
            ),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_message(_msg("bili", "100")) is True
        assert f.allows_message(_msg("bili", "200")) is False  # sub 维度拒绝
        assert f.allows_message(_msg("xhs", "u456")) is False  # platform 维度拒绝

    def test_empty_platforms_list_denies_all(self) -> None:
        """``platforms=[]`` = 拒绝一切（与 ``None=全权限`` 相反，spec §5.3）。"""
        token = ApiTokenEntry(
            name="x",
            token_hash="h",
            resource_rules=ResourceRules(platforms=[]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_platform("bili") is False
        assert f.allows_message(_msg("bili", "100")) is False

    def test_empty_subscription_refs_denies_all(self) -> None:
        """``subscription_refs=[]`` = 拒绝一切订阅。"""
        token = ApiTokenEntry(
            name="x",
            token_hash="h",
            resource_rules=ResourceRules(subscription_refs=[]),
        )
        f = TokenResourceFilter.from_token(token)
        assert f.allows_subscription("bili", "100") is False
        assert f.allows_message(_msg("bili", "100")) is False

    def test_unrestricted_factory(self) -> None:
        f = TokenResourceFilter.unrestricted()
        assert f.allows_message(_msg("bili", "100")) is True

    def test_frozen_dataclass(self) -> None:
        """``TokenResourceFilter`` 不可变（防止路由层意外修改）。"""
        f = TokenResourceFilter(platforms=frozenset({"bili"}), subscription_refs=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.platforms = None  # type: ignore[misc]
