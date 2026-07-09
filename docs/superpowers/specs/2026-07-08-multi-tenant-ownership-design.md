# 多租户订阅所有权模型（Multi-Tenant Ownership）设计

- 日期: 2026-07-08
- 状态: Draft (待用户 review)
- 范围: `shared/config.py`、`shared/protocols.py`、`web/auth.py`、`api/auth.py`、
  `api/resource_filter.py`（重写为 `api/ownership.py` 职责）、`api/routes/{messages,subscriptions}.py`、
  `api/token_tool.py`、`api/schemas.py`、`core/subscription_cli.py`、`tests/test_api_*.py`
- 关联 issue: #108
- 前置:
  - #103（API Token Scopes）已合入并部署
  - #106（API Token 行级权限 Row-Level）已合入但**未部署**（#107 刚合入还没生产），故
    本 spec 决定废弃 #106 的 `ResourceRules` 整套机制，趁没生产直接替换为更直观的
    owner/assigned 模型（见 §1.2）
- 分支: `feat/multi-tenant-ownership-108`

## 1. 背景与动机

issue #106 引入 `ResourceRules`（platforms + subscription_refs 两维 AND 过滤）解决了
「token 能看到哪些行」的问题，但**生产实践中暴露了三个核心缺陷**：

1. **表达力不足**：实际部署场景是「把订阅 A 分配给 bot-token B」，这是**订阅级别的
   所有权/分配关系**，而不是「token 能看哪些平台 + 哪些 ref」。运维心智里没有
   `platforms=["bili"] subscription_refs=["bili:100"]` 这种 AND 组合，只有
   「这个 token 拥有/被分配了哪几个订阅」。

2. **写权限漏洞**：#106 的 `ResourceRules` 只过滤读（GET 静默过滤）、对写路由
   （rerun / delete / bind）做越权合并成「未找到」语义。但 `add_subscription`
   路由**完全不过滤** —— 持 `subscriptions:write` 的 bili-only token 能创建
   xhs 订阅，违反隔离意图。根因是 #106 的模型没有「订阅归属」概念，只能事后过滤，
   不能事前授权。

3. **endpoint 全局暴露**：endpoints 是全局 `[[endpoints]]` AoT，#106 明确「不做
   endpoint 行级绑定」。但 endpoints 含推送 token（敏感凭证），任何持
   `subscriptions:write` 的 token 通过 bind/unbind 路由可以**操控任意 endpoint
   配置**（绑定到任意订阅）。这在多租户场景下不可接受。

### 1.1 与 #103 scope / #106 ResourceRules 的关系

| 维度 | scope（#103） | ResourceRules（#106） | ownership（#108 本 spec） |
|------|---------------|----------------------|---------------------------|
| 答什么 | 能不能调用此路由 | 能看到哪些平台/订阅的行 | 谁拥有/被分配了哪个订阅 |
| 颗粒 | 资源 × 操作 | 平台 × 订阅（数据行） | 订阅 × token（双向关系） |
| 写权限 | scope 通过即可写 | 写路由越权合并成「未找到」 | 写权限独占给 owner/superuser |
| endpoint | — | 不绑定 | superuser 独占（普通 token 不可见） |
| 实现 | FastAPI `Security(scopes=...)` | 路由层手写过滤 + `ResourceRules` | sub 加 `owner_token` + `assigned_tokens` 字段 |
| 状态 | **保留**（操作维度正交） | **废弃**（趁 #107 未部署） | **新增**（替代 #106） |

**关键决策（决策 #4）**：废弃 `ResourceRules`。理由：
- #107（#106 的实现 PR）刚合入 master 还没发版（部署由 git tag 触发，见
  `AGENTS.md` 发版流程），生产无 `ResourceRules` 数据需要迁移
- 趁早换更直观的模型，避免两套权限模型并存增加维护成本
- 删除面已明确（见 §10 清理清单），全是一次性删除，无数据迁移负担

### 1.2 ownership 模型核心思路

每个订阅（`BiliSubscription` / `UserSubscription`）携带两个新字段：

- `owner_token: str` — 创建者 token name，对 sub 拥有**全权 CRUD**
- `assigned_tokens: list[str]` — 被分配的 token name 列表，对 sub 拥有**只读访问**

权限三态：

| 角色 | 权限 |
|------|------|
| **owner** | 全权 CRUD sub（改名 / 删 / 绑 endpoint）+ 读 sub 下消息 |
| **assigned** | 只读：能看 sub 存在、能看 sub 下消息，**不能改 sub** |
| **superuser** | 持 `tokens:manage` scope，bypass 所有 owner/assigned 检查，看全部 |
| outsider | 看不到 sub（不存在语义），看不到 sub 下消息 |

权限公式（§5 详述）：

```
can_access(token, sub) = is_superuser(token) OR sub.owner_token == token.name
                         OR token.name in sub.assigned_tokens
can_write(token, sub)  = is_superuser(token) OR sub.owner_token == token.name
```

## 2. 设计目标

1. **订阅级所有权**：每个 sub 有明确 owner（创建者），owner 全权管理自己的 sub
2. **只读分配**：owner/superuser 能把 sub 分配给其他 token，被分配 token 只读访问
3. **superuser bypass**：持 `tokens:manage` scope 的 token = superuser，绕过所有
   ownership 检查，管理任何 sub（含孤儿 sub）
4. **endpoint 收紧**：endpoint 是全局敏感配置，普通 token 不可见不可改；只有
   superuser（Web session 等价）能 CRUD endpoint
5. **#106 清理**：废弃 `ResourceRules` dataclass + 相关 I/O + 路由过滤 + CLI flag + 测试
6. **破坏性变更明示**：空 scopes 不再 = 全权限（必须显式持 `tokens:manage` 才是
   superuser）；老 sub 无 `owner_token` 字段加载为 `""` = 孤儿，只有 superuser 能管
7. **CLI adopt**：提供 `trawler api-token adopt` 一键给孤儿 sub 补 owner

## 3. 非目标

- **不做 Web UI 分配界面**。与 #103/#106 一致，本 PR 不动 `web/templates/`。token
  ownership 配置只能通过 API + CLI。Web UI 分配界面挂独立 issue
- **不做 owner 转让 API**。owner 被 revoke 后 sub 变孤儿，只有 superuser 能接管
  （通过 adopt CLI 或 assign API）。不支持「owner 转让给另一个 token」的操作
  （YAGNI，运维场景频率极低，superuser 接管足够）
- **不做软删除/回收站**。sub 删了就删了，不保留删除痕迹
- **不做缓存层**。ownership 判断每次请求实时查 auth.toml + subscriptions.toml
- **不做资源数量上限**。不限制单个 token 能 own/assign 多少 sub
- **不做级联权限**。assigned token 不能再把 sub 分配给其他 token（只有 owner/superuser 能 assign）
- **不动 endpoint 数据模型**。`EndpointConfig` 字段不变，只改访问权限（API 层
  endpoints 路由不开放，Web endpoints CRUD 保持现状 = session = superuser）
- **不动 detector/pipeline**。ownership 是纯 API 层概念，detector 遍历
  subscriptions 时只读 uid/notify_endpoints，不受 owner/assigned 影响

## 4. 数据模型

### 4.1 `BiliSubscription` / `UserSubscription` 加字段

`shared/config.py:206-216`：

```python
@dataclass
class BiliSubscription:
    uid: int = 0
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)
    owner_token: str = ""  # 新增（issue #108）：创建者 token name，全权 CRUD
    assigned_tokens: list[str] = field(default_factory=list)  # 新增：被分配 token，只读


@dataclass
class UserSubscription:
    user_id: str = ""
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)
    owner_token: str = ""  # 新增（issue #108）
    assigned_tokens: list[str] = field(default_factory=list)  # 新增
```

两个字段都有默认值（`""` / `[]`），向后兼容老 `subscriptions.toml`（无这两个字段
时加载为空字符串 / 空 list）。

### 4.2 `subscriptions.toml` AoT 序列化

`config/subscriptions.toml` 的 `[[xxx.subscriptions]]` AoT 每条加两个可选字段：

```toml
# 老格式（仍兼容，加载时 owner_token="" assigned_tokens=[]）
[[bilibili.subscriptions]]
uid = 123456
name = "UP1"
notify_endpoints = ["default"]

# 新格式：带 ownership
[[bilibili.subscriptions]]
uid = 123456
name = "UP1"
notify_endpoints = ["default"]
owner_token = "bili-admin-bot"
assigned_tokens = ["reader-bot-1", "reader-bot-2"]
```

**落盘规则**（`core/subscription_cli.py` 改造）：
- `owner_token == ""` → 不写出该字段（保持老格式可读，diff 干净）
- `assigned_tokens == []` → 不写出该字段
- 非空值正常写出

### 4.3 `_parse_config` / `add_subscription` 改造

`shared/config.py:_parse_config:381-409` 现有 `BiliSubscription(**s)` / 
`UserSubscription(**s)` 解析依赖 `_dict_to_dataclass` 风格，但当前是直接 `**s`
展开 —— 新字段有默认值，老 dict 不含这两字段时 dataclass 默认值兜底，**无需改
`_parse_config`**。

`core/subscription_cli.add_subscription:122-186` 创建新 sub 时**注入 owner_token**：

```python
async def add_subscription(
    platform: str,
    identifier: int | str,
    name: str,
    path: str = "config/subscriptions.toml",
    default_notify_endpoint: str | None = None,
    owner_token: str = "",  # 新增参数（issue #108）
) -> tuple[bool, str]:
    ...
    # Append new subscription
    new_entry = tomlkit.table()
    new_entry[key] = typed_id
    new_entry["name"] = name
    if owner_token:  # 非空才写
        new_entry["owner_token"] = owner_token
    arr.append(new_entry)
    ...
```

`owner_token` 参数由 API 路由层注入（创建 sub 的 token name），CLI 直接调
`add_subscription` 时不传（owner_token="" 留给 superuser adopt）。

### 4.4 `ApiTokenEntry` 废弃 `resource_rules` 字段

`shared/config.py:266-285`：

```python
@dataclass
class ApiTokenEntry:
    """API token 条目（``data/auth.toml`` 的 ``[[api_tokens]]`` AoT 行）。

    ``scopes`` 空 list 在 #108 后**不再 = 全权限**（破坏性变更，见 §6.2）。
    要成为 superuser 必须显式持 ``tokens:manage`` scope。

    issue #108 废弃 ``resource_rules`` 字段（趁 #107 未部署直接删，避免两套权限
    模型并存）。ownership 由 sub 上的 ``owner_token`` / ``assigned_tokens`` 表达。
    """

    name: str
    token_hash: str
    created_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    # resource_rules 字段删除（issue #108）
```

`ResourceRules` dataclass（`shared/config.py:248-263`）**整段删除**。

### 4.5 `find_subscription_by_ref` 已存在，复用

`shared/protocols.py:424-456` 的 `find_subscription_by_ref(config, platform, ref)` 
返回 `BiliSubscription | UserSubscription | None`，**messages 路由用它做 msg→sub
反查**（§7.3）。无需改造，返回的 sub 对象自带 `owner_token` / `assigned_tokens`。

## 5. 权限模型

### 5.1 三态角色定义

```
┌─────────────────────────────────────────────────────────────┐
│ is_superuser(token) = tokens:manage in token.scopes         │
│   (注意：#108 后空 scopes ≠ 全权限，必须显式持 tokens:manage)│
└─────────────────────────────────────────────────────────────┘

              ┌────────────────────────┐
              │  can_access(token,sub) │ = is_superuser(token)
              │  （读权限）             │   OR sub.owner_token == token.name
              │                        │   OR token.name in sub.assigned_tokens
              └────────────────────────┘

              ┌─────────────────────────┐
              │  can_write(token, sub)  │ = is_superuser(token)
              │  （写权限）              │   OR sub.owner_token == token.name
              │                         │   (assigned 不能写！)
              └─────────────────────────┘
```

### 5.2 assigned 只读的边界

| 操作 | owner | assigned | outsider | superuser |
|------|-------|----------|----------|-----------|
| 看 sub 存在（GET /subscriptions） | ✓ | ✓ | ✗（不存在语义） | ✓ |
| 看 sub 下消息（GET /messages） | ✓ | ✓ | ✗（404） | ✓ |
| 改 sub name | ✓ | ✗（403） | ✗ | ✓ |
| 删 sub | ✓ | ✗ | ✗ | ✓ |
| 绑/解 endpoint | ✓ | ✗ | ✗ | ✓ |
| rerun sub 下消息 | ✓ | ✗（404） | ✗ | ✓ |
| assign/unassign token | ✗（只 superuser） | ✗ | ✗ | ✓ |
| adopt 孤儿 sub | ✗（只 superuser） | ✗ | ✗ | ✓ |

**关键不对称**：
- **assign/unassign 是 superuser 独占**（连 owner 也不能分配自己的 sub 给别的
  token）—— 决策 #10 明确「assigned token 不能升级为 owner」，为防止权限扩散，
  把分配权收紧到 superuser
- **assigned 不能 rerun**：rerun 是写操作（消耗 pipeline 资源、可能触发推送），
  归 `can_write` 管。assigned token 想重跑只能找 owner 或 superuser

### 5.3 空 owner_token = 孤儿 sub

老 sub 加载时 `owner_token = ""`（dataclass 默认值），等同孤儿。孤儿 sub：
- **只有 superuser 能管理**（改/删/绑 endpoint / assign）
- **assigned_tokens 仍生效**：如果老 sub 已有 `assigned_tokens`（极少见，但允许），
  assigned token 仍能只读访问
- **outvisitor 看不到**：非 superuser / 非 assigned token 看孤儿 sub = 看不到
  （GET 过滤掉、写入路由返回「未找到」语义）
- **adopt CLI**：`trawler api-token adopt --platform <p> --id <id> --owner <token_name>`
  一键给孤儿补 owner（只 superuser 调用，但 CLI 本身 = 管理员 = superuser）

### 5.4 越权不暴露存在性（与 #106 一致）

- **GET 路由**：静默过滤（越权 sub / msg 不出现在响应里）
- **写入路由**：越权时合并成「未找到」语义（200 + `success=False` for sub 写入；
  404 for messages rerun），不暴露 sub 存在

例外：**assign/unassign 路由**是 superuser 专用，非 superuser 调用直接 **403**
（暴露路由存在但不暴露 sub 数据）—— assign 是管理操作，不需要隐藏存在性。

## 6. 破坏性变更

### 6.1 废弃 `ResourceRules` 整套（决策 #4）

删除清单（详见 §10）：

| 位置 | 删除内容 |
|------|----------|
| `shared/config.py:248-263` | `ResourceRules` dataclass 整段 |
| `shared/config.py:285` | `ApiTokenEntry.resource_rules` 字段 |
| `web/auth.py:31,74-88,101-111,142-157` | `_resource_rules_from_dict` 函数 + `load_auth_config` 的 resource_rules 解析 + `save_auth_config` 的 resource_rules 嵌套 table 写出 |
| `api/auth.py:29,182-199,202-229` | `create_token(resource_rules=)` 参数 + `get_resource_filter` 内的 resource_filter 构造（重写为 ownership 视图，§7.1） |
| `api/resource_filter.py` | 整文件重写为 `api/ownership.py` 职责（保留文件名避免大范围 import 改动，但内部类/函数全替换，§7.1） |
| `api/token_tool.py:57-173,196-208` | `--resource-platform` / `--resource-sub` flag + Resource Rules 列显示 |
| `tests/test_resource_filter.py` | 整文件删除（替换为 `tests/test_ownership.py`） |
| `tests/test_api_auth.py:426-575` | `TestResourceRulesData` + `ResourceFilterDep` 部分 |
| `tests/test_api_token_tool.py:260-424` | `TestCreateResourceRules` + `TestListResourceRules` |
| `tests/test_api_messages.py:479-852` | `row_filtered_client` + `TestRowLevel*` 全部 |
| `tests/test_api_subscriptions.py:473-664` | 同上 |
| `tests/test_api_fetch.py:137-225` | 同上 |

### 6.2 空 scopes 不再 = 全权限（决策 #5）

`api/auth.py:82-90` 当前：

```python
def token_has_scope(token: ApiTokenEntry, required: str) -> bool:
    if not token.scopes:  # ← 这一行删除
        return True
    return any(scope_implies(g, required) for g in token.scopes)
```

改造后：

```python
def token_has_scope(token: ApiTokenEntry, required: str) -> bool:
    """token 是否满足 required scope（issue #108 破坏性变更）。

    #105 设计「空 scopes = 全权限」是为了向后兼容老 token，但实际部署中没人
    会真的创建空 scope token（CLI 默认就提示）。#108 把 superuser 收紧为
    「显式持 tokens:manage」，空 scopes token **无任何权限**（连 messages:read
    都没有）。

    要成为 superuser：token.scopes 必须包含 ``tokens:manage``。
    """
    return any(scope_implies(g, required) for g in token.scopes)
```

**影响面**：所有 `authed_client` fixture（4 份：test_api_check.py:33 /
test_api_messages.py:57 / test_api_subscriptions.py:30 / test_api_fetch.py:32）
当前都 `create_token("test-bot")` 不传 scopes → 空 scopes → #108 后无任何权限
→ 所有现有测试 401/403。**必须全部改为 `create_token("super-bot",
scopes=["tokens:manage"])`**（§9.1）。

### 6.3 SCOPE_TOKENS_MANAGE 注释更新

`api/auth.py:48-50` 当前注释「占位 scope，本 PR 不在路由层消费」改为「**superuser
标识 scope**，持此 scope 的 token bypass 所有 owner/assigned 检查（issue #108）」。
常量名 / 值不变（`SCOPE_TOKENS_MANAGE = "tokens:manage"`）。

## 7. 视图层与路由改造

### 7.1 `api/resource_filter.py` 重写为 ownership 视图

文件**保留**（避免改所有 import 路径），但内部类/函数全替换为 ownership 模型。

重写后内容（完整新文件，187 行 → 约 200 行）：

```python
"""token ownership 视图与订阅/消息可见性 helper（issue #108）。

本模块是路由层「消息 / 订阅可见性」判断的唯一集中点：
- ``TokenOwnership``：token 的 ownership 视图（是否 superuser + token name），
  路由层调 ``has_sub_access`` / ``has_sub_write`` 判断
- ``filter_subscription_dict`` / ``subscription_visible``：订阅可见性 helper
- ``msg_id_visible`` / ``message_visible``：消息可见性 helper（需 config 反查 sub）

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

SECTION_TO_SHORT: dict[str, str] = {v: k for k, v in PLATFORM_TO_SECTION.items()}
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
                # config 里查不到（数据不一致），保守丢弃避免越权泄漏
                continue
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

### 7.2 `api/auth.py` 改造

`get_resource_filter` 函数**重命名为 `get_token_ownership`**（保留 `get_resource_filter`
为别名避免破坏 import，但内部返回 `TokenOwnership`）：

```python
async def get_token_ownership(
    security_scopes: SecurityScopes,
    request: Request,
) -> TokenOwnership:
    """FastAPI 依赖：scope 校验 + ownership 视图构造（issue #108）。

    一个依赖同时承担两层职责：
    - 401/403 同 ``require_scopes``
    - 通过 → 返回 ``TokenOwnership.from_token(entry)``

    路由层用 ``ownership.has_sub_access(sub)`` / ``has_sub_write(sub)`` 判断。
    """
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return TokenOwnership.from_token(entry)


# 向后兼容别名（#106 历史命名，#108 重命名后保留别名减少 import 改动）
get_resource_filter = get_token_ownership
```

`create_token` 函数删除 `resource_rules` 参数：

```python
def create_token(
    name: str,
    scopes: list[str] | None = None,
    auth_path: Path = AUTH_TOML_PATH,  # resource_rules 参数删除（issue #108）
) -> str:
    ...
    cfg.api_tokens.append(
        ApiTokenEntry(
            name=name,
            token_hash=_hash_token(plain),
            created_at=datetime.now(timezone.utc).timestamp(),
            scopes=list(scopes) if scopes else [],
            # resource_rules 字段已从 ApiTokenEntry 删除
        )
    )
    ...
```

### 7.3 API 路由改造表

| 文件:行 | 方法 路径 | scope | ownership 校验（#108 新增） |
|--------|----------|-------|------------------------------|
| `subscriptions.py:44-59` | GET `/subscriptions` | `subscriptions:read` | `filter_subscription_dict(result, ownership, config)` 过滤 |
| `subscriptions.py:62-83` | POST `/subscriptions` | `subscriptions:write` | 注入 `owner_token=ownership.token_name` 到 `add_subscription` |
| `subscriptions.py:86-110` | DELETE `/subscriptions/{p}/{id}` | `subscriptions:write` | `subscription_visible(ownership, config, p, id, require_write=True)` → 越权 `success=False` |
| `subscriptions.py:118-147` | POST `/subscriptions/{p}/{id}/endpoints` | `subscriptions:write` | 同上（require_write=True） |
| `subscriptions.py:150-175` | DELETE `/subscriptions/{p}/{id}/endpoints/{ep}` | `subscriptions:write` | 同上 |
| `messages.py:96-146` | GET `/messages` | `messages:read` | `[m for m in matched if message_visible(ownership, config, m)]` |
| `messages.py:154-172` | GET `/messages/{msg_id}` | `messages:read` | `message_visible(ownership, config, rec)` 为 False → 404 |
| `messages.py:180-287` | POST `/messages/rerun` | `messages:write` | `[m for m in existing if message_visible(ownership, config, m)]`；全越权 → 404 |
| `messages.py:295-395` | POST `/messages/fetch` | `messages:write` + **superuser** | **非 superuser → 403**（决策：fetch 无主消息无法判 owner） |
| `subscriptions.py` 新增 | POST `/subscriptions/{p}/{id}/assign` | `tokens:manage` | superuser 专用（scope 即校验） |
| `subscriptions.py` 新增 | DELETE `/subscriptions/{p}/{id}/assign/{token}` | `tokens:manage` | superuser 专用 |

### 7.4 GET `/subscriptions` 过滤实现

```python
@router.get("/subscriptions", response_model=SubscriptionListResponse)
async def list_subs(
    request: Request,
    platform: str | None = Query(default=None),
    ownership: TokenOwnership = Security(get_token_ownership, scopes=["subscriptions:read"]),
    # 新增：load_config 拿 config 对象供 filter_subscription_dict 反查 sub
) -> SubscriptionListResponse:
    result = await list_subscriptions(platform=platform)
    config = await load_config()  # 新增
    result = filter_subscription_dict(result, ownership, config)
    return SubscriptionListResponse(platforms=result)
```

**注意**：`list_subscriptions` 返回的 dict 不含 `owner_token` / `assigned_tokens`
字段（tomlkit dict 透传，实际上含；但 ownership 判断走 `find_subscription_by_ref`
反查 `config` 对象，不依赖 dict 字段 —— 保持单一数据源，避免 tomlkit dict 与
dataclass 字段不一致）。

### 7.5 POST `/subscriptions` 注入 owner

```python
@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    ownership: TokenOwnership = Security(get_token_ownership, scopes=["subscriptions:write"]),
) -> SubscriptionAddResponse:
    success, message = await add_subscription(
        body.platform,
        body.identifier,
        body.name,
        default_notify_endpoint=body.default_notify_endpoint,
        owner_token=ownership.token_name,  # 新增：注入创建者
    )
    return SubscriptionAddResponse(success=success, message=message)
```

`add_subscription` 的 `owner_token` 参数非空时写入 `[[xxx.subscriptions]]` 的
`owner_token` 字段。

### 7.6 新增 assign/unassign 路由（superuser 专用）

```python
class AssignRequest(BaseModel):
    """``POST /subscriptions/{p}/{id}/assign`` 请求体。"""
    token_name: str


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
        get_token_ownership, scopes=["tokens:manage"]  # superuser 专用
    ),
) -> SubscriptionAddResponse:
    """把 token 分配到 sub（只 superuser，spec §5.2）。

    - sub 不存在 → 200 + success=False, message="未找到订阅"
    - token 不存在（不在 auth.toml）→ 200 + success=False, message="未知 token"
    - 已分配（幂等）→ 200 + success=True
    - 成功 → 200 + success=True
    """
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
    """取消分配（只 superuser，幂等）。"""
    success, message = await unassign_token_from_subscription(
        platform=platform, identifier=identifier, token_name=token_name,
    )
    return SubscriptionAddResponse(success=success, message=message)
```

底层 `core/subscription_cli.py` 新增：

```python
async def assign_token_to_subscription(
    platform: str, identifier: int | str, token_name: str,
    path: str = "config/subscriptions.toml",
    auth_path: Path = AUTH_TOML_PATH,  # 校验 token 存在
) -> tuple[bool, str]:
    """把 token_name 加到 sub.assigned_tokens（幂等）。

    返回 ``(True, "已分配")`` / ``(True, "已分配（幂等）")``
        / ``(False, "未找到订阅")`` / ``(False, "未知 token")``
    """
    # 校验 token 存在
    from web.auth import load_auth_config
    auth_cfg = load_auth_config()
    if not any(t.name == token_name for t in auth_cfg.api_tokens):
        return False, f"未知 token: {token_name}"
    # 找 sub + 加 assigned_tokens（去重）
    ...


async def unassign_token_from_subscription(
    platform: str, identifier: int | str, token_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """从 sub.assigned_tokens 移除 token_name（幂等）。"""
    ...
```

### 7.7 GET `/messages` 过滤实现

```python
@router.get("/messages", response_model=MessageListResponse)
async def list_messages(
    request: Request,
    since: str | None = Query(None),
    ...,
    ownership: TokenOwnership = Security(get_token_ownership, scopes=["messages:read"]),
) -> MessageListResponse:
    ...
    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)
    matched = store.query_messages(...)
    # ownership 过滤：msg → sub → has_sub_access
    matched = [m for m in matched if message_visible(ownership, cfg, m)]
    return MessageListResponse(...)
```

### 7.8 POST `/messages/fetch` 限制 superuser

```python
@router.post("/messages/fetch", response_model=FetchResponse, status_code=202)
async def fetch_messages(
    body: FetchRequest,
    request: Request,
    ownership: TokenOwnership = Security(get_token_ownership, scopes=["messages:write"]),
) -> FetchResponse | JSONResponse:
    # issue #108: fetch 抓取的消息可能 subscription_ref 为空（无主），
    # 无法判断 owner，只允许 superuser 调用
    if not ownership.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="fetch requires tokens:manage (superuser only)",
        )
    ...  # 原有锁检查 + 后台 task 逻辑
```

## 8. CLI 扩展

### 8.1 删除 `--resource-platform` / `--resource-sub` flag

`api/token_tool.py:57-70` 的两个 Click option 整段删除。`create` 命令的
`resource_platforms` / `resource_subs` 参数 + 校验逻辑（line 98-136）+ 落盘
（line 146-173）全删。

`list` 命令的 `Resource Rules` 列（line 189, 196-208）删除。

### 8.2 新增 `trawler api-token adopt` 子命令

```bash
# 给孤儿 sub 补 owner
trawler api-token adopt --platform bili --id 123456 --owner bili-admin-bot
```

Click 实现：

```python
@cli.command()
@click.option("--platform", "platform", required=True,
              type=click.Choice(["bili", "xhs", "weibo"]),
              help="平台 short name")
@click.option("--id", "identifier", required=True, help="订阅 id（bili=uid, xhs/weibo=user_id）")
@click.option("--owner", "owner_token", required=True, help="要绑定为 owner 的 token name")
def adopt(platform: str, identifier: str, owner_token: str) -> None:
    """给孤儿 sub 补 owner_token（issue #108）。

    CLI 本身 = 管理员 = superuser 等价，直接改 subscriptions.toml。
    如果 token 不存在（不在 auth.toml）→ 退出码非 0。
    如果 sub 不存在 → 退出码非 0。
    """
    import asyncio
    from core.subscription_cli import set_subscription_owner

    ok, msg = asyncio.run(set_subscription_owner(
        platform=platform, identifier=identifier, owner_token=owner_token,
    ))
    if ok:
        console.print(f"[green]✓[/] {msg}")
    else:
        console.print(f"[red]✗[/] {msg}", style="red")
        sys.exit(1)
```

底层 `core/subscription_cli.py` 新增：

```python
async def set_subscription_owner(
    platform: str, identifier: int | str, owner_token: str,
    path: str = "config/subscriptions.toml",
    auth_path: Path = AUTH_TOML_PATH,
) -> tuple[bool, str]:
    """给 sub 设置 owner_token（adopt CLI 用，issue #108）。

    返回 ``(True, "已设置 owner: ...")`` / ``(False, "未找到订阅")``
        / ``(False, "未知 token")``
    """
    # 校验 token 存在
    from web.auth import load_auth_config
    auth_cfg = load_auth_config()
    if not any(t.name == owner_token for t in auth_cfg.api_tokens):
        return False, f"未知 token: {owner_token}"
    # 找 sub + 写 owner_token
    ...
```

### 8.3 `create` 强制提示空 scopes 风险

#108 后空 scopes = 无权，CLI 必须明示：

```python
if scope_list:
    console.print(f"[cyan]📝[/] Scopes: {', '.join(scope_list)}")
else:
    console.print(
        "[red]⚠️[/] 未指定 scope = [bold]无任何权限[/]（#108 破坏性变更）。"
        " 要创建 superuser token 加 --scope tokens:manage；"
        " 要创建只读 token 加 --scope messages:read --scope subscriptions:read。",
        style="red",
    )
```

（原来是「未指定 scope = 全权限」的 yellow warning，改为 red + 明确无权）

## 9. 测试策略

### 9.1 Fixture 重写：`authed_client` → `superuser_client`

4 份 `authed_client`（test_api_check.py:33 / test_api_messages.py:57 /
test_api_subscriptions.py:30 / test_api_fetch.py:32）全部改名 + 改 scopes：

```python
@pytest.fixture
async def superuser_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncClient:
    """持 tokens:manage 的 superuser client（#108 后空 scopes 无权，必须显式）。"""
    auth_path = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", auth_path)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", auth_path)
    set_password(PASSWORD)

    from api.auth import create_token

    app = create_app()
    plain = create_token("super-bot", scopes=["tokens:manage"])  # 关键改动
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {plain}"},
    ) as c:
        c._app = app  # type: ignore[attr-defined]
        yield c
```

所有原 `authed_client` 用例改为 `superuser_client`（保持 superuser 看全部的语义）。

`test_api_auth.py:583` 的 `authed_client` 同样改。

### 9.2 Fixture 重写：`row_filtered_client` → `owner_client` / `assigned_client` / `outsider_client`

3 份 `row_filtered_client`（test_api_messages.py:480 / test_api_subscriptions.py:474 /
test_api_fetch.py:137）全部删除，替换为三个新 fixture（在每个测试文件中独立定义，
与原 `row_filtered_client` 同模式）：

```python
@pytest.fixture
async def owner_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncClient:
    """owner token 的 client。

    request.param 是 list[str] scopes（默认 ["subscriptions:write", "messages:read"]）。
    配合 fixture 写盘 sub 时 owner_token = "owner-bot"。
    """
    scopes = getattr(request, "param", ["subscriptions:write", "messages:read"])
    # ... 同模式 create_token("owner-bot", scopes=scopes)
    ...


@pytest.fixture
async def assigned_client(...) -> AsyncClient:
    """assigned token 的 client（被分配只读访问某个 sub）。

    request.param 同上。
    配合 fixture 写盘 sub 时 assigned_tokens = ["assigned-bot"]。
    """
    ...


@pytest.fixture
async def outsider_client(...) -> AsyncClient:
    """ outsider token 的 client（既非 owner 也非 assigned，无权访问）。"""
    ...
```

配合的 sub 落盘 fixture（在每个测试文件中）：

```python
@pytest.fixture
def tmp_config_with_owned_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """写盘 config/subscriptions.toml 含一条 owner_token='owner-bot' 的 bili sub。

    - uid=100, owner_token="owner-bot", assigned_tokens=["assigned-bot"]
    - uid=200, owner_token="" (孤儿)
    - xhs user_id=u456, owner_token="owner-bot"
    """
    ...
```

### 9.3 新增 `tests/test_ownership.py`（替换 `tests/test_resource_filter.py`）

纯逻辑测试 `TokenOwnership` 视图对象：

- `from_token(token_with_tokens_manage)` → `is_superuser=True`
- `from_token(token_with_messages_read_only)` → `is_superuser=False`
- `has_sub_access(sub)`：superuser / owner / assigned / outsider 四态
- `has_sub_write(sub)`：superuser / owner True；assigned / outsider False
- `can_manage_assign()`：只 superuser True
- `frozen` dataclass 不可变

### 9.4 集成测试矩阵

每个 API 路由加 4 角色测试矩阵：

| 路由 | superuser | owner | assigned | outsider |
|------|-----------|-------|----------|----------|
| GET /subscriptions | 看全部 | 看自己的 | 看分配的 | 空 |
| GET /messages | 看全部 | 看自己 sub 的 | 看分配 sub 的 | 空 |
| GET /messages/{id} | 200 | 200（自己 sub） | 200（分配 sub） | 404 |
| POST /subscriptions | 创建，owner=自己 | 创建，owner=自己 | 创建，owner=自己（只要有 subscriptions:write） | 403（无 scope） |
| DELETE /subscriptions/{p}/{id} | 成功 | 成功（自己 sub） | success=False | success=False |
| bind/unbind endpoint | 成功 | 成功（自己 sub） | success=False | success=False |
| POST /messages/rerun | 成功 | 成功（自己 sub 的 msg） | 全越权→404 | 404 |
| POST /messages/fetch | 202 | **403**（只 superuser） | **403** | 403/401 |
| POST /assign | 成功 | **403**（scope 拦） | **403** | **403** |
| DELETE /assign/{token} | 成功 | **403** | **403** | **403** |

### 9.5 孤儿 sub 测试

- 老 sub（无 owner_token）加载后 `owner_token = ""`
- superuser 能管理孤儿 sub（改 / 删 / assign）
- assigned token 仍能只读访问（如果 assigned_tokens 非空）
- outsider 看不到孤儿 sub
- adopt CLI 一键补 owner 后，owner token 能管理

### 9.6 兼容性测试

- 老 `subscriptions.toml`（无 owner_token / assigned_tokens）→ 加载默认值 `""` / `[]`
- 老 `auth.toml`（含 `resource_rules` 字段）→ 加载时**忽略** resource_rules（不报错，
  但字段不再生效）—— `_resource_rules_from_dict` 删除后，`load_auth_config` 直接
  `t.get("resource_rules", {})` 不读，多余字段被 tomlkit 保留但不影响 dataclass

## 10. #106 清理清单（删除面）

完整删除清单（plan Task 1 执行）：

```
shared/config.py
  ├─ 248-263: ResourceRules dataclass（整段删）
  └─ 285: ApiTokenEntry.resource_rules 字段（删一行）

web/auth.py
  ├─ 31: from shared.config import ... ResourceRules ...（删 ResourceRules）
  ├─ 74-88: _resource_rules_from_dict 函数（整段删）
  ├─ 107: load_auth_config 的 resource_rules=_resource_rules_from_dict(...) 调用（删）
  └─ 142-157: save_auth_config 的 resource_rules 嵌套 table 写出（删）

api/auth.py
  ├─ 29: from api.resource_filter import TokenResourceFilter（→ TokenOwnership）
  ├─ 29: from shared.config import ... ResourceRules（删 ResourceRules）
  ├─ 182-199: get_resource_filter（重写为 get_token_ownership，返回 TokenOwnership）
  └─ 202-229: create_token(resource_rules=) 参数（删）

api/resource_filter.py
  └─ 整文件重写（187 行 → 约 200 行，TokenResourceFilter → TokenOwnership）

api/token_tool.py
  ├─ 57-70: --resource-platform / --resource-sub flag（删）
  ├─ 98-136: resource platform/sub 校验 + 无意义组合 warning（删）
  ├─ 146-173: create 内 ResourceRules 构造 + 落盘 + 提示（删）
  └─ 189, 196-208: list 的 Resource Rules 列（删）

api/routes/subscriptions.py
  └─ 全文件改造（filt → ownership，新增 assign/unassign 路由）

api/routes/messages.py
  └─ 全文件改造（filt → ownership，fetch 加 superuser 检查）

api/schemas.py
  └─ 新增 AssignRequest

core/subscription_cli.py
  ├─ add_subscription: 加 owner_token 参数 + 落盘
  ├─ 新增 assign_token_to_subscription
  ├─ 新增 unassign_token_from_subscription
  └─ 新增 set_subscription_owner（adopt 用）

tests/test_resource_filter.py
  └─ 删整文件，替换为 tests/test_ownership.py

tests/test_api_auth.py
  ├─ 426-575: TestResourceRulesData + get_resource_filter 相关（删 / 改）
  └─ 583: authed_client → superuser_client

tests/test_api_token_tool.py
  └─ 260-424: TestCreateResourceRules + TestListResourceRules（删）

tests/test_api_messages.py
  ├─ 57: authed_client → superuser_client
  ├─ 479-852: row_filtered_client + TestRowLevel*（删，替换为 owner/assigned/outsider 矩阵）
  └─ 新增 tmp_config_with_owned_sub fixture

tests/test_api_subscriptions.py
  ├─ 30: authed_client → superuser_client
  └─ 473-664: row_filtered_client + TestRowLevel*（删，替换）

tests/test_api_fetch.py
  ├─ 32: authed_client → superuser_client
  └─ 137-225: row_filtered_client + TestRowLevelFetch（删，替换 + 加 fetch superuser-only 测试）

tests/test_api_check.py
  └─ 33: authed_client → superuser_client

config/subscriptions.toml.example
  └─ 加 owner_token / assigned_tokens 注释示例
```

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 空 scopes 老 token #108 后无权，破坏现有部署 | #107 未部署，生产无 token 数据；迁移文档明确「升级前手动给老 token 加 tokens:manage」；CLI `create` 强制 red warning |
| fetch 路由收紧为 superuser-only，破坏现有 bot 集成 | 文档明确「fetch 是管理操作」；普通 bot 用 rerun 替代（rerun 只处理已入库消息，有 owner 判断）；破坏性变更在 CHANGELOG 标红 |
| `find_subscription_by_ref` 反查 sub 性能（每次请求 N 次） | subscriptions 数量通常 < 100，线性扫描可接受；若未来成为瓶颈再加索引（YAGNI） |
| `list_subscriptions` 返回的 tomlkit dict 含 owner_token / assigned_tokens 字段（敏感） | `filter_subscription_dict` 透传 dict，响应里会暴露 owner_token name（不是 token hash，敏感度低）；未来如需隐藏，在 helper 里 strip 字段（挂独立 issue） |
| assigned_tokens 列表可能含已被 revoke 的 token name | 不影响正确性（revoke 后 name 不再匹配任何 token，assigned 判断恒 False）；superuser 可通过 assign 路由重新绑定新 token；不做自动清理（YAGNI） |
| 孤儿 sub 累积（owner 被 revoke 后无 adopt） | superuser 通过 list 看到孤儿（owner_token="" 的 sub）；adopt CLI 一键补 owner；不做自动检测告警（YAGNI） |
| 老 auth.toml 含 resource_rules 字段，加载时被忽略但文件里残留 | 不主动清理（避免 tomlkit 改写风险）；下次 `save_auth_config` 全量重写时自然消失 |
| messages 路由 ownership 过滤需要 config + store 两次 IO（性能） | `list_messages` 本来就要 load_config（拿 data_dir），无额外 IO；`message_visible` 用已加载的 config，无重复 |
| tomlkit AoT 写 owner_token / assigned_tokens 序列化格式异常 | plan Task 1 加 round-trip 单元测试覆盖 4 种形态（无字段 / owner only / assigned only / both） |
| 测试 fixture 大范围重写引入回归 | plan Task 7 独立 task，先跑现有测试确认全绿（superuser_client 替换后行为等价），再加新矩阵 |

## 12. 验证清单

实现完成后必须通过：

```bash
uv run ruff check .
uv run pyright
uv run pytest -x tests/test_ownership.py -v
uv run pytest -x tests/test_api_auth.py -v
uv run pytest -x tests/test_api_token_tool.py -v
uv run pytest -x tests/test_api_messages.py -v
uv run pytest -x tests/test_api_subscriptions.py -v
uv run pytest -x tests/test_api_fetch.py -v
uv run pytest -x                                     # 全量回归
```

手动验证：

```bash
# 1. 创建 superuser token
python -m api.token_tool create admin-bot --scope tokens:manage

# 2. 创建普通 owner token（持 subscriptions:write）
python -m api.token_tool create bili-owner \
    --scope subscriptions:write --scope subscriptions:read --scope messages:read

# 3. 创建 assigned token（只读）
python -m api.token_tool create reader-bot --scope messages:read --scope subscriptions:read

# 4. 用 bili-owner 创建订阅（自动成为 owner）
curl -X POST http://localhost:8000/api/v1/subscriptions \
    -H "Authorization: Bearer <bili-owner-token>" \
    -H "Content-Type: application/json" \
    -d '{"platform": "bili", "identifier": "100", "name": "UP100"}'

# 5. 用 admin-bot 分配 reader-bot 到这个 sub
curl -X POST http://localhost:8000/api/v1/subscriptions/bili/100/assign \
    -H "Authorization: Bearer <admin-bot-token>" \
    -H "Content-Type: application/json" \
    -d '{"token_name": "reader-bot"}'

# 6. reader-bot 能看 sub（只读）
curl http://localhost:8000/api/v1/subscriptions \
    -H "Authorization: Bearer <reader-bot-token>"
# 预期：能看到 bili uid=100

# 7. reader-bot 不能删 sub（只读）
curl -X DELETE http://localhost:8000/api/v1/subscriptions/bili/100 \
    -H "Authorization: Bearer <reader-bot-token>"
# 预期：200 + success=False

# 8. outsider 看不到 sub
curl http://localhost:8000/api/v1/subscriptions \
    -H "Authorization: Bearer <outsider-token>"
# 预期：platforms={} 空

# 9. fetch 只 superuser 能调
curl -X POST http://localhost:8000/api/v1/messages/fetch \
    -H "Authorization: Bearer <bili-owner-token>" \
    -H "Content-Type: application/json" \
    -d '{"msg_ids": ["bili:BV1xx"]}'
# 预期：403 + "fetch requires tokens:manage"

# 10. adopt 孤儿 sub
python -m api.token_tool adopt --platform bili --id 200 --owner bili-owner
```

## 13. 未来工作（非本 PR）

挂独立 issue：

- **Web UI ownership 管理**：subscriptions.html 加「owner / assigned tokens」显示 +
  HTMX 分配界面（与 #103 Web UI 同期）
- **owner 转让 API**：支持 owner 把 sub 转让给另一个 token（不经过 superuser）
- **assigned token 自动清理**：revoke token 时自动从所有 sub.assigned_tokens 移除
- **列级权限**：assigned token 看消息时隐藏 body / summary（只看 title）
- **ownership 审计日志**：记录每次 assign/unassign/adopt 操作
- **endpoint 行级绑定**：把 endpoint 也纳入 ownership 模型（目前是 superuser 独占）
- **sub 数量上限**：防止单个 token own 过多 sub 拖慢过滤
