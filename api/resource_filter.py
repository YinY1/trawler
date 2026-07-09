"""token ownership 视图与订阅/消息可见性 helper（issue #108）。

本模块是路由层「消息 / 订阅可见性」判断的唯一集中点：
- ``TokenOwnership``：token 的 ownership 视图（是否 superuser + token name），
  路由层调 ``has_sub_access`` / ``has_sub_write`` 判断
- ``filter_subscription_dict`` / ``subscription_visible``：订阅可见性 helper
- ``message_visible`` / ``msg_id_visible``：消息可见性 helper（需 config 反查 sub）

所有 helper 都是纯逻辑（无 IO、无 LLM、无外部副作用）。

issue #108 废弃 #106 的 ResourceRules（platforms + subscription_refs AND 过滤），
改为更直观的 owner/assigned 模型（决策 #1/#2）。
"""

from __future__ import annotations

# pyright: basic
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.subscription_cli import PLATFORM_TO_SECTION

if TYPE_CHECKING:
    from shared.config import ApiTokenEntry, BiliSubscription, Config, UserSubscription
    from shared.protocols import MessageRecord


# ═══════════════════════════════════════════════════════════
# 平台映射常量（与 #106 保持一致，全文唯一来源）
# ═══════════════════════════════════════════════════════════

#: TOML section 全名 → CLI short name（bilibili → bili）。
SECTION_TO_SHORT: dict[str, str] = {v: k for k, v in PLATFORM_TO_SECTION.items()}

#: short name → 订阅主键字段（spec §7.3）。
SHORT_TO_KEY_FIELD: dict[str, str] = {"bili": "uid", "xhs": "user_id", "weibo": "user_id"}


# ═══════════════════════════════════════════════════════════
# TokenOwnership — token 的 ownership 视图
# ═══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TokenOwnership:
    """token ownership 视图（issue #108）。

    不可变（``frozen=True``），从 ``ApiTokenEntry`` 一次性构造，路由层调
    ``has_sub_access`` / ``has_sub_write`` 判断可见性。

    - ``is_superuser``: token 是否持 ``tokens:manage`` scope（bypass 所有检查）
    - ``token_name``: token 的 name（与 sub.owner_token / sub.assigned_tokens 比对）
    """

    is_superuser: bool
    token_name: str

    @classmethod
    def from_token(cls, token: ApiTokenEntry) -> TokenOwnership:
        """从 ``ApiTokenEntry`` 构造（查 scopes 判断 superuser）。"""
        from api.auth import SCOPE_TOKENS_MANAGE, token_has_scope

        return cls(
            is_superuser=token_has_scope(token, SCOPE_TOKENS_MANAGE),
            token_name=token.name,
        )

    @classmethod
    def unrestricted(cls, token_name: str = "") -> TokenOwnership:
        """全权限视图（仅供测试 / Web session 等价场景使用）。

        生产中只有持 ``tokens:manage`` 的 token 才是 superuser，本工厂方法
        用于测试 fixture 构造 superuser client，或 Web session 路由（session
        登录 = admin = superuser 等价）。
        """
        return cls(is_superuser=True, token_name=token_name)

    def has_sub_access(self, sub: BiliSubscription | UserSubscription) -> bool:
        """读权限：token 能否看到此 sub（spec §5.1）。

        ``is_superuser OR sub.owner_token == token_name
        OR token_name in sub.assigned_tokens``
        """
        if self.is_superuser:
            return True
        if sub.owner_token == self.token_name:
            return True
        return self.token_name in sub.assigned_tokens

    def has_sub_write(self, sub: BiliSubscription | UserSubscription) -> bool:
        """写权限：token 能否改/删/绑 endpoint 此 sub（spec §5.1）。

        ``is_superuser OR sub.owner_token == token_name``
        （assigned 不能写！）
        """
        if self.is_superuser:
            return True
        return sub.owner_token == self.token_name

    def can_manage_assign(self) -> bool:
        """assign/unassign 路由专用：仅 superuser（spec §5.2）。

        连 owner 也不能分配自己的 sub 给别的 token，决策 #10。
        """
        return self.is_superuser


# ═══════════════════════════════════════════════════════════
# 订阅可见性 helper（GET /subscriptions 过滤 + 写入路由越权判断）
# ═══════════════════════════════════════════════════════════


def filter_subscription_dict(
    result: dict[str, list[dict]],
    ownership: TokenOwnership,
    config: Config,
) -> dict[str, list[dict]]:
    """过滤 ``list_subscriptions`` 的原始返回（issue #108）。

    ``result`` 的 key 是 TOML section 全名（bilibili/xiaohongshu/weibo）。
    对每条 sub dict，用主键反查 ``config`` 拿到真实 sub 对象（含 owner_token /
    assigned_tokens），调 ``ownership.has_sub_access`` 判断可见性。

    superuser 看全部；owner/assigned 看自己的；outvisitor 看不到。
    越权 sub 不出现在响应里（不暴露存在性）。

    superuser bypass：``ownership.is_superuser`` 直接返回原始 ``result``，
    不反查 sub（让响应完整透传 superuser 看到的全部 sub）。
    """
    from shared.protocols import find_subscription_by_ref

    if ownership.is_superuser:
        return result
    out: dict[str, list[dict]] = {}
    for section, subs in result.items():
        short = SECTION_TO_SHORT.get(section)
        if short is None:
            continue  # 未知 section 保守丢弃
        key_field = SHORT_TO_KEY_FIELD.get(short, "")
        kept: list[dict] = []
        for s in subs:
            sub_id = str(s.get(key_field, ""))
            sub_obj = find_subscription_by_ref(config, short, sub_id)
            if sub_obj is None:
                continue  # config 里查不到（数据不一致），保守丢弃避免越权泄漏
            if ownership.has_sub_access(sub_obj):
                kept.append(s)
        if kept:
            out[section] = kept
    return out


def subscription_visible(
    ownership: TokenOwnership,
    config: Config,
    platform_full: str,
    identifier: str | int,
    require_write: bool = False,
) -> bool:
    """订阅是否在 token 的 ownership 内（写入路由越权判断用）。

    ``platform_full`` 是路由 URL 段，优先按 TOML section 全名解析（bilibili），
    fallback 接受 short name（bili）—— 与 #106 历史兼容。

    ``require_write=True`` 时用 ``has_sub_write``（assigned 不能写），
    否则用 ``has_sub_access``（assigned 可读）。

    越权时调用方合并成「未找到」语义（200 + success=False），不暴露存在性。

    superuser bypass：``ownership.is_superuser`` 直接返回 True，不反查 sub
    （让业务函数自行报「未找到」，superuser 不被 ownership 层拦）。
    """
    from shared.protocols import find_subscription_by_ref

    if ownership.is_superuser:
        return True
    short = SECTION_TO_SHORT.get(platform_full)
    if short is None and platform_full in PLATFORM_TO_SECTION:
        short = platform_full
    if short is None:
        return False
    sub_obj = find_subscription_by_ref(config, short, str(identifier))
    if sub_obj is None:
        return False
    if require_write:
        return ownership.has_sub_write(sub_obj)
    return ownership.has_sub_access(sub_obj)


# ═══════════════════════════════════════════════════════════
# 消息可见性 helper（GET /messages 过滤 + rerun 越权判断）
# ═══════════════════════════════════════════════════════════


def message_visible(
    ownership: TokenOwnership,
    config: Config,
    msg: MessageRecord,
) -> bool:
    """单条消息是否可见（issue #108）。

    msg → subscription_ref 反查 sub → ownership.has_sub_access。
    **无主消息**（``msg.subscription_ref == ""`` 或反查不到 sub）：
    - superuser 可见
    - 非 superuser 不可见（404 / 过滤掉，不暴露存在性）
    """
    from shared.protocols import find_subscription_by_ref

    if ownership.is_superuser:
        return True
    if not msg.subscription_ref:
        return False  # 无主消息只 superuser 可见
    sub_obj = find_subscription_by_ref(config, msg.platform, msg.subscription_ref)
    if sub_obj is None:
        return False  # 反查不到 sub，保守不可见
    return ownership.has_sub_access(sub_obj)


def msg_id_visible(
    ownership: TokenOwnership,
    msg_id: str,
) -> bool:
    """``msg_id`` 维度可见性（fetch 路由专用，issue #108）。

    fetch 是按需抓取，消息可能还没入库，无法 msg→sub 反查。**只有 superuser
    能调 fetch**（决策：无主消息无法判断 owner，普通 token 调 fetch 等同越权）。

    普通 token 调 fetch → 403（路由层直接拦，不走到这里）。
    本函数仅供 superuser 调用时做防御性检查（永远 True）。
    """
    return ownership.is_superuser
