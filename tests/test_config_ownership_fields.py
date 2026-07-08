"""临时测试：BiliSubscription/UserSubscription 加 owner_token/assigned_tokens 字段。

Task 7 整理时删除（合并到 test_api_*.py 的 fixture 里）。
"""
from __future__ import annotations

from shared.config import ApiTokenEntry, BiliSubscription, UserSubscription


def test_bili_subscription_default_owner_token() -> None:
    """新 BiliSubscription 默认 owner_token='' / assigned_tokens=[]（向后兼容）。"""
    sub = BiliSubscription(uid=100, name="UP1")
    assert sub.owner_token == ""
    assert sub.assigned_tokens == []


def test_user_subscription_default_owner_token() -> None:
    sub = UserSubscription(user_id="u456", name="XHS1")
    assert sub.owner_token == ""
    assert sub.assigned_tokens == []


def test_bili_subscription_with_owner() -> None:
    sub = BiliSubscription(
        uid=100, name="UP1", owner_token="bili-admin", assigned_tokens=["reader"]
    )
    assert sub.owner_token == "bili-admin"
    assert sub.assigned_tokens == ["reader"]


def test_api_token_entry_no_resource_rules() -> None:
    """#108 删除 ApiTokenEntry.resource_rules 字段。"""
    entry = ApiTokenEntry(name="x", token_hash="h")
    # resource_rules 字段不存在（attr 访问抛 AttributeError）
    try:
        _ = entry.resource_rules  # type: ignore[attr-defined]
        raise AssertionError("resource_rules should be removed")
    except AttributeError:
        pass
