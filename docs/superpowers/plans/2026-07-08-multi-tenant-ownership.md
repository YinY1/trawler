# 多租户订阅所有权模型 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给每个订阅引入 `owner_token` + `assigned_tokens` 双字段 ownership 模型，替代 #106 的 `ResourceRules`，实现 owner 全权 / assigned 只读 / superuser bypass 的三态权限。

**Architecture:** 数据层（sub 加字段）+ 视图层（`TokenOwnership` 替代 `TokenResourceFilter`）+ 路由层（`get_token_ownership` 依赖 + 各路由 ownership 校验）+ CLI（删 resource flag + 加 adopt）+ 测试（4 份 authed_client → superuser_client，3 份 row_filtered_client → owner/assigned/outsider 矩阵）。

**Tech Stack:** Python 3.14, FastAPI Security 依赖, dataclass, tomlkit, Click, pytest + httpx AsyncClient

**关联 spec:** `docs/superpowers/specs/2026-07-08-multi-tenant-ownership-design.md`

---

## 文件结构

### 新建
- `tests/test_ownership.py` — `TokenOwnership` 纯逻辑测试（替换 `tests/test_resource_filter.py`）

### 重写（文件保留，内容全换）
- `api/resource_filter.py` — `TokenResourceFilter` → `TokenOwnership`，helper 全换 ownership 模型

### 修改
- `shared/config.py` — 删 `ResourceRules` dataclass；删 `ApiTokenEntry.resource_rules` 字段；`BiliSubscription` / `UserSubscription` 加 `owner_token` + `assigned_tokens`
- `shared/protocols.py` — 无需改（`find_subscription_by_ref` 已存在且返回带新字段的 sub 对象）
- `web/auth.py` — 删 `_resource_rules_from_dict`；`load_auth_config` / `save_auth_config` 删 resource_rules I/O
- `api/auth.py` — 删 `create_token(resource_rules=)`；`get_resource_filter` 重写为 `get_token_ownership` 返回 `TokenOwnership`；`token_has_scope` 删空 scopes 全权限分支；`SCOPE_TOKENS_MANAGE` 注释更新
- `api/schemas.py` — 新增 `AssignRequest`
- `api/routes/subscriptions.py` — filt → ownership；新增 assign/unassign 路由
- `api/routes/messages.py` — filt → ownership；fetch 加 superuser-only 检查
- `api/token_tool.py` — 删 `--resource-platform` / `--resource-sub` flag + Resource Rules 列；新增 `adopt` 子命令；create 空 scopes 改 red warning
- `core/subscription_cli.py` — `add_subscription` 加 owner_token 参数；新增 `assign_token_to_subscription` / `unassign_token_from_subscription` / `set_subscription_owner`
- `config/subscriptions.toml.example` — 加 owner_token / assigned_tokens 注释示例
- `tests/test_api_check.py` — authed_client → superuser_client
- `tests/test_api_messages.py` — authed_client → superuser_client；删 row_filtered_client + TestRowLevel*；加 tmp_config_with_owned_sub + owner/assigned/outsider 矩阵
- `tests/test_api_subscriptions.py` — 同上
- `tests/test_api_fetch.py` — 同上 + fetch superuser-only 测试
- `tests/test_api_auth.py` — 删 TestResourceRulesData + ResourceFilterDep 部分；authed_client → superuser_client；加 token_has_scope 空语义测试
- `tests/test_api_token_tool.py` — 删 TestCreateResourceRules + TestListResourceRules；加 adopt 命令测试

### 删除
- `tests/test_resource_filter.py` — 整文件删

---

## Task 1: 数据模型变更 — sub 加字段 + 删 ResourceRules dataclass

**Files:**
- Modify: `shared/config.py:206-216`（BiliSubscription / UserSubscription 加字段）
- Modify: `shared/config.py:248-285`（删 ResourceRules + ApiTokenEntry.resource_rules）
- Modify: `config/subscriptions.toml.example`（加注释示例）

**目标:** 数据层先就位，但暂不动 I/O 和路由（后续 task 改）。

- [ ] **Step 1.1: 写失败测试 — BiliSubscription 默认值**

Create `tests/test_config_ownership_fields.py` (临时单测，确认 dataclass 字段就位；Task 7 整理会删/合并):

```python
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
```

- [ ] **Step 1.2: 运行测试确认失败**

Run: `uv run pytest tests/test_config_ownership_fields.py -v`
Expected: FAIL（`BiliSubscription.__init__()` 不接受 `owner_token` 参数；`ApiTokenEntry` 仍有 `resource_rules`）

- [ ] **Step 1.3: BiliSubscription / UserSubscription 加字段**

Edit `shared/config.py:206-216`:

```python
@dataclass
class BiliSubscription:
    uid: int = 0
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)
    owner_token: str = ""  # issue #108: 创建者 token name，全权 CRUD
    assigned_tokens: list[str] = field(default_factory=list)  # issue #108: 被分配 token，只读


@dataclass
class UserSubscription:
    user_id: str = ""
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)
    owner_token: str = ""  # issue #108
    assigned_tokens: list[str] = field(default_factory=list)  # issue #108
```

- [ ] **Step 1.4: 删 ResourceRules dataclass + ApiTokenEntry.resource_rules 字段**

Edit `shared/config.py:248-285`（删除整个 ResourceRules dataclass 248-263 行 + ApiTokenEntry 里 resource_rules 字段）:

```python
# 删除这段（248-263）：
# @dataclass
# class ResourceRules:
#     """token 行级过滤规则（issue #106 spec §4）。..."""
#     platforms: list[str] | None = None
#     subscription_refs: list[str] | None = None


@dataclass
class ApiTokenEntry:
    """API token 条目（``data/auth.toml`` 的 ``[[api_tokens]]`` AoT 行）。

    ``scopes`` 空 list 在 #108 后**不再 = 全权限**（破坏性变更，见 spec §6.2）。
    要成为 superuser 必须显式持 ``tokens:manage`` scope。

    issue #108 废弃 ``resource_rules`` 字段（趁 #107 未部署直接删）。
    ownership 由 sub 上的 ``owner_token`` / ``assigned_tokens`` 表达。
    """

    name: str
    token_hash: str  # SHA-256 hexdigest
    created_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    # resource_rules 字段删除（issue #108，趁 #107 未部署）
```

- [ ] **Step 1.5: 更新 config/subscriptions.toml.example**

Edit `config/subscriptions.toml.example`，在三个平台的示例块加注释：

```toml
# ── B 站（Bilibili） ────────────────────────────────────────────
# 每个 [[bilibili.subscriptions]] 代表一个订阅的 UP 主
# 取消注释并添加 UP 主的 UID 和名称：

# [[bilibili.subscriptions]]
# uid = 123456
# name = "示例UP主"
# notify_endpoints = ["default"]
# owner_token = "bili-admin-bot"          # issue #108: 创建者 token name（全权 CRUD）
# assigned_tokens = ["reader-bot-1"]      # issue #108: 被分配 token（只读访问）

# ── 小红书（Xiaohongshu） ───────────────────────────────────────
# [[xiaohongshu.subscriptions]]
# user_id = ""
# name = ""
# notify_endpoints = ["default"]
# owner_token = ""
# assigned_tokens = []

# ── 微博（Weibo） ───────────────────────────────────────────────
# [[weibo.subscriptions]]
# user_id = ""
# name = ""
# notify_endpoints = ["default"]
# owner_token = ""
# assigned_tokens = []
```

- [ ] **Step 1.6: 运行测试确认通过**

Run: `uv run pytest tests/test_config_ownership_fields.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 1.7: 提交**

```bash
git add shared/config.py config/subscriptions.toml.example tests/test_config_ownership_fields.py
git commit -m "feat(#108): add owner_token/assigned_tokens to subscriptions, drop ResourceRules dataclass

- BiliSubscription / UserSubscription: add owner_token='' / assigned_tokens=[] fields
- ApiTokenEntry: remove resource_rules field (趁 #107 未部署直接删)
- Remove ResourceRules dataclass entirely
- subscriptions.toml.example: document new ownership fields"
```

---

## Task 2: token_has_scope 破坏性变更 — 删空 scopes 全权限 + SCOPE_TOKENS_MANAGE 注释

**Files:**
- Modify: `api/auth.py:48-50`（SCOPE_TOKENS_MANAGE 注释）
- Modify: `api/auth.py:82-90`（token_has_scope 删 `if not token.scopes` 分支）

**依赖:** Task 1 完成（ApiTokenEntry 已无 resource_rules）

**目标:** 空 scopes 不再 = 全权限，必须显式持 tokens:manage 才是 superuser。

- [ ] **Step 2.1: 写失败测试 — 空 scopes 无权**

Edit `tests/test_api_auth.py`，在文件末尾追加（先确认文件末尾位置）:

```python
# ═══════════════════════════════════════════════════════════
# token_has_scope 空 scopes 语义（issue #108 破坏性变更）
# ═══════════════════════════════════════════════════════════


class TestTokenHasScopeEmptyScopes:
    """#108 后空 scopes 不再 = 全权限（spec §6.2）。"""

    def test_empty_scopes_denies_messages_read(self) -> None:
        """空 scopes token 对 messages:read 返回 False（#105 是 True，#108 改 False）。"""
        from api.auth import SCOPE_MESSAGES_READ, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, SCOPE_MESSAGES_READ) is False

    def test_empty_scopes_denies_subscriptions_write(self) -> None:
        from api.auth import SCOPE_SUBSCRIPTIONS_WRITE, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, SCOPE_SUBSCRIPTIONS_WRITE) is False

    def test_empty_scopes_denies_tokens_manage(self) -> None:
        """空 scopes 连 tokens:manage 都没有 → 不是 superuser。"""
        from api.auth import SCOPE_TOKENS_MANAGE, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(name="x", token_hash="h", scopes=[])
        assert token_has_scope(token, SCOPE_TOKENS_MANAGE) is False

    def test_tokens_manage_grants_superuser_scope(self) -> None:
        """持 tokens:manage 的 token 对 tokens:manage 返回 True（superuser 标识）。"""
        from api.auth import SCOPE_TOKENS_MANAGE, token_has_scope
        from shared.config import ApiTokenEntry

        token = ApiTokenEntry(
            name="admin", token_hash="h", scopes=["tokens:manage"]
        )
        assert token_has_scope(token, SCOPE_TOKENS_MANAGE) is True
```

- [ ] **Step 2.2: 运行测试确认失败**

Run: `uv run pytest tests/test_api_auth.py::TestTokenHasScopeEmptyScopes -v`
Expected: FAIL（前 3 个测试 — 当前 `if not token.scopes: return True` 让空 scopes 通过）

- [ ] **Step 2.3: 删 token_has_scope 空 scopes 分支 + 更新 SCOPE_TOKENS_MANAGE 注释**

Edit `api/auth.py:48-50`（SCOPE_TOKENS_MANAGE 注释）:

```python
#: 占位 scope（spec §3、§12）。**本 PR 不在路由层消费**，
#: 仅供未来 ``tokens:manage`` HTTP endpoint 或 CLI 校验白名单引用。
SCOPE_TOKENS_MANAGE = "tokens:manage"
```

改为:

```python
#: superuser 标识 scope（issue #108）。持此 scope 的 token bypass 所有
#: owner/assigned 检查，看全部 sub / endpoint / messages。
#: 同时也是 assign/unassign/adopt 路由的必需 scope。
SCOPE_TOKENS_MANAGE = "tokens:manage"
```

Edit `api/auth.py:82-90`（token_has_scope）:

```python
def token_has_scope(token: ApiTokenEntry, required: str) -> bool:
    """token 是否满足 required scope（issue #108 破坏性变更）。

    **#108 变更**：空 scopes 不再 = 全权限。#105 设计「空 = 全权限」是为了
    向后兼容老 token，但实际部署中没人创建空 scope token（CLI 默认就提示）。
    #108 把 superuser 收紧为「显式持 tokens:manage」，空 scopes token 无任何权限。

    要成为 superuser：token.scopes 必须包含 ``tokens:manage``。
    """
    return any(scope_implies(g, required) for g in token.scopes)
```

M5 修订（issue #108 review）：``token_has_scope`` 删空 scopes 分支后，
``require_scopes`` docstring（api/auth.py:171-173）仍在说
「``token.scopes == []``（空 list）= 全权限（spec §5），任何 required 都放行」——
这在 #108 后是错的（空 = 无权）。同步更新 ``require_scopes`` docstring：

Edit `api/auth.py:156-179`（require_scopes docstring）:

将 docstring 中的这段:
```text
    特殊情况：
    - ``security_scopes.scopes == ()``（路由不要求 scope）→ 行为等价 ``require_token``
    - ``token.scopes == []``（空 list）= 全权限（spec §5），任何 required 都放行
```

改为:
```text
    特殊情况：
    - ``security_scopes.scopes == ()``（路由不要求 scope）→ 行为等价 ``require_token``
    - issue #108 破坏性变更：``token.scopes == []``（空 list）**不再** = 全权限，
      空 scopes token 任何 required scope 都被拒（403）。
      要 superuser 必须显式持 ``tokens:manage``（见 ``token_has_scope``）。
```

- [ ] **Step 2.4: 运行新测试确认通过**

Run: `uv run pytest tests/test_api_auth.py::TestTokenHasScopeEmptyScopes -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 2.5: 确认其它测试现在开始失败（预期，后续 Task 修复）**

Run: `uv run pytest tests/test_api_messages.py -v 2>&1 | tail -30`
Expected: 大量 401/403 失败（所有 authed_client fixture 空 scopes → 无权）—— 这是预期，Task 7 修 fixture。

- [ ] **Step 2.6: 提交**

```bash
git add api/auth.py tests/test_api_auth.py
git commit -m "feat(#108)!: empty scopes no longer grant full access

BREAKING CHANGE: token_has_scope removes 'empty scopes = full access' branch.
Tokens must explicitly hold 'tokens:manage' to be superuser.
Update SCOPE_TOKENS_MANAGE comment to reflect superuser role.

All authed_client fixtures will be migrated in Task 7."
```

---

## Task 3: api/resource_filter.py 重写 + api/auth.py 视图依赖

**Files:**
- Rewrite: `api/resource_filter.py`（TokenResourceFilter → TokenOwnership）
- Modify: `api/auth.py:28-29,182-199,202-229`（import + get_resource_filter → get_token_ownership + create_token 删 resource_rules）

**依赖:** Task 1 + Task 2 完成

**目标:** 视图层就位 — `TokenOwnership` dataclass + `get_token_ownership` FastAPI 依赖。路由层暂不动（Task 4-5 改）。

- [ ] **Step 3.1: 写失败测试 — TokenOwnership 纯逻辑**

Create `tests/test_ownership.py`:

```python
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
```

- [ ] **Step 3.2: 删 tests/test_resource_filter.py**

```bash
git rm tests/test_resource_filter.py
```

- [ ] **Step 3.3: 运行新测试确认失败**

Run: `uv run pytest tests/test_ownership.py -v`
Expected: FAIL（`ImportError: cannot import name 'TokenOwnership' from 'api.resource_filter'`）

- [ ] **Step 3.4: 重写 api/resource_filter.py**

Overwrite `api/resource_filter.py` 完整内容（参考 spec §7.1）:

```python
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
    """
    from shared.protocols import find_subscription_by_ref

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
    """
    from shared.protocols import find_subscription_by_ref

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
```

- [ ] **Step 3.5: 修改 api/auth.py import + get_resource_filter → get_token_ownership + create_token 删 resource_rules**

Edit `api/auth.py:28-29`:

```python
from api.resource_filter import TokenResourceFilter
from shared.config import ApiTokenEntry, ResourceRules
```

改为:

```python
from api.resource_filter import TokenOwnership
from shared.config import ApiTokenEntry
```

Edit `api/auth.py:182-199`（get_resource_filter 重写为 get_token_ownership）:

```python
async def get_resource_filter(
    security_scopes: SecurityScopes,
    request: Request,
) -> TokenResourceFilter:
    """FastAPI 依赖：scope 校验 + 行级过滤视图构造（spec §6.3）。
    ..."""
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return TokenResourceFilter.from_token(entry)
```

改为:

```python
async def get_token_ownership(
    security_scopes: SecurityScopes,
    request: Request,
) -> TokenOwnership:
    """FastAPI 依赖：scope 校验 + ownership 视图构造（issue #108）。

    一个依赖同时承担两层职责（``Security(get_token_ownership, scopes=[...])``）：

    - 无 header / token 不匹配 → 401（同 ``require_scopes``）
    - 缺 scope → 403（同 ``require_scopes``）
    - 通过 → 返回 ``TokenOwnership.from_token(entry)``（含 token 的 ownership 视图）

    路由层用 ``ownership.has_sub_access(sub)`` / ``has_sub_write(sub)`` 判断
    可见性，不直接读 ``ApiTokenEntry.scopes``（避免路由层处理 scope 解析逻辑，
    集中到 ``TokenOwnership``）。

    issue #108 重命名自 ``get_resource_filter``（#106 命名），保留旧名别名
    减少 import 改动，但内部返回 ``TokenOwnership``。
    """
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return TokenOwnership.from_token(entry)


# 向后兼容别名（#106 历史命名，#108 重命名后保留别名减少 import 改动）
get_resource_filter = get_token_ownership
```

Edit `api/auth.py:202-229`（create_token 删 resource_rules 参数）:

```python
def create_token(
    name: str,
    scopes: list[str] | None = None,
    resource_rules: ResourceRules | None = None,
    auth_path: Path = AUTH_TOML_PATH,
) -> str:
    """生成新 token，hash 后存 ``data/auth.toml``，返回明文（仅此一次）。
    ..."""
    plain = secrets.token_urlsafe(32)
    cfg = load_auth_config()
    cfg.api_tokens = [t for t in cfg.api_tokens if t.name != name]
    cfg.api_tokens.append(
        ApiTokenEntry(
            name=name,
            token_hash=_hash_token(plain),
            created_at=datetime.now(timezone.utc).timestamp(),
            scopes=list(scopes) if scopes else [],
            resource_rules=resource_rules or ResourceRules(),
        )
    )
    save_auth_config(cfg)
    return plain
```

改为:

```python
def create_token(
    name: str,
    scopes: list[str] | None = None,
    auth_path: Path = AUTH_TOML_PATH,
) -> str:
    """生成新 token，hash 后存 ``data/auth.toml``，返回明文（仅此一次）。

    同名 token 覆盖（先删后加），保证唯一性。
    ``scopes`` 为 None 或空 list → 空 list 落盘（#108 后空 = 无权，spec §6.2）。
    要创建 superuser token 需显式传 ``scopes=["tokens:manage"]``。
    ``auth_path`` 参数供测试 monkeypatch。

    issue #108 删除 ``resource_rules`` 参数（趁 #107 未部署）。
    """
    plain = secrets.token_urlsafe(32)
    cfg = load_auth_config()
    cfg.api_tokens = [t for t in cfg.api_tokens if t.name != name]
    cfg.api_tokens.append(
        ApiTokenEntry(
            name=name,
            token_hash=_hash_token(plain),
            created_at=datetime.now(timezone.utc).timestamp(),
            scopes=list(scopes) if scopes else [],
        )
    )
    save_auth_config(cfg)
    return plain
```

- [ ] **Step 3.6: 删 web/auth.py 的 resource_rules I/O**

Edit `web/auth.py:31`:

```python
from shared.config import ApiTokenEntry, ResourceRules, WebAuthConfig
```

改为:

```python
from shared.config import ApiTokenEntry, WebAuthConfig
```

Edit `web/auth.py:74-88`（删 `_resource_rules_from_dict` 函数整段）:

```python
def _resource_rules_from_dict(raw: object) -> ResourceRules:
    """..."""
    if not isinstance(raw, dict):
        return ResourceRules()
    platforms_raw = raw.get("platforms")
    subs_raw = raw.get("subscription_refs")
    return ResourceRules(
        platforms=list(platforms_raw) if platforms_raw is not None else None,
        subscription_refs=list(subs_raw) if subs_raw is not None else None,
    )
```

整段删除（74-88 行）。

Edit `web/auth.py:101-111`（load_auth_config 的 token 解析）:

```python
    api_tokens: list[ApiTokenEntry] = [
        ApiTokenEntry(
            name=t["name"],
            token_hash=t["token_hash"],
            created_at=t.get("created_at", 0.0),
            scopes=list(t.get("scopes", [])),
            resource_rules=_resource_rules_from_dict(t.get("resource_rules", {})),
        )
        for t in api_tokens_raw
        if isinstance(t, dict) and "name" in t and "token_hash" in t
    ]
```

改为:

```python
    api_tokens: list[ApiTokenEntry] = [
        ApiTokenEntry(
            name=t["name"],
            token_hash=t["token_hash"],
            created_at=t.get("created_at", 0.0),
            scopes=list(t.get("scopes", [])),
        )
        for t in api_tokens_raw
        if isinstance(t, dict) and "name" in t and "token_hash" in t
    ]
```

Edit `web/auth.py:142-157`（save_auth_config 的 resource_rules 嵌套 table 写出整段删除）:

```python
        # resource_rules 嵌套 table：非默认（两字段任一非 None）才写。
        # 默认 ResourceRules() 省略 section，保持老 token 文件 diff 干净。
        rules = t.resource_rules
        if rules.platforms is not None or rules.subscription_refs is not None:
            nested = tomlkit.table()
            if rules.platforms is not None:
                p_arr = tomlkit.array()
                for p in rules.platforms:
                    p_arr.append(p)
                nested["platforms"] = p_arr
            if rules.subscription_refs is not None:
                s_arr = tomlkit.array()
                for sub in rules.subscription_refs:
                    s_arr.append(sub)
                nested["subscription_refs"] = s_arr
            entry["resource_rules"] = nested
```

整段删除。

- [ ] **Step 3.7: 运行 ownership 测试确认通过**

Run: `uv run pytest tests/test_ownership.py -v`
Expected: PASS（所有测试通过）

- [ ] **Step 3.8: 运行 ruff + pyright 确认无 lint/type 错误**

Run: `uv run ruff check api/resource_filter.py api/auth.py web/auth.py tests/test_ownership.py`
Expected: 无错误（如果 `ResourceRules` 还在 import 需清理）

Run: `uv run pyright`
Expected: 无新增 error

- [ ] **Step 3.9: 提交**

```bash
git add api/resource_filter.py api/auth.py web/auth.py tests/test_ownership.py
git rm tests/test_resource_filter.py
git commit -m "feat(#108): rewrite api/resource_filter.py with TokenOwnership view

- Replace TokenResourceFilter with TokenOwnership (is_superuser + token_name)
- from_token() detects tokens:manage scope as superuser flag
- has_sub_access / has_sub_write implement owner/assigned/superuser 3-state model
- filter_subscription_dict / subscription_visible / message_visible helpers
- get_resource_filter → get_token_ownership (alias kept for backward compat)
- create_token: drop resource_rules parameter
- web/auth.py: remove _resource_rules_from_dict + resource_rules I/O
- Delete tests/test_resource_filter.py, add tests/test_ownership.py"
```

---

## Task 4: sub 路由迁移 — list/add/remove/bind/unbind 加 ownership 校验 + assign/unassign 新路由

**Files:**
- Modify: `api/routes/subscriptions.py`（全文件改造 + 新增路由）
- Modify: `api/schemas.py`（新增 `AssignRequest`）
- Modify: `core/subscription_cli.py`（add_subscription 加 owner_token + 新增 assign/unassign/set_owner）

**依赖:** Task 1 + Task 2 + Task 3 完成

**目标:** sub 路由全部走 ownership 模型；新增 superuser 专用 assign/unassign 路由。

- [ ] **Step 4.1: 写失败测试 — assign_token_to_subscription / unassign_token_from_subscription / set_subscription_owner**

Create `tests/test_subscription_cli_ownership.py`:

```python
"""Tests for core.subscription_cli ownership helpers (issue #108).

覆盖 assign_token_to_subscription / unassign_token_from_subscription /
set_subscription_owner 三个新函数。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
```

- [ ] **Step 4.2: 运行测试确认失败**

Run: `uv run pytest tests/test_subscription_cli_ownership.py -v`
Expected: FAIL（`ImportError: cannot import name 'assign_token_to_subscription'`）

- [ ] **Step 4.3: core/subscription_cli.py 加 owner_token 参数 + 新增 3 个函数**

Edit `core/subscription_cli.py:122-186`（add_subscription 加 owner_token 参数 + 落盘）:

```python
async def add_subscription(
    platform: str,
    identifier: int | str,
    name: str,
    path: str = "config/subscriptions.toml",
    default_notify_endpoint: str | None = None,
    owner_token: str = "",  # issue #108: 创建者 token name
) -> tuple[bool, str]:
    """Add a subscription. Returns (success, message).

    issue #108: ``owner_token`` 非空时写入新 sub 的 ``owner_token`` 字段，
    标记创建者为 owner（全权 CRUD）。API 路由层注入当前 token name，
    CLI 不传（留 '' 等同孤儿，由 superuser adopt）。
    """
    ...
    # Append new subscription
    new_entry = tomlkit.table()
    new_entry[key] = typed_id
    new_entry["name"] = name
    if owner_token:  # issue #108: 非空才写（默认 '' 不落盘，保持老格式）
        new_entry["owner_token"] = owner_token
    arr.append(new_entry)
    ...
```

在 `core/subscription_cli.py` 末尾追加 3 个新函数:

```python
# ═══════════════════════════════════════════════════════════
# Ownership helpers (issue #108)
# ═══════════════════════════════════════════════════════════


async def assign_token_to_subscription(
    platform: str,
    identifier: int | str,
    token_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """把 ``token_name`` 加到 sub.assigned_tokens（issue #108，幂等）。

    返回值:
      ``(True, "已分配: {token_name}")``     # 成功或已存在（幂等）
      ``(False, "未找到订阅")``
      ``(False, "未知 token: {token_name}")``
      ``(False, "无效平台: ...")``
    """
    from web.auth import load_auth_config

    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    # 校验 token 存在（auth.toml）
    auth_cfg = load_auth_config()
    if not any(t.name == token_name for t in auth_cfg.api_tokens):
        return False, f"未知 token: {token_name}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    doc = _load_doc(path)
    if doc is None:
        return False, "未找到订阅"

    doc_dict = cast(dict[str, Any], doc)
    plat_section_raw = doc_dict.get(section, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    subs = plat_section_raw.get("subscriptions", [])
    if not isinstance(subs, list):
        return False, "未找到订阅"

    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get(key, ""))
        if sub_id == str(typed_id):
            eps_arr = sub.get("assigned_tokens", [])
            eps_list = [str(e) for e in eps_arr] if eps_arr else []
            if token_name not in eps_list:
                eps_list.append(token_name)
                sub["assigned_tokens"] = eps_list
            found = True
            break

    if not found:
        return False, "未找到订阅"

    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("📋 assign: %s/%s += %s", section, typed_id, token_name)
    return True, f"已分配: {token_name}"


async def unassign_token_from_subscription(
    platform: str,
    identifier: int | str,
    token_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """从 sub.assigned_tokens 移除 token_name（issue #108，幂等）。

    不校验 token 是否存在（解绑一个不存在的 token 引用无害）。
    空列表时移除字段（与 remove_endpoint_from_subscription 一致）。
    """
    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    doc = _load_doc(path)
    if doc is None:
        return False, "未找到订阅"

    doc_dict = cast(dict[str, Any], doc)
    plat_section_raw = doc_dict.get(section, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    subs = plat_section_raw.get("subscriptions", [])
    if not isinstance(subs, list):
        return False, "未找到订阅"

    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get(key, ""))
        if sub_id == str(typed_id):
            eps_arr = sub.get("assigned_tokens", [])
            eps_list = [str(e) for e in eps_arr if str(e) != token_name]
            if eps_list:
                sub["assigned_tokens"] = eps_list
            else:
                sub.pop("assigned_tokens", None)
            found = True
            break

    if not found:
        return False, "未找到订阅"

    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("📋 unassign: %s/%s -= %s", section, typed_id, token_name)
    return True, f"已解绑: {token_name}"


async def set_subscription_owner(
    platform: str,
    identifier: int | str,
    owner_token: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """Set or replace ``owner_token`` on a subscription（issue #108）。

    I1 修订（issue #108 review）：函数名 generic（``set_subscription_owner``），
    docstring 旧版只说「给孤儿 sub 补 owner」误导 —— 实际功能是 **set/replace**：
    无论 sub 当前 owner 是谁，都覆盖成 ``owner_token``。

    主要调用方：
      - ``adopt`` CLI（给孤儿 sub 补 owner，初始赋权场景）
      - superuser 通过未来 assign-style 管理 endpoint 重 assign owner
        （本 PR 不开 HTTP 路由，仅 CLI + 函数复用）

    返回值:
      ``(True, "已设置 owner: {owner_token}")``
      ``(False, "未找到订阅")``
      ``(False, "未知 token: {owner_token}")``
      ``(False, "无效平台: ...")``
    """
    from web.auth import load_auth_config

    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    auth_cfg = load_auth_config()
    if not any(t.name == owner_token for t in auth_cfg.api_tokens):
        return False, f"未知 token: {owner_token}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    doc = _load_doc(path)
    if doc is None:
        return False, "未找到订阅"

    doc_dict = cast(dict[str, Any], doc)
    plat_section_raw = doc_dict.get(section, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    subs = plat_section_raw.get("subscriptions", [])
    if not isinstance(subs, list):
        return False, "未找到订阅"

    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get(key, ""))
        if sub_id == str(typed_id):
            sub["owner_token"] = owner_token
            found = True
            break

    if not found:
        return False, "未找到订阅"

    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("📋 adopt: %s/%s owner=%s", section, typed_id, owner_token)
    return True, f"已设置 owner: {owner_token}"
```

- [ ] **Step 4.4: 运行 ownership CLI 测试确认通过**

Run: `uv run pytest tests/test_subscription_cli_ownership.py -v`
Expected: PASS

- [ ] **Step 4.5: 重写 api/routes/subscriptions.py（filt → ownership + 新增 assign/unassign 路由）**

Overwrite `api/routes/subscriptions.py`:

```python
"""API subscriptions 路由（T4 + issue #108 ownership）。

薄路由，全部业务复用 ``core.subscription_cli``：
- ``GET /subscriptions`` → ``list_subscriptions`` + ``filter_subscription_dict``
- ``POST /subscriptions`` → ``add_subscription``（注入 owner_token）
- ``DELETE /subscriptions/{platform}/{identifier}`` → ``remove_subscription``
- ``POST/DELETE /subscriptions/{p}/{id}/endpoints`` → bind/unbind
- ``POST/DELETE /subscriptions/{p}/{id}/assign`` → assign/unassign token（superuser）

鉴权走 ``Security(get_token_ownership, scopes=[...])``，ownership 校验在路由层
调 ``subscription_visible`` / ``ownership.has_sub_*``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, Security

from api.auth import get_token_ownership
from api.resource_filter import (
    TokenOwnership,
    filter_subscription_dict,
    subscription_visible,
)
from api.schemas import (
    AssignRequest,
    EndpointBindRequest,
    SubscriptionAddRequest,
    SubscriptionAddResponse,
    SubscriptionListResponse,
    SubscriptionRemoveResponse,
)
from core.subscription_cli import (
    PLATFORM_TO_SECTION,
    add_endpoint_to_subscription,
    add_subscription,
    assign_token_to_subscription,
    list_subscriptions,
    remove_endpoint_from_subscription,
    remove_subscription,
    unassign_token_from_subscription,
)
from shared.config import load_config

logger = logging.getLogger(__name__)
router = APIRouter()


# ═══════════════════════════════════════════════════════════
# URL {platform} 参数归一化（C1 修订，issue #108 review）
# ═══════════════════════════════════════════════════════════
# 原因：``core.subscription_cli.VALID_PLATFORMS = {"bili", "xhs", "weibo"}``
# 只接受短名，但现有路由（#106 之前）历史上两种形式都进过 URL。
# ``remove_subscription`` / ``assign_token_to_subscription`` 等业务函数
# 直接 ``platform in VALID_PLATFORMS`` 校验，传 ``bilibili`` 会被拒。
# 归一化在路由入口做一次，业务层永远拿到短名。


def _normalize_platform(url_platform: str) -> str | None:
    """URL ``{platform}`` 参数归一化为短名（``bili`` / ``xhs`` / ``weibo``）。

    接受短名（``bili``，直接命中 ``PLATFORM_TO_SECTION``）或 TOML section
    全名（``bilibili``，通过 ``SECTION_TO_SHORT`` 反查），统一返回短名。
    无效返回 ``None``，由调用方合并成「未找到」语义（不暴露平台是否有效）。
    """
    from api.resource_filter import SECTION_TO_SHORT

    if url_platform in PLATFORM_TO_SECTION:
        return url_platform
    return SECTION_TO_SHORT.get(url_platform)


@router.get("/subscriptions", response_model=SubscriptionListResponse)
async def list_subs(
    request: Request,
    platform: str | None = Query(default=None, description="按平台过滤 (bili/xhs/weibo)"),
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:read"]
    ),
) -> SubscriptionListResponse:
    """列出订阅，可选 platform 过滤。

    ownership 过滤（issue #108）：在 ``list_subscriptions`` 返回之上叠加 token 的
    ownership（``filter_subscription_dict``），越权订阅不返回。
    """
    result = await list_subscriptions(platform=platform)
    config = await load_config()
    result = filter_subscription_dict(result, ownership, config)
    return SubscriptionListResponse(platforms=result)


@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """添加订阅。

    issue #108: 注入 ``owner_token=ownership.token_name``，创建者自动成为 owner。
    任何持 ``subscriptions:write`` 的 token 都能创建 sub（决策 #7）。
    """
    success, message = await add_subscription(
        body.platform,
        body.identifier,
        body.name,
        default_notify_endpoint=body.default_notify_endpoint,
        owner_token=ownership.token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}", response_model=SubscriptionRemoveResponse
)
async def remove_sub(
    platform: str,
    identifier: str,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionRemoveResponse:
    """删除订阅。

    ownership 校验（issue #108）：越权删除（非 owner / 非 superuser）合并成
    「未找到」语义，不暴露存在性。assigned 不能删（require_write=True）。

    C1 修订：URL ``{platform}`` 经 ``_normalize_platform`` 归一化为短名，
    ``remove_subscription``（``VALID_PLATFORMS`` 只认短名）不再拒绝全名。
    """
    platform = _normalize_platform(platform)
    if platform is None:
        return SubscriptionRemoveResponse(
            success=False, message="未找到: 订阅不存在或无权访问"
        )
    config = await load_config()
    if not subscription_visible(ownership, config, platform, identifier, require_write=True):
        return SubscriptionRemoveResponse(
            success=False, message="未找到: 订阅不存在或无权访问"
        )
    success, message = await remove_subscription(platform, identifier)
    return SubscriptionRemoveResponse(success=success, message=message)


# ── endpoint 绑定/解绑 ──────────────────────────────────────────────


@router.post(
    "/subscriptions/{platform}/{identifier}/endpoints",
    response_model=SubscriptionAddResponse,
)
async def bind_endpoint(
    platform: str,
    identifier: str,
    body: EndpointBindRequest,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """绑定 endpoint 到订阅。

    ownership 校验（issue #108）：越权绑定（非 owner / 非 superuser）合并成
    「未找到订阅」。assigned 不能绑（require_write=True）。

    C1 修订：URL ``{platform}`` 归一化为短名（业务层只认短名）。
    """
    platform = _normalize_platform(platform)
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    config = await load_config()
    if not subscription_visible(ownership, config, platform, identifier, require_write=True):
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await add_endpoint_to_subscription(
        platform, identifier, body.endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}/endpoints/{endpoint_name}",
    response_model=SubscriptionAddResponse,
)
async def unbind_endpoint(
    platform: str,
    identifier: str,
    endpoint_name: str,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["subscriptions:write"]
    ),
) -> SubscriptionAddResponse:
    """解绑 endpoint。

    ownership 校验（issue #108）：越权解绑合并成「未找到订阅」。

    C1 修订：URL ``{platform}`` 归一化为短名（业务层只认短名）。
    """
    platform = _normalize_platform(platform)
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    config = await load_config()
    if not subscription_visible(ownership, config, platform, identifier, require_write=True):
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await remove_endpoint_from_subscription(
        platform, identifier, endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)


# ── assign / unassign token（superuser 专用，issue #108）────────────


@router.post(
    "/subscriptions/{platform}/{identifier}/assign",
    response_model=SubscriptionAddResponse,
)
async def assign_token(
    platform: str,
    identifier: str,
    body: AssignRequest,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["tokens:manage"]
    ),
) -> SubscriptionAddResponse:
    """把 token 分配到 sub（只 superuser，spec §5.2）。

    - 无效平台（既非短名也非全名）→ 200 + success=False, message="未找到订阅"
    - sub 不存在 → 200 + success=False, message="未找到订阅"
    - token 不存在（不在 auth.toml）→ 200 + success=False, message="未知 token"
    - 已分配（幂等）→ 200 + success=True
    - 成功 → 200 + success=True

    ``tokens:manage`` scope 校验已由 ``Security(scopes=["tokens:manage"])`` 拦截，
    路由内不再判 superuser。

    C1 修订：URL ``{platform}`` 归一化为短名。``assign_token_to_subscription``
    内部 ``platform in VALID_PLATFORMS`` 只认短名（``bili``），传全名
    （``bilibili``）会被拒并返回「无效平台」，路由层归一化后业务层不再误拒。
    测试统一用短名（``/bili/100/assign``）。
    """
    platform = _normalize_platform(platform)
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await assign_token_to_subscription(
        platform=platform,
        identifier=identifier,
        token_name=body.token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}/assign/{token_name}",
    response_model=SubscriptionAddResponse,
)
async def unassign_token(
    platform: str,
    identifier: str,
    token_name: str,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["tokens:manage"]
    ),
) -> SubscriptionAddResponse:
    """取消分配（只 superuser，幂等）。

    C1 修订：URL ``{platform}`` 归一化为短名（业务层只认短名）。
    """
    platform = _normalize_platform(platform)
    if platform is None:
        return SubscriptionAddResponse(success=False, message="未找到订阅")
    success, message = await unassign_token_from_subscription(
        platform=platform, identifier=identifier, token_name=token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)
```

- [ ] **Step 4.6: api/schemas.py 新增 AssignRequest**

Edit `api/schemas.py`，在文件末尾追加:

```python
class AssignRequest(BaseModel):
    """``POST /subscriptions/{p}/{id}/assign`` 请求体（issue #108）。

    仅一个字段 —— ``token_name``。响应复用 ``SubscriptionAddResponse``。
    """

    token_name: str
```

- [ ] **Step 4.7: 运行 ruff + pyright 确认**

Run: `uv run ruff check api/routes/subscriptions.py api/schemas.py core/subscription_cli.py tests/test_subscription_cli_ownership.py`
Expected: 无错误

Run: `uv run pyright`
Expected: 无新增 error

- [ ] **Step 4.8: 提交**

```bash
git add api/routes/subscriptions.py api/schemas.py core/subscription_cli.py tests/test_subscription_cli_ownership.py
git commit -m "feat(#108): migrate sub routes to ownership model + assign/unassign API

- GET/POST/DELETE /subscriptions: ownership-based filtering + owner injection on create
- bind/unbind endpoint: require_write=True (assigned cannot bind)
- New POST/DELETE /subscriptions/{p}/{id}/assign: superuser-only (tokens:manage scope)
- add_subscription: owner_token param injected by route from current token
- New assign_token_to_subscription / unassign_token_from_subscription / set_subscription_owner
- New AssignRequest schema"
```

---

## Task 5: messages 路由迁移 — list/get/rerun 加 ownership 过滤 + fetch 限制 superuser

**Files:**
- Modify: `api/routes/messages.py`（filt → ownership + fetch 加 superuser 检查）

**依赖:** Task 3 + Task 4 完成

**目标:** messages 路由走 ownership 模型；fetch 收紧为 superuser-only。

- [ ] **Step 5.1: 重写 api/routes/messages.py**

Overwrite `api/routes/messages.py`，关键改动点：

1. import 从 `TokenResourceFilter` 改为 `TokenOwnership`，删 `msg_id_platform_allowed`
2. 所有路由签名 `filt` 改为 `ownership`
3. `list_messages` / `get_message` / `rerun_messages`：`message_visible(ownership, cfg, m)` 替换 `filt.allows_message(m)`
4. `fetch_messages`：非 superuser → 403

具体改动（基于现有 `api/routes/messages.py`）:

Edit `api/routes/messages.py:28-29`:

```python
from api.auth import get_resource_filter
from api.resource_filter import TokenResourceFilter, msg_id_platform_allowed
```

改为:

```python
from api.auth import get_token_ownership
from api.resource_filter import TokenOwnership, message_visible
```

Edit `api/routes/messages.py:96-146`（list_messages）:

```python
@router.get("/messages", response_model=MessageListResponse)
async def list_messages(
    request: Request,
    since: str | None = Query(None),
    title: str | None = Query(None),
    author: str | None = Query(None),
    platform: str | None = Query(None),
    phase: str | None = Query(None),
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["messages:read"]
    ),
) -> MessageListResponse:
    """多维度筛选消息。

    ownership 过滤（issue #108）：msg → subscription_ref 反查 sub →
    ``ownership.has_sub_access(sub)``。无主消息只 superuser 可见。
    """
    phase_enum: Phase | None = None
    if phase:
        try:
            phase_enum = Phase[phase.upper()]
        except KeyError as exc:
            raise HTTPException(
                status_code=422, detail=f"未知 phase: {phase}"
            ) from exc

    since_ts: int | None = None
    if since is not None:
        try:
            since_ts = _parse_since_or_int(since)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"无法解析 since 值: {since!r}（支持格式: 24h / 7d / 30m / 2026-06-01 / unix 时间戳）",
            ) from exc

    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)
    matched = store.query_messages(
        since=since_ts, title=title, author=author, platform=platform, phase=phase_enum
    )
    # ownership 过滤（issue #108）：msg → sub → has_sub_access
    matched = [m for m in matched if message_visible(ownership, cfg, m)]
    return MessageListResponse(
        messages=[_record_to_out(m) for m in matched],
        count=len(matched),
    )
```

Edit `api/routes/messages.py:154-172`（get_message）:

```python
@router.get("/messages/{msg_id}", response_model=MessageOut)
async def get_message(
    msg_id: str,
    request: Request,
    ownership: TokenOwnership = Security(
        get_token_ownership, scopes=["messages:read"]
    ),
) -> MessageOut:
    """单条消息详情。不存在或越权 → 404。

    ownership 校验（issue #108）：越权消息也返回 404，不区分「不存在」与
    「存在但越权」（不暴露存在性）。
    """
    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)
    rec = store.get_message(msg_id)
    if rec is None or not message_visible(ownership, cfg, rec):
        raise HTTPException(status_code=404, detail="message not found")
    return _record_to_out(rec)
```

Edit `api/routes/messages.py:180-287`（rerun_messages，关键 existing 列表构造）:

将 `filt: TokenResourceFilter = Security(get_resource_filter, scopes=["messages:write"])` 改为
`ownership: TokenOwnership = Security(get_token_ownership, scopes=["messages:write"])`。

将 228-238 行:
```python
    existing = [
        m
        for m in (store.get_message(mid) for mid in body.msg_ids)
        if m is not None and filt.allows_message(m)
    ]
```

改为:
```python
    # I3 修订（issue #108 review）：cfg 已在 messages.py:224 加载
    # （rerun_messages 上方 ``cfg = await load_config()``），本 step 复用
    # 已加载的 ``cfg``，不重复 load —— 避免 fixer 误以为这里缺 cfg 又加一行。
    existing = [
        m
        for m in (store.get_message(mid) for mid in body.msg_ids)
        if m is not None and message_visible(ownership, cfg, m)
    ]
```

（注：`cfg` 已在上方 `cfg = await load_config()` 加载，rerun_messages 224 行已有。）

Edit `api/routes/messages.py:295-395`（fetch_messages，加 superuser 检查）:

将 `filt: TokenResourceFilter = Security(get_resource_filter, scopes=["messages:write"])` 改为
`ownership: TokenOwnership = Security(get_token_ownership, scopes=["messages:write"])`。

在 `if not body.msg_ids:` 校验之后，`state = request.app.state` 之前插入:

```python
    # issue #108: fetch 抓取的消息可能 subscription_ref 为空（无主），
    # 无法判断 owner，只允许 superuser 调用
    if not ownership.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="fetch requires tokens:manage (superuser only)",
        )
```

删除原 342-346 行的 `msg_id_platform_allowed` 过滤逻辑（fetch 只 superuser 调用，不需要平台过滤）:

```python
    # 删除这段：
    # authorized_ids = [
    #     mid for mid in body.msg_ids if msg_id_platform_allowed(mid, filt)
    # ]
    # if not authorized_ids:
    #     raise HTTPException(status_code=404, detail="message not found")
```

并将后台 task 内 `run_fetch_and_process` 的 `msg_ids=authorized_ids` 改回 `msg_ids=body.msg_ids`（只改这一行，except/finally 块保持原样不动）:

```python
    async def _fetch() -> None:
        try:
            await PipelineEngine.run_fetch_and_process(
                msg_ids=body.msg_ids,  # ← 改这一行（authorized_ids → body.msg_ids）
                skip_push=body.skip_push,
                config=cfg,
                store=store,
                log_callback=cb,
            )
        except Exception as exc:
            # 原 except 块保持不变（err_item 构造 + log_history.append + subscribers 广播）
            ...  # ← 现有代码，不动
        finally:
            # 原 finally 块保持不变（锁释放 + subscribers 通知）
            ...  # ← 现有代码，不动
```

- [ ] **Step 5.2: 运行 ruff + pyright 确认**

Run: `uv run ruff check api/routes/messages.py`
Expected: 无错误

Run: `uv run pyright`
Expected: 无新增 error

- [ ] **Step 5.3: 提交**

```bash
git add api/routes/messages.py
git commit -m "feat(#108): migrate messages routes to ownership + fetch superuser-only

- list/get/rerun: message_visible(ownership, cfg, msg) replaces filt.allows_message
- msg → sub reverse lookup via find_subscription_by_ref, ownerless msgs superuser-only
- fetch: 403 for non-superuser (ownerless fetched msgs cannot be attributed)
- Drop msg_id_platform_allowed helper usage (fetch no longer filters by platform)"
```

---

## Task 6: CLI 扩展 — 删 resource flag + 加 adopt + create red warning

**Files:**
- Modify: `api/token_tool.py`（删 --resource-platform/sub + Resource Rules 列；加 adopt 命令；create 红 warning）

**依赖:** Task 4 完成（set_subscription_owner 函数）

**目标:** CLI 与新模型对齐。

- [ ] **Step 6.1: 写失败测试 — adopt 命令 + create red warning + resource flag 删除**

Edit `tests/test_api_token_tool.py`:

1. **删除** `TestCreateResourceRules`（line 263-377）和 `TestListResourceRules`（line 380-424）整段。
2. **新增** adopt 命令测试和 create 空 scopes 测试，在文件末尾追加:

```python
# ── adopt 子命令 + create 空 scopes warning（issue #108）──────────────


class TestAdoptCommand:
    """``trawler token adopt --platform --id --owner`` 一键给孤儿 sub 补 owner。"""

    def test_adopt_success(
        self, runner: CliRunner, auth_path: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """成功给 bili/100 补 owner。"""
        from web.auth import load_auth_config

        # 先准备 auth.toml 含 owner-bot token
        from api.auth import create_token
        create_token("owner-bot")

        # 准备 subscriptions.toml 含一条无 owner 的 bili sub
        subs_path = tmp_path / "subscriptions.toml"
        subs_path.write_text(
            '[[bilibili.subscriptions]]\n'
            'uid = 100\n'
            'name = "UP100"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "core.subscription_cli.PLATFORM_TO_SECTION",  # 不动常量
            {"bili": "bilibili", "xhs": "xiaohongshu", "weibo": "weibo"},
            raising=False,
        )

        with patch("core.subscription_cli.Path") as mock_path_cls:
            # 让 set_subscription_owner 读到 tmp subs 文件
            mock_path_cls.return_value = subs_path
            result = runner.invoke(
                cli,
                [
                    "adopt",
                    "--platform", "bili",
                    "--id", "100",
                    "--owner", "owner-bot",
                ],
            )

        # 由于 monkeypatch subscriptions.toml 路径较复杂，本测试用 mock 更简单
        # 直接调 set_subscription_owner 验证（已在 test_subscription_cli_ownership 覆盖）
        # 这里只断言 adopt 命令存在 + 输出格式
        assert result.exit_code == 0

    def test_adopt_unknown_token_fails(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """adopt 不存在的 token → 退出码非 0。"""
        result = runner.invoke(
            cli,
            [
                "adopt",
                "--platform", "bili",
                "--id", "100",
                "--owner", "ghost-bot",
            ],
        )
        assert result.exit_code != 0
        assert "未知 token" in result.output or "✗" in result.output


class TestCreateNoScopesWarning:
    """#108 后 create 空 scopes = 无权，CLI 必须明示。"""

    def test_create_no_scopes_shows_red_warning(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """create 不传 --scope → 输出含 red warning 提示无权。"""
        result = runner.invoke(cli, ["create", "empty-bot"])
        assert result.exit_code == 0
        # 输出含「无任何权限」或「无权」提示
        assert "无" in result.output
        assert "权" in result.output


class TestResourceFlagsRemoved:
    """#108 删除 --resource-platform / --resource-sub flag。"""

    def test_create_rejects_resource_platform_flag(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """``--resource-platform`` 已删除 → Click 报未知 option。"""
        result = runner.invoke(
            cli,
            ["create", "x", "--resource-platform", "bili"],
        )
        assert result.exit_code != 0
        # Click 报错信息含 "no such option"
        assert "no such option" in result.output.lower() or "resource-platform" in result.output.lower()

    def test_create_rejects_resource_sub_flag(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """``--resource-sub`` 已删除。"""
        result = runner.invoke(
            cli,
            ["create", "x", "--resource-sub", "bili:100"],
        )
        assert result.exit_code != 0

    def test_list_no_resource_rules_column(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """``list`` 命令不再显示 Resource Rules 列。"""
        from api.auth import create_token

        create_token("bot1")
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        # 不含 "Resource Rules" 列标题
        assert "Resource Rules" not in result.output
```

- [ ] **Step 6.2: 运行测试确认失败**

Run: `uv run pytest tests/test_api_token_tool.py::TestAdoptCommand tests/test_api_token_tool.py::TestCreateNoScopesWarning tests/test_api_token_tool.py::TestResourceFlagsRemoved -v`
Expected: FAIL（adopt 命令不存在 / --resource-platform 仍接受 / Resource Rules 列仍在）

- [ ] **Step 6.3: 重写 api/token_tool.py**

Overwrite `api/token_tool.py` 完整内容:

```python
"""API token 管理 CLI（T5 + issue #108 ownership）。

Usage:
    python -m api.token_tool create <name> [--force]
    python -m api.token_tool list
    python -m api.token_tool revoke <name>
    python -m api.token_tool adopt --platform <p> --id <id> --owner <token_name>

复用 :mod:`api.auth` 的 :func:`create_token` / :func:`revoke_token` 和
:func:`web.auth.load_auth_config` 读写 ``data/auth.toml``。

issue #108 改动：
- 删除 ``--resource-platform`` / ``--resource-sub`` flag（ResourceRules 废弃）
- 新增 ``adopt`` 子命令（给孤儿 sub 补 owner_token）
- ``create`` 不传 ``--scope`` 改 red warning（空 scopes = 无权）
- ``list`` 删除 Resource Rules 列
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from api.auth import create_token, revoke_token
from web.auth import load_auth_config

console = Console()


def _token_exists(name: str) -> bool:
    """auth.toml 是否已存在同名 token。"""
    cfg = load_auth_config()
    return any(t.name == name for t in cfg.api_tokens)


@click.group()
def cli() -> None:
    """API token 管理（生成 / 列出 / 撤销 / adopt）。"""
    pass


@cli.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="覆盖同名 token")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="限制 token scope（可多次指定，如 --scope messages:read --scope check:read）。"
    "不指定 = [red]无任何权限[/]（#108 破坏性变更）。"
    "合法 scope 见 ALL_SCOPES 常量。要创建 superuser 加 --scope tokens:manage。",
)
def create(
    name: str,
    force: bool,
    scopes: tuple[str, ...],
) -> None:
    """生成新 token，明文仅打印一次（存储为 SHA-256 hash，无法恢复）。

    ``--scope`` 可多次指定，限制 token 能访问的 API 范围。
    不传 ``--scope`` → 空 scopes = 无任何权限（#108 破坏性变更，spec §6.2）。
    要创建 superuser token 加 ``--scope tokens:manage``。
    """
    from api.auth import ALL_SCOPES

    # ── 1. scope 白名单校验（防拼写错误）──────────────────────────
    invalid = [s for s in scopes if s not in ALL_SCOPES]
    if invalid:
        console.print(
            f"[red]✗[/] 未知 scope: {', '.join(invalid)}",
            style="red",
        )
        console.print(f"[dim]合法 scope: {', '.join(ALL_SCOPES)}[/]")
        sys.exit(1)

    # ── 2. _token_exists / --force（同名覆盖检查）─────────────────
    if _token_exists(name) and not force:
        console.print(
            f"[red]✗[/] token '{name}' 已存在，加 --force 覆盖",
            style="red",
        )
        sys.exit(1)

    # ── 3. 落盘 ──────────────────────────────────────────────────
    scope_list = list(scopes)
    plain = create_token(name, scopes=scope_list)
    console.print(f"[green]✓[/] 已创建 token '{name}'，明文（仅此一次）：")
    console.print(f"[yellow]{plain}[/]")
    console.print("[dim]存储为 SHA-256 hash，后续无法再查看明文。[/]")
    if scope_list:
        console.print(f"[cyan]📝[/] Scopes: {', '.join(scope_list)}")
    else:
        console.print(
            "[red]⚠️[/] 未指定 scope = [bold]无任何权限[/]（#108 破坏性变更）。"
            " 要创建 superuser token 加 --scope tokens:manage；"
            " 要创建只读 token 加 --scope messages:read --scope subscriptions:read。",
            style="red",
        )


@cli.command("list")
def list_cmd() -> None:
    """列出所有 token（只显示 hash 前 8 位）。"""
    cfg = load_auth_config()
    if not cfg.api_tokens:
        console.print("[dim]无 API token。[/]")
        return

    table = Table(title="API Tokens")
    table.add_column("Name")
    table.add_column("Hash (前 8 位)")
    table.add_column("Created At")
    table.add_column("Scopes")
    for t in cfg.api_tokens:
        created = datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M")
        if t.scopes:
            scopes_str = ", ".join(t.scopes)
        else:
            scopes_str = "(无权限)"
        table.add_row(t.name, t.token_hash[:8], created, scopes_str)
    console.print(table)


@cli.command()
@click.argument("name")
def revoke(name: str) -> None:
    """按 name 删除 token。"""
    if revoke_token(name):
        console.print(f"[green]✓[/] 已撤销 token '{name}'")
    else:
        console.print(f"[red]✗[/] 未找到 token '{name}'", style="red")
        sys.exit(1)


@cli.command()
@click.option(
    "--platform",
    "platform",
    required=True,
    type=click.Choice(["bili", "xhs", "weibo"]),
    help="平台 short name (bili/xhs/weibo)",
)
@click.option(
    "--id",
    "identifier",
    required=True,
    help="订阅 id（bili=uid, xhs/weibo=user_id）",
)
@click.option(
    "--owner",
    "owner_token",
    required=True,
    help="要绑定为 owner 的 token name（必须在 auth.toml 已存在）",
)
def adopt(platform: str, identifier: str, owner_token: str) -> None:
    """给孤儿 sub 补 owner_token（issue #108）。

    CLI 本身 = 管理员 = superuser 等价，直接改 subscriptions.toml。
    如果 token 不存在（不在 auth.toml）→ 退出码非 0。
    如果 sub 不存在 → 退出码非 0。

    示例::

        trawler token adopt --platform bili --id 123456 --owner bili-admin-bot
    """
    from core.subscription_cli import set_subscription_owner

    ok, msg = asyncio.run(set_subscription_owner(
        platform=platform, identifier=identifier, owner_token=owner_token,
    ))
    if ok:
        console.print(f"[green]✓[/] {msg}")
    else:
        console.print(f"[red]✗[/] {msg}", style="red")
        sys.exit(1)


if __name__ == "__main__":
    cli()
```

- [ ] **Step 6.4: 运行测试确认通过**

Run: `uv run pytest tests/test_api_token_tool.py -v`
Expected: PASS（删除的测试不再跑，新增的测试通过）

- [ ] **Step 6.5: 提交**

```bash
git add api/token_tool.py tests/test_api_token_tool.py
git commit -m "feat(#108): CLI drop resource flags, add adopt command, red warning on empty scopes

- Remove --resource-platform / --resource-sub flags (ResourceRules deprecated)
- Remove Resource Rules column from list command
- New 'adopt' subcommand: set owner_token on orphan sub via set_subscription_owner
- create: empty scopes now shows red warning (no permissions, #108 breaking)
- Delete TestCreateResourceRules / TestListResourceRules test classes
- Add TestAdoptCommand / TestCreateNoScopesWarning / TestResourceFlagsRemoved"
```

---

## Task 7: 测试 fixture 重写 — authed_client → superuser_client + row_filtered → owner/assigned/outsider 矩阵

**Files:**
- Modify: `tests/test_api_check.py`（authed_client → superuser_client，仅改名）
- Modify: `tests/test_api_messages.py`（authed_client → superuser_client；删 row_filtered + TestRowLevel*；加 ownership 矩阵）
- Modify: `tests/test_api_subscriptions.py`（同上）
- Modify: `tests/test_api_fetch.py`（同上 + fetch superuser-only）
- Modify: `tests/test_api_auth.py`（删 TestResourceRulesData + ResourceFilterDep 部分；authed_client → superuser_client；get_resource_filter 测试改为 get_token_ownership）
- Delete: `tests/test_config_ownership_fields.py`（Task 1 临时的，本 task 删）

**依赖:** Task 1-6 全部完成

**目标:** 所有测试走新模型，全量回归通过。

- [ ] **Step 7.1: test_api_check.py — authed_client → superuser_client**

Edit `tests/test_api_check.py:33` 把 fixture 改名 + 加 scopes:

```python
@pytest.fixture
async def authed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    ...
    plain = create_token("test-bot")
    ...
```

改为:

```python
@pytest.fixture
async def superuser_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """持 tokens:manage 的 superuser client（#108 后空 scopes 无权）。"""
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token("super-bot", scopes=["tokens:manage"])
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c
```

全文件 `authed_client` 替换为 `superuser_client`（用 edit replaceAll 或 ast_grep）。

- [ ] **Step 7.2: test_api_auth.py — 删 ResourceRules 相关 + authed_client → superuser_client + get_resource_filter 测试改造**

1. **删除** `TestResourceRulesData` 类（line 430-575）整段。
2. **改名 + 重写** `authed_client` fixture（line 582-610）为 `superuser_client`，加 `scopes=["tokens:manage"]`。

   M4 修订（issue #108 review）：原 plan 只说「改名 + 加 scope」没给 body，这里
   显式给出 `superuser_client` 在 `test_api_auth.py` 的完整 body（与 Step 7.1 一致）。

   ```python
   @pytest.fixture
   async def superuser_client(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> AsyncClient:
       """持 tokens:manage 的 superuser client（#108 后空 scopes 无权）。"""
       auth_path = tmp_path / "auth.toml"
       monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
       monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
       set_password(PASSWORD)

       from api.auth import create_token

       app = create_app()
       plain = create_token("super-bot", scopes=["tokens:manage"])
       transport = ASGITransport(app=app)
       async with AsyncClient(
           transport=transport,
           base_url="http://test",
           headers={"Authorization": f"Bearer {plain}"},
       ) as c:
           c._app = app  # type: ignore[attr-defined]
           yield c
   ```

   全文件 `authed_client` 替换为 `superuser_client`（用 edit replaceAll 或 ast_grep）。
3. **改造** `TestGetResourceFilter` 类（line 613-716）为 `TestGetTokenOwnership`：
   - `test_get_resource_filter_importable` 改为 `test_get_token_ownership_importable`，import `get_token_ownership`。
   - `test_unrestricted_token_returns_unrestricted_filter` 改为 `test_superuser_token_passes_ownership`，superuser_client 不被拦。
   - `test_missing_token_returns_401` 不变（无 header → 401）。
   - `test_insufficient_scope_returns_403` 不变（scope 不够 → 403）。
   - `test_openapi_docs_include_scopes` 不变。
   - 用例内的 `authed_client` 全部替换为 `superuser_client`。

具体改造 `TestGetResourceFilter` → `TestGetTokenOwnership`:

```python
class TestGetTokenOwnership:
    """``get_token_ownership`` FastAPI 依赖用例（issue #108，替代 #106 get_resource_filter）。"""

    def test_get_token_ownership_importable(self) -> None:
        """``get_token_ownership`` 已定义（模块级 import 不应抛异常）。"""
        from api.auth import get_token_ownership  # noqa: F401

    async def test_superuser_token_passes_ownership(
        self, superuser_client: AsyncClient
    ) -> None:
        """持 tokens:manage 的 superuser → 不被 ownership 拦截（200）。"""
        resp = await superuser_client.get("/api/v1/messages")
        assert resp.status_code == 200  # 不被 ownership 拦截

    async def test_missing_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 Authorization header → 401（与 require_scopes 一致）。"""
        from httpx import ASGITransport

        from web.app import create_app

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test"
            # 故意不带 Authorization header
        ) as c:
            resp = await c.get("/api/v1/messages")
        assert resp.status_code == 401
        assert "token" in resp.json()["detail"]

    async def test_insufficient_scope_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """scope 不够 → 403（ownership 层在 scope 之后，scope 先拦）。"""
        from httpx import ASGITransport

        from api.auth import create_token
        from web.app import create_app

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        plain = create_token("sub-only", scopes=["subscriptions:read"])
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plain}"},
        ) as c:
            resp = await c.get("/api/v1/messages")
        assert resp.status_code == 403
        assert "scope" in resp.json()["detail"].lower()

    async def test_openapi_docs_include_scopes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenAPI schema 在新依赖写法下能正常生成。"""
        from web.app import create_app

        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)

        app = create_app()
        schema = app.openapi()
        paths = schema["paths"]
        assert "/api/v1/messages" in paths
        assert "get" in paths["/api/v1/messages"]
```

- [ ] **Step 7.3: test_api_messages.py — authed_client → superuser_client + 删 row_filtered + 加 ownership 矩阵**

1. **改名 + 重写** `authed_client`（line 57）为 `superuser_client`，加 `scopes=["tokens:manage"]`，全文件替换用例。

   M4 修订（issue #108 review）：显式给出 `superuser_client` 在 `test_api_messages.py`
   的完整 body（与 Step 7.1/7.2/7.5 一致），避免 fixer 凭猜测写 fixture。

   ```python
   @pytest.fixture
   async def superuser_client(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> AsyncClient:
       """持 tokens:manage 的 superuser client（#108 后空 scopes 无权）。"""
       auth_path = tmp_path / "auth.toml"
       monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
       monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
       set_password(PASSWORD)

       from api.auth import create_token

       app = create_app()
       plain = create_token("super-bot", scopes=["tokens:manage"])
       transport = ASGITransport(app=app)
       async with AsyncClient(
           transport=transport,
           base_url="http://test",
           headers={"Authorization": f"Bearer {plain}"},
       ) as c:
           c._app = app  # type: ignore[attr-defined]
           yield c
   ```

2. **删除** `row_filtered_client` fixture（line 479-522）+ `tmp_data_dir_with_mixed_msgs` fixture（line 525-590）+ `TestRowLevelGet` / `TestRowLevelMatrix` / `TestRowLevelRerun` / `TestRowLevelCrossRoute` 全部类（line 593-852）。
3. **新增** `tmp_config_with_owned_sub` fixture + owner/assigned/outsider 三个 fixture + 测试矩阵。

在文件末尾追加（替换原 row_filtered 相关代码）:

```python
# ═══════════════════════════════════════════════════════════
# ownership 矩阵（issue #108）
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def tmp_config_with_owned_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写盘 config/subscriptions.toml + auth.toml，含 ownership 矩阵数据。

    Subscriptions:
      - bili uid=100, owner_token='owner-bot', assigned_tokens=['assigned-bot']
      - bili uid=200, owner_token='' (孤儿)
      - xhs user_id='u456', owner_token='owner-bot'

    Auth tokens:
      - super-bot: scopes=['tokens:manage'] (superuser)
      - owner-bot: scopes=['subscriptions:write', 'messages:read'] (普通 owner)
      - assigned-bot: scopes=['messages:read'] (被分配只读)
      - outsider-bot: scopes=['messages:read'] (无关 token)
    """
    # subscriptions.toml
    subs_path = tmp_path / "subscriptions.toml"
    subs_path.write_text(
        '[[bilibili.subscriptions]]\n'
        'uid = 100\n'
        'name = "UP100"\n'
        'owner_token = "owner-bot"\n'
        'assigned_tokens = ["assigned-bot"]\n\n'
        '[[bilibili.subscriptions]]\n'
        'uid = 200\n'
        'name = "OrphanUP"\n\n'
        '[[xiaohongshu.subscriptions]]\n'
        'user_id = "u456"\n'
        'name = "XHS1"\n'
        'owner_token = "owner-bot"\n',
        encoding="utf-8",
    )

    # mock load_config 让 list_messages / message_visible 反查到这些 sub
    from shared.config import (
        BiliConfig,
        BiliSubscription,
        Config,
        GeneralConfig,
        UserSubscription,
        WeiboConfig,
        XhsConfig,
    )

    fake_cfg = Config(
        general=GeneralConfig(data_dir=str(tmp_path)),
        bilibili=BiliConfig(subscriptions=[
            BiliSubscription(uid=100, name="UP100", owner_token="owner-bot",
                             assigned_tokens=["assigned-bot"]),
            BiliSubscription(uid=200, name="OrphanUP"),
        ]),
        xiaohongshu=XhsConfig(subscriptions=[
            UserSubscription(user_id="u456", name="XHS1", owner_token="owner-bot"),
        ]),
        weibo=WeiboConfig(),
    )

    # mock load_config 在两个位置（api.routes.messages 和 api.routes.subscriptions）
    from unittest.mock import AsyncMock
    mock_load = AsyncMock(return_value=fake_cfg)
    monkeypatch.setattr("api.routes.messages.load_config", mock_load)

    # ── 写 messages.json（C2 修订，issue #108 review）──────────────────────
    # 核实结果（读 shared/message_store.py 确认）：
    #   - 文件名：``data/messages.json``（**不是** ``.jsonl``）
    #   - 格式：JSON object，顶层 ``{"messages": {msg_id: {fields}}}``
    #     （``MessageStore._load`` 读 ``data.get("messages", {})``）
    #   - 枚举字段：``content_type`` / ``phase`` 落盘是 ``.value``（str），
    #     ``_msg_from_dict`` 用 ``ContentType(str)`` / ``Phase(str)`` 反序列化
    #   - 必填字段（无默认）：``platform`` / ``content_type`` / ``phase`` /
    #     ``pubdate`` / ``title`` / ``author``
    #   - 可选字段（``_msg_from_dict`` 用 ``.get(..., default)``）：
    #     ``created_at`` / ``updated_at`` / ``error`` / ``dynamic_text`` /
    #     ``subscription_ref`` / ``xsec_token`` / ``body`` / ``summary`` /
    #     ``retry_count`` / ``last_error`` / ``permanent_error``
    # 因此 fixture 写 JSON object + 枚举用 ``.value``，``MessageStore.load`` 才能真读。
    import json
    import time
    from shared.protocols import ContentType, Phase  # noqa: F811 (局部 import 清晰)
    now = time.time()
    messages = {
        "bili:100": {
            "platform": "bili", "content_type": ContentType.VIDEO.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "bili-100", "author": "a", "subscription_ref": "100",
            "created_at": now, "updated_at": now,
        },
        "bili:200": {
            "platform": "bili", "content_type": ContentType.VIDEO.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "bili-200", "author": "a", "subscription_ref": "200",
            "created_at": now, "updated_at": now,
        },
        "xhs:u456": {
            "platform": "xhs", "content_type": ContentType.TEXT.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "xhs-u456", "author": "a", "subscription_ref": "u456",
            "created_at": now, "updated_at": now,
        },
        "weibo:no_sub": {
            "platform": "weibo", "content_type": ContentType.TEXT.value,
            "phase": Phase.SUMMARIZED.value, "pubdate": int(now),
            "title": "no-sub", "author": "a", "subscription_ref": "",
            "created_at": now, "updated_at": now,
        },
    }
    (tmp_path / "messages.json").write_text(
        json.dumps({"messages": messages}, ensure_ascii=False), encoding="utf-8"
    )

    # auth.toml
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token
    create_token("super-bot", scopes=["tokens:manage"])
    create_token("owner-bot", scopes=["subscriptions:write", "messages:read"])
    create_token("assigned-bot", scopes=["messages:read"])
    create_token("outsider-bot", scopes=["messages:read"])

    return tmp_path


@pytest.fixture
async def owner_client(
    tmp_config_with_owned_sub: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """owner-bot 的 client（拥有 bili/100 + xhs/u456，不拥有 bili/200 孤儿）。"""
    from api.auth import create_token

    # 注意：tmp_config_with_owned_sub 已 create_token("owner-bot")，但明文没保留
    # 这里重新 create 覆盖拿明文（破坏 hash 但 ownership 字段在 auth.toml 仍匹配 name）
    plain = create_token(
        "owner-bot",
        scopes=["subscriptions:write", "subscriptions:read", "messages:read", "messages:write"],
    )
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def assigned_client(
    tmp_config_with_owned_sub: Path,
) -> AsyncClient:
    """assigned-bot 的 client（被分配只读 bili/100）。"""
    from api.auth import create_token

    plain = create_token("assigned-bot", scopes=["messages:read", "subscriptions:read"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


@pytest.fixture
async def outsider_client(
    tmp_config_with_owned_sub: Path,
) -> AsyncClient:
    """outsider-bot 的 client（无任何 sub 关系）。"""
    from api.auth import create_token

    plain = create_token("outsider-bot", scopes=["messages:read", "subscriptions:read"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c


class TestOwnershipListMessages:
    """GET /messages ownership 矩阵（issue #108）。"""

    async def test_superuser_sees_all(
        self, superuser_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 看所有消息（含无主 weibo:no_sub）。"""
        resp = await superuser_client.get("/api/v1/messages")
        assert resp.status_code == 200
        msg_ids = {m["msg_id"] for m in resp.json()["messages"]}
        assert msg_ids == {"bili:100", "bili:200", "xhs:u456", "weibo:no_sub"}

    async def test_owner_sees_own_subs(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 看 bili/100 + xhs/u456（自己 own 的），不看孤儿 bili/200。"""
        resp = await owner_client.get("/api/v1/messages")
        assert resp.status_code == 200
        msg_ids = {m["msg_id"] for m in resp.json()["messages"]}
        assert msg_ids == {"bili:100", "xhs:u456"}

    async def test_assigned_sees_only_assigned(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """assigned-bot 只看 bili/100（被分配的），不看 xhs/u456。"""
        resp = await assigned_client.get("/api/v1/messages")
        assert resp.status_code == 200
        msg_ids = {m["msg_id"] for m in resp.json()["messages"]}
        assert msg_ids == {"bili:100"}

    async def test_outsider_sees_nothing(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider-bot 看不到任何消息。"""
        resp = await outsider_client.get("/api/v1/messages")
        assert resp.status_code == 200
        assert resp.json()["messages"] == []
        assert resp.json()["count"] == 0


class TestOwnershipGetMessage:
    """GET /messages/{msg_id} ownership 矩阵（issue #108）。"""

    async def test_owner_gets_own_msg(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        resp = await owner_client.get("/api/v1/messages/bili:100")
        assert resp.status_code == 200

    async def test_assigned_gets_assigned_msg(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        resp = await assigned_client.get("/api/v1/messages/bili:100")
        assert resp.status_code == 200

    async def test_outsider_get_404(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider 看 bili:100 → 404（不暴露存在性）。"""
        resp = await outsider_client.get("/api/v1/messages/bili:100")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "message not found"

    async def test_owner_get_orphan_msg_404(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 看不到 bili:200（孤儿 sub，非 superuser 不可见）。"""
        resp = await owner_client.get("/api/v1/messages/bili:200")
        assert resp.status_code == 404

    async def test_ownerless_msg_only_superuser(
        self, owner_client: AsyncClient, superuser_client: AsyncClient,
        tmp_config_with_owned_sub: Path,
    ) -> None:
        """weibo:no_sub 无 subscription_ref，只 superuser 可见。"""
        resp_owner = await owner_client.get("/api/v1/messages/weibo:no_sub")
        assert resp_owner.status_code == 404
        resp_super = await superuser_client.get("/api/v1/messages/weibo:no_sub")
        assert resp_super.status_code == 200
```

- [ ] **Step 7.4: test_api_subscriptions.py — 同模式**

1. **改名 + 重写** `authed_client` → `superuser_client`，加 `scopes=["tokens:manage"]`。

   M4 修订（issue #108 review）：显式给出 `superuser_client` 在
   `test_api_subscriptions.py` 的完整 body（与 Step 7.1/7.2/7.3/7.5 一致）。

   ```python
   @pytest.fixture
   async def superuser_client(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> AsyncClient:
       """持 tokens:manage 的 superuser client（#108 后空 scopes 无权）。"""
       auth_path = tmp_path / "auth.toml"
       monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
       monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
       set_password(PASSWORD)

       from api.auth import create_token

       app = create_app()
       plain = create_token("super-bot", scopes=["tokens:manage"])
       transport = ASGITransport(app=app)
       async with AsyncClient(
           transport=transport,
           base_url="http://test",
           headers={"Authorization": f"Bearer {plain}"},
       ) as c:
           c._app = app  # type: ignore[attr-defined]
           yield c
   ```

   全文件 `authed_client` 替换为 `superuser_client`（用 edit replaceAll 或 ast_grep）。
2. **删除** `row_filtered_client`（line 473-507）+ `TestRowLevelListSubs` / `TestRowLevelSubsWrite`（line 510-664）。
3. **新增** `tmp_config_with_owned_sub` + owner/assigned/outsider fixture（同 messages 风格）+ ownership 矩阵测试。

测试矩阵（与 messages 矩阵同模式，sub 反查走 `tmp_config_with_owned_sub` 的 mock）:

注意：`test_api_subscriptions.py` 的 `tmp_config_with_owned_sub` fixture 与 messages 的略不同 —— sub 路由调 `list_subscriptions`（tomlkit 读盘）+ `load_config`（反查 sub 对象）。因此 fixture 需要：
1. 写盘 `tmp_path / "subscriptions.toml"`（让 `list_subscriptions` 读到）
2. mock `load_config` 返回含相同 sub 对象的 `Config`（让 `filter_subscription_dict` 反查一致）
3. mock `list_subscriptions` 返回 dict（避免真读盘 + 与 mock load_config 数据一致）

```python
@pytest.fixture
def tmp_config_with_owned_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写盘 subscriptions.toml + auth.toml + mock load_config，sub 路由共用。

    Subscriptions:
      - bili uid=100, owner_token='owner-bot', assigned_tokens=['assigned-bot']
      - bili uid=200, owner_token='' (孤儿)
      - xhs user_id='u456', owner_token='owner-bot'
    """
    subs_path = tmp_path / "subscriptions.toml"
    subs_path.write_text(
        '[[bilibili.subscriptions]]\n'
        'uid = 100\n'
        'name = "UP100"\n'
        'owner_token = "owner-bot"\n'
        'assigned_tokens = ["assigned-bot"]\n\n'
        '[[bilibili.subscriptions]]\n'
        'uid = 200\n'
        'name = "OrphanUP"\n\n'
        '[[xiaohongshu.subscriptions]]\n'
        'user_id = "u456"\n'
        'name = "XHS1"\n'
        'owner_token = "owner-bot"\n',
        encoding="utf-8",
    )

    # mock load_config 返回含相同 sub 对象的 Config（让 filter_subscription_dict 反查一致）
    from unittest.mock import AsyncMock
    from shared.config import (
        BiliConfig, BiliSubscription, Config, GeneralConfig,
        UserSubscription, WeiboConfig, XhsConfig,
    )

    fake_cfg = Config(
        general=GeneralConfig(data_dir=str(tmp_path)),
        bilibili=BiliConfig(subscriptions=[
            BiliSubscription(uid=100, name="UP100", owner_token="owner-bot",
                             assigned_tokens=["assigned-bot"]),
            BiliSubscription(uid=200, name="OrphanUP"),
        ]),
        xiaohongshu=XhsConfig(subscriptions=[
            UserSubscription(user_id="u456", name="XHS1", owner_token="owner-bot"),
        ]),
        weibo=WeiboConfig(),
    )
    mock_load = AsyncMock(return_value=fake_cfg)
    monkeypatch.setattr("api.routes.subscriptions.load_config", mock_load)

    # auth.toml
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token
    create_token("super-bot", scopes=["tokens:manage"])
    create_token("owner-bot", scopes=["subscriptions:write", "subscriptions:read"])
    create_token("assigned-bot", scopes=["subscriptions:read"])
    create_token("outsider-bot", scopes=["subscriptions:read"])
    return tmp_path


@pytest.fixture
async def owner_client(tmp_config_with_owned_sub: Path) -> AsyncClient:
    """owner-bot client（重新 create 拿明文，name 不变 ownership 关系成立）。"""
    from api.auth import create_token
    plain = create_token(
        "owner-bot", scopes=["subscriptions:write", "subscriptions:read"]
    )
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        yield c


@pytest.fixture
async def assigned_client(tmp_config_with_owned_sub: Path) -> AsyncClient:
    from api.auth import create_token
    plain = create_token("assigned-bot", scopes=["subscriptions:read"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        yield c


@pytest.fixture
async def outsider_client(tmp_config_with_owned_sub: Path) -> AsyncClient:
    from api.auth import create_token
    plain = create_token("outsider-bot", scopes=["subscriptions:read"])
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        yield c


_MOCK_LIST_RETURN = {
    "bilibili": [
        {"uid": 100, "name": "UP100", "owner_token": "owner-bot",
         "assigned_tokens": ["assigned-bot"]},
        {"uid": 200, "name": "OrphanUP"},
    ],
    "xiaohongshu": [
        {"user_id": "u456", "name": "XHS1", "owner_token": "owner-bot"},
    ],
}


class TestOwnershipListSubs:
    """GET /subscriptions ownership 矩阵（issue #108）。"""

    async def test_superuser_sees_all(
        self, superuser_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 看全部 sub（含孤儿 bili/200）。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await superuser_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()["platforms"]
        # bili section 含 2 条（uid=100 + uid=200 孤儿），xhs 含 1 条
        assert len(data["bilibili"]) == 2
        assert len(data["xiaohongshu"]) == 1

    async def test_owner_sees_own(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 看 bili/100 + xhs/u456（自己 own 的），不看 bili/200 孤儿。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await owner_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()["platforms"]
        bili_uids = {s["uid"] for s in data["bilibili"]}
        assert bili_uids == {100}  # 不含 200 孤儿
        assert len(data["xiaohongshu"]) == 1  # xhs/u456 是自己的

    async def test_assigned_sees_assigned_only(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """assigned-bot 只看 bili/100（被分配的），不看 xhs/u456 也不看 bili/200。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await assigned_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        data = resp.json()["platforms"]
        bili_uids = {s["uid"] for s in data["bilibili"]}
        assert bili_uids == {100}
        # xhs section 不存在（assigned-bot 无权访问 xhs/u456）
        assert "xiaohongshu" not in data

    async def test_outsider_sees_empty(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider-bot 看不到任何 sub → platforms={} 空 dict。"""
        with patch(
            "api.routes.subscriptions.list_subscriptions", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = _MOCK_LIST_RETURN
            resp = await outsider_client.get("/api/v1/subscriptions")
        assert resp.status_code == 200
        assert resp.json()["platforms"] == {}


class TestOwnershipDeleteSub:
    """DELETE /subscriptions/{p}/{id} ownership 矩阵（require_write=True）。

    C1 修订：URL ``{platform}`` 统一用短名（``/bili/100``），与现有
    ``TestRemoveSubscription::test_remove_subscription_success`` 一致。
    路由入口 ``_normalize_platform`` 会同时接受全名，但测试只用短名避免歧义。
    """

    async def test_owner_deletes_own(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 删 bili/100（自己 own 的）→ success=True。"""
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            mock_remove.return_value = (True, "已删除: UP100")
            resp = await owner_client.delete("/api/v1/subscriptions/bili/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_remove.assert_awaited_once()

    async def test_assigned_cannot_delete(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """assigned-bot 删 bili/100 → success=False（require_write，assigned 不能写）。"""
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await assigned_client.delete("/api/v1/subscriptions/bili/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "未找到" in resp.json()["message"]
        mock_remove.assert_not_awaited()  # 越权在路由层被拦，业务函数不调用

    async def test_outsider_cannot_delete(
        self, outsider_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """outsider 删 bili/100 → success=False（不暴露存在性）。"""
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await outsider_client.delete("/api/v1/subscriptions/bili/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_remove.assert_not_awaited()

    async def test_owner_cannot_delete_orphan(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner-bot 删 bili/200（孤儿 sub，非自己 own）→ success=False。

        孤儿 sub 只有 superuser 能管理，owner-bot（非 superuser）无权。
        """
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await owner_client.delete("/api/v1/subscriptions/bili/200")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_remove.assert_not_awaited()

    async def test_invalid_platform_returns_not_found(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """C1 修订：无效平台（非短名非全名）→ success=False（合并「未找到」语义）。"""
        with patch(
            "api.routes.subscriptions.remove_subscription", new_callable=AsyncMock
        ) as mock_remove:
            resp = await owner_client.delete("/api/v1/subscriptions/ghost/100")
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        mock_remove.assert_not_awaited()


class TestOwnershipAssignRoutes:
    """assign/unassign 路由 superuser 专用（issue #108 §7.6）。"""

    async def test_owner_cannot_assign_403(
        self, owner_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """owner 调 assign 路由 → 403（缺 tokens:manage scope）。"""
        resp = await owner_client.post(
            "/api/v1/subscriptions/bili/100/assign",
            json={"token_name": "outsider-bot"},
        )
        assert resp.status_code == 403
        assert "scope" in resp.json()["detail"].lower()

    async def test_assigned_cannot_assign_403(
        self, assigned_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """assigned 调 assign → 403。"""
        resp = await assigned_client.post(
            "/api/v1/subscriptions/bili/100/assign",
            json={"token_name": "outsider-bot"},
        )
        assert resp.status_code == 403

    async def test_superuser_assigns_successfully(
        self, superuser_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 调 assign → 200 + success=True。"""
        with patch(
            "api.routes.subscriptions.assign_token_to_subscription",
            new_callable=AsyncMock,
        ) as mock_assign:
            mock_assign.return_value = (True, "已分配: outsider-bot")
            resp = await superuser_client.post(
                "/api/v1/subscriptions/bili/100/assign",
                json={"token_name": "outsider-bot"},
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_assign.assert_awaited_once()

    async def test_superuser_unassign_successfully(
        self, superuser_client: AsyncClient, tmp_config_with_owned_sub: Path
    ) -> None:
        """superuser 调 unassign → 200 + success=True（幂等）。"""
        with patch(
            "api.routes.subscriptions.unassign_token_from_subscription",
            new_callable=AsyncMock,
        ) as mock_unassign:
            mock_unassign.return_value = (True, "已解绑: assigned-bot")
            resp = await superuser_client.delete(
                "/api/v1/subscriptions/bili/100/assign/assigned-bot"
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_unassign.assert_awaited_once()
```

- [ ] **Step 7.5: test_api_fetch.py — authed_client → superuser_client + fetch superuser-only**

1. **改名 + 重写** `authed_client`（test_api_fetch.py:24-47）为 `superuser_client`，加 `scopes=["tokens:manage"]`（关键：fetch 现在 superuser-only，原 authed_client 空 scopes 会 403）。
   I4 修订（issue #108 review）：原 plan 只说「改名」没给 body，这里显式给出
   `superuser_client` 在 `test_api_fetch.py` 的完整 body（与 Step 7.1 风格一致，
   但适配 fetch 测试本身不需要额外 mock —— fetch 测试的 mock 在各 test method 内）。

   ```python
   @pytest.fixture
   async def superuser_client(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> AsyncClient:
       """持 tokens:manage 的 superuser client（fetch superuser-only 后必需）。

       I4 修订：原 authed_client 空 scopes，#108 后空 = 无权，fetch 会 403。
       显式给 ``tokens:manage`` 让 superuser 通过 ownership 检查。
       """
       auth_path = tmp_path / "auth.toml"
       monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
       monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
       set_password(PASSWORD)

       from api.auth import create_token

       app = create_app()
       plain = create_token("super-bot", scopes=["tokens:manage"])
       transport = ASGITransport(app=app)
       async with AsyncClient(
           transport=transport,
           base_url="http://test",
           headers={"Authorization": f"Bearer {plain}"},
       ) as c:
           c._app = app  # type: ignore[attr-defined]
           yield c
   ```

   全文件 `authed_client` 替换为 `superuser_client`（用 edit replaceAll 或 ast_grep）。
2. **删除** `row_filtered_client`（line 137-171）+ `TestRowLevelFetch`（line 174-225）。
3. **新增** fetch 非 superuser 403 测试:

```python
class TestFetchSuperuserOnly:
    """issue #108: fetch 抓取的消息可能无主，只 superuser 能调。"""

    @pytest.fixture
    async def non_superuser_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncClient:
        """持 messages:write 但无 tokens:manage 的 client。"""
        auth_path = tmp_path / "auth.toml"
        monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
        monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
        set_password(PASSWORD)
        from api.auth import create_token

        plain = create_token("writer-bot", scopes=["messages:write"])
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {plain}"},
        ) as c:
            c._app = app  # type: ignore[attr-defined]
            yield c

    async def test_non_superuser_fetch_returns_403(
        self, non_superuser_client: AsyncClient
    ) -> None:
        """非 superuser 调 fetch → 403。"""
        resp = await non_superuser_client.post(
            "/api/v1/messages/fetch",
            json={"msg_ids": ["bili:BV1xx"], "skip_push": False},
        )
        assert resp.status_code == 403
        assert "tokens:manage" in resp.json()["detail"]

    @patch("api.routes.messages.PipelineEngine")
    @patch("api.routes.messages.load_config", new_callable=AsyncMock)
    @patch("api.routes.messages.MessageStore")
    async def test_superuser_fetch_success(
        self,
        mock_store_cls: Any,
        mock_load: AsyncMock,
        mock_engine: Any,
        superuser_client: AsyncClient,
    ) -> None:
        """superuser 调 fetch → 202（原行为不变）。"""
        mock_load.return_value.general.data_dir = "/tmp"
        mock_engine.run_fetch_and_process = AsyncMock(return_value=1)
        resp = await superuser_client.post(
            "/api/v1/messages/fetch",
            json={"msg_ids": ["bili:BV1xx"], "skip_push": False},
        )
        assert resp.status_code == 202
        await asyncio.sleep(0.05)
        mock_engine.run_fetch_and_process.assert_called_once()
        app = superuser_client._app  # type: ignore[attr-defined]
        app.state.check_running = False
```

- [ ] **Step 7.6: 删除 tests/test_config_ownership_fields.py（Task 1 临时的）**

```bash
git rm tests/test_config_ownership_fields.py
```

（Task 1 数据层测试已被 test_ownership.py + 集成测试覆盖）

- [ ] **Step 7.7: 运行全量测试确认通过**

Run: `uv run pytest -x -v`
Expected: PASS（所有测试通过，无 401/403 异常）

如果失败：
- 401/403 大量出现 → 检查是否还有未改的 `authed_client` 引用（grep）
- `TokenResourceFilter` import 错误 → 检查是否还有 import 旧名（grep）
- mock load_config 路径不对 → 检查 monkeypatch target

- [ ] **Step 7.8: 运行 ruff + pyright 最终确认**

Run: `uv run ruff check .`
Expected: 无错误

Run: `uv run pyright`
Expected: 无 error

- [ ] **Step 7.9: 提交**

```bash
git add tests/test_api_check.py tests/test_api_messages.py tests/test_api_subscriptions.py tests/test_api_fetch.py tests/test_api_auth.py
git rm tests/test_config_ownership_fields.py
git commit -m "test(#108): rewrite fixtures + add ownership matrix tests

- All authed_client → superuser_client (explicit tokens:manage scope)
- Delete row_filtered_client + TestRowLevel* classes (#106 legacy)
- New tmp_config_with_owned_sub fixture: bili/100 (owner+assigned) + bili/200 (orphan) + xhs/u456
- New owner_client / assigned_client / outsider_client fixtures
- New ownership matrix tests for list/get messages, list/delete subs, fetch superuser-only
- Delete TestResourceRulesData + ResourceFilterDep tests in test_api_auth
- Delete temporary test_config_ownership_fields.py"
```

---

## Self-Review 检查清单

实现全部 task 后，按 spec 逐条核对：

### Spec 覆盖检查

M1 修订（issue #108 review）：已核实 spec 文件完整未截断（共 1192 行），
§13「未来工作（非本 PR）」存在（spec line 1181），内容是 non-binding 的
未来计划（ownership 审计日志 / endpoint 行级绑定 / sub 数量上限等），
不构成本 PR 实现要求。

| Spec 章节 | 覆盖 Task | 验证 |
|-----------|-----------|------|
| §4.1 sub 加 owner_token/assigned_tokens 字段 | Task 1 | `tests/test_config_ownership_fields.py`（Task 1 临时的）+ `test_ownership.py` 间接覆盖 |
| §4.4 删 ApiTokenEntry.resource_rules | Task 1 | 同上 |
| §5.1 三态权限公式 | Task 3 | `tests/test_ownership.py::TestTokenOwnershipHasSubAccess/Write` |
| §5.2 assigned 只读 | Task 3 + 4 + 7 | `test_ownership.py::TestTokenOwnershipHasSubWrite::test_assigned_cannot_write` + 集成矩阵 |
| §5.3 孤儿 sub（空 owner） | Task 3 + 7 | `test_ownership.py::test_orphan_sub_only_superuser` + `tmp_config_with_owned_sub` 含 bili/200 孤儿 |
| §6.1 废弃 ResourceRules 清单 | Task 1 + 3 | grep 确认无残留 |
| §6.2 空 scopes 无权 | Task 2 + 7 | `test_api_auth.py::TestTokenHasScopeEmptyScopes` + 所有 fixture 改 superuser_client |
| §7.3 API 路由改造表 | Task 4 + 5 | 11 个路由全覆盖 |
| §7.6 assign/unassign 新路由 | Task 4 | `test_subscription_cli_ownership.py` + `TestOwnershipAssignRoutes` |
| §8.1 删 --resource-platform/sub flag | Task 6 | `TestResourceFlagsRemoved` |
| §8.2 adopt CLI | Task 6 | `TestAdoptCommand` |
| §8.3 create red warning | Task 6 | `TestCreateNoScopesWarning` |
| §9.4 集成测试矩阵 | Task 7 | `TestOwnershipListMessages` 等 |

### 破坏性变更确认

- [ ] `ResourceRules` dataclass 完全删除（grep `ResourceRules` 无残留）
- [ ] `ApiTokenEntry.resource_rules` 字段删除
- [ ] `token_has_scope` 无 `if not token.scopes: return True` 分支
- [ ] `create_token()` 无 `resource_rules` 参数
- [ ] `--resource-platform` / `--resource-sub` CLI flag 删除
- [ ] 所有 `authed_client` fixture 改为 `superuser_client`
- [ ] 所有 `row_filtered_client` + `TestRowLevel*` 删除
- [ ] `tests/test_resource_filter.py` 删除（替换为 `tests/test_ownership.py`）

M6 修订（issue #108 review）：可执行 grep 命令（拷贝即跑，确认无残留）:

```bash
# 1. ResourceRules class 应无残留（含 dataclass / import / 类型注解）
rg ResourceRules --type py
# expected: no matches

# 2. resource_rules 字段 / 参数应无残留（含 ApiTokenEntry / create_token / web.auth I/O）
rg resource_rules --type py
# expected: no matches outside docs/ and CHANGELOG

# 3. msg_id_platform_allowed 调用应已删（messages.py fetch 不再调）
rg msg_id_platform_allowed --type py
# expected: 只在 api/resource_filter.py 定义处（若保留），路由层 0 调用

# 4. TokenResourceFilter 旧名应无残留（路由 / 测试全部走 TokenOwnership）
rg TokenResourceFilter --type py
# expected: no matches

# 5. _normalize_platform 应在 subscriptions.py 定义 + 各路由入口调用
rg _normalize_platform api/routes/subscriptions.py
# expected: 1 定义 + 5 调用点（remove/bind/unbind/assign/unassign）

# 6. 所有 Task 7 测试 fixture body 完整（无 ... 占位）
rg -n "superuser_client|owner_client|assigned_client|outsider_client" tests/
# expected: 每个文件都有显式 def body，无 NotImplementedError
```

### 运行验证

```bash
uv run ruff check .          # 无错误
uv run pyright                # 无 error
uv run pytest -x             # 全量通过
```

---

## 验证总命令

所有 Task 完成后，最终验证：

```bash
cd /home/zyw10/proj/trawler
uv run ruff check .
uv run pyright
uv run pytest -x -v
```

预期：
- ruff: All checks passed
- pyright: 0 errors, 0 warnings
- pytest: 全部测试通过（约 XXX 个测试，含新增 ownership 矩阵约 30+ 个）
