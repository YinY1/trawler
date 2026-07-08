"""token 行级过滤视图与订阅可见性 helper（issue #106 — spec §6 / plan T2）。

本模块是路由层「消息 / 订阅可见性」判断的唯一集中点：
- ``TokenResourceFilter``：token 行级规则的不可变视图（从 ``ApiTokenEntry``
  一次性构造），路由层调 ``allows_*`` 三方法判断消息/订阅/平台可见性
- ``SECTION_TO_SHORT`` / ``SHORT_TO_KEY_FIELD``：平台映射常量（全文唯一来源，
  从 ``core.subscription_cli.PLATFORM_TO_SECTION`` 反推，禁止在路由文件里
  inline 重定义同一映射）
- ``filter_subscription_dict`` / ``subscription_visible``：订阅可见性 helper
  （路由层 GET /subscriptions / 写入路由越权处理用，避免重复 inline）

所有 helper 都是纯逻辑（无 IO、无 LLM、无外部副作用）。
"""

from __future__ import annotations

# pyright: basic
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.subscription_cli import PLATFORM_TO_SECTION

if TYPE_CHECKING:
    from shared.config import ApiTokenEntry
    from shared.protocols import MessageRecord


# ═══════════════════════════════════════════════════════════
# 平台映射常量（全文唯一来源，T4/T5 路由 import 复用，禁止 inline 重定义）
# ═══════════════════════════════════════════════════════════

#: TOML section 全名 → CLI short name（``bilibili`` → ``bili``）。
#: 从 ``core.subscription_cli.PLATFORM_TO_SECTION`` 反推，避免两处手写同一映射（DRY）。
SECTION_TO_SHORT: dict[str, str] = {v: k for k, v in PLATFORM_TO_SECTION.items()}

#: short name → 订阅主键字段（spec §7.3）。
#: ``bili`` 主键是 ``uid``（int），``xhs`` / ``weibo`` 主键是 ``user_id``（str）。
SHORT_TO_KEY_FIELD: dict[str, str] = {"bili": "uid", "xhs": "user_id", "weibo": "user_id"}


# ═══════════════════════════════════════════════════════════
# TokenResourceFilter — token 行级规则的不可变视图
# ═══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TokenResourceFilter:
    """token 行级过滤视图（spec §6.2）。

    不可变（``frozen=True``），从 token ``ResourceRules`` 一次性构造，路由层
    调 ``allows_*`` 方法判断可见性。``None`` 字段（``platforms`` /
    ``subscription_refs``）表示**不限制**该维度；空 ``frozenset`` 表示
    **拒绝一切**（与 ``None=全权限`` 相反，见 spec §5.3）。
    """

    platforms: frozenset[str] | None
    subscription_refs: frozenset[str] | None

    @classmethod
    def from_token(cls, token: ApiTokenEntry) -> TokenResourceFilter:
        """从 token 的 ``ResourceRules`` 构造（``list`` → ``frozenset``）。"""
        rules = token.resource_rules
        return cls(
            platforms=frozenset(rules.platforms)
            if rules.platforms is not None
            else None,
            subscription_refs=frozenset(rules.subscription_refs)
            if rules.subscription_refs is not None
            else None,
        )

    @classmethod
    def unrestricted(cls) -> TokenResourceFilter:
        """全权限视图（无任何限制）。"""
        return cls(platforms=None, subscription_refs=None)

    def allows_platform(self, platform: str) -> bool:
        """platform 是否可见。

        ``platforms is None`` → 不限平台（全可见）；否则 ``platform in self.platforms``。
        空 ``frozenset`` → 任何平台都 False（拒绝一切，spec §5.3）。
        """
        if self.platforms is None:
            return True
        return platform in self.platforms

    def allows_subscription(self, platform: str, subscription_ref: str) -> bool:
        """订阅是否可见。

        ``subscription_ref`` 是 **detector 注入的原始值**（不带 ``<platform>:``
        前缀，如 ``"100"`` / ``"u456"``）。内部拼 ``f"{platform}:{subscription_ref}"``
        复合 key 再比对 token 的 ``subscription_refs`` 集合（spec §5.4）。
        """
        if self.subscription_refs is None:
            return True
        composite = f"{platform}:{subscription_ref}"
        return composite in self.subscription_refs

    def allows_message(self, msg: MessageRecord) -> bool:
        """消息是否可见（platform + subscription_ref 两维 AND 组合，spec §5.2）。

        两维都通过才可见：``platform`` 维度先粗筛，``subscription_ref`` 维度
        细筛。``platforms=["bili"]`` + ``subscription_refs=["xhs:u456"]`` 这种
        无意义组合会拒绝一切（CLI 创建时 warning 但不强制阻止，见 plan T6）。
        """
        return self.allows_platform(msg.platform) and self.allows_subscription(
            msg.platform, msg.subscription_ref
        )


# ═══════════════════════════════════════════════════════════
# 订阅可见性 helper（路由层 GET /subscriptions 与写入路由越权处理复用）
# ═══════════════════════════════════════════════════════════


def filter_subscription_dict(
    result: dict[str, list[dict]], filt: TokenResourceFilter
) -> dict[str, list[dict]]:
    """过滤 ``list_subscriptions`` 的原始返回（spec §7.3）。

    ``result`` 的 key 是 TOML section 全名（``bilibili`` / ``xiaohongshu`` /
    ``weibo``），通过 ``SECTION_TO_SHORT`` 反查 short name 后：

    1. 平台维度：section 对应 short 不在 ``filt.platforms`` → 整个 section 丢弃
    2. 订阅维度：section 内每条订阅的主键（``uid`` / ``user_id``）拼复合 key
       比对 ``filt.subscription_refs``；空 section 不写出

    ``filt.subscription_refs is None`` → 整个 section 全保留（不细筛）。
    """
    out: dict[str, list[dict]] = {}
    for section, subs in result.items():
        short = SECTION_TO_SHORT.get(section)
        # 未知 section（非已知平台）→ 保守丢弃，避免越权泄漏
        if short is None or not filt.allows_platform(short):
            continue
        if filt.subscription_refs is None:
            out[section] = subs
            continue
        key_field = SHORT_TO_KEY_FIELD.get(short, "")
        kept: list[dict] = []
        for s in subs:
            sub_id = str(s.get(key_field, ""))
            if filt.allows_subscription(short, sub_id):
                kept.append(s)
        if kept:
            out[section] = kept
    return out


def subscription_visible(
    filt: TokenResourceFilter, platform_full: str, identifier: str | int
) -> bool:
    """订阅是否在 token 的行级权限内（写入路由越权判断用，spec §8.3）。

    ``platform_full`` 是路由 URL 段的 TOML section 全名（如 ``bilibili``），
    ``identifier`` 是订阅主键（``uid`` / ``user_id``）。内部走
    ``SECTION_TO_SHORT`` 反查 short name 再交给 ``filt.allows_subscription``。

    与 ``filter_subscription_dict`` 区别：本函数返回 ``bool``（单条订阅判断），
    供 ``DELETE /subscriptions/...`` / ``bind/unbind endpoint`` 等写入路由
    越权时合并成「未找到」语义，不暴露存在性。
    """
    short = SECTION_TO_SHORT.get(platform_full)
    if short is None:
        return False
    return filt.allows_subscription(short, str(identifier))
