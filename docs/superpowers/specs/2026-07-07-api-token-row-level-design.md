# API Token 行级权限（Row-Level）设计

- 日期: 2026-07-07
- 状态: Draft (待用户 review)
- 范围: `shared/config.py`、`web/auth.py`、`api/auth.py`、`api/routes/{messages,subscriptions}.py`、`api/token_tool.py`、`tests/test_api_*.py`
- 关联 issue: #106
- 前置: #103（API Token Scopes）已合入；本 spec 在 scope 之上叠加「行级过滤」维度

## 1. 背景与动机

issue #103 给每个 token 加了 7 个 scope（`messages:read` / `subscriptions:write`
…），解决「token 拿到就能干所有事」的**操作维度**问题。但 scope 只能回答
「**这个 token 能不能 GET /messages**」，不能回答「**这个 token 能看到哪些
平台 / 哪些订阅的消息**」。

实际部署场景里，运维常常需要做**资源隔离**：

- 给「B 站通知 bot」一个 token，只让它看 bili 平台的消息，不能拉 xhs/weibo
- 给「某个 UP 主的转播 bot」一个 token，只让它看 `bili:100`（uid=100）这条订阅
  的消息，不能看其他 UP
- 给「内部测试 bot」一个 token，绑死到一个测试订阅，避免污染生产数据

issue #106 要求在 scope 之上引入**行级过滤（row-level filtering）**：每个 token
可以声明它能访问的 **platform 集合** 和 **subscription_ref 集合**，路由层强制
过滤响应（GET）和拒绝越权（写入）。本 spec 锁定设计决策、给出实现蓝图、明确
非目标。

### 1.1 与 #103 scope 的关系

| 维度 | scope（#103） | 行级过滤（#106） |
|------|---------------|------------------|
| 答什么 | 能不能调用此路由 | 能看到哪些行 |
| 颗粒 | 资源 × 操作（13 个路由 × read/write） | 平台 × 订阅（数据行） |
| 实现 | FastAPI `Security(scopes=...)` | 路由层手写过滤 |
| 关系 | **正交**，叠加使用 | **正交**，叠加使用 |
| 失败码 | 403（缺 scope） | GET：静默过滤；写入：404 |

scope 通过 = 身份够格；行级过滤 = 数据可见。两层 AND。

## 2. 目标

1. 给每个 token 关联一个 `ResourceRules`：可访问的 platform 列表 +
   subscription_ref 列表。
2. GET 路由自动过滤响应（messages / subscriptions）—— 越权行不返回，调用方
   不感知被过滤。
3. 写入路由（rerun / fetch）对越权 id 返回 404，不暴露存在性。
4. 空 `ResourceRules`（`platforms=None` 且 `subscription_refs=None`）= 全权限，
   与 #103 空 scope 语义对齐，向后兼容老 token。
5. CLI 扩展：`token create --resource-platform ... --resource-sub ...` multi-flag，
   `token list` 显示规则。
6. 复合 key 规范化：`subscription_ref` 统一写成 `<platform_short>:<id>`（与
   `msg_id` 前缀风格一致），让 token 规则**自带平台归属**，避免「bili 平台 +
   xhs subscription_ref」这种无意义组合。

## 3. 非目标

- **不做 Web UI**。本 PR 不动 `web/templates/` / `web/routes/`，token 行级规则
  只能通过 CLI 配置。Web UI token 管理挂独立 issue（与 #103 一致）。
- **不做 endpoint 行级绑定**。本 PR 只过滤 messages 和 subscriptions 两个资源
  类，不过滤 endpoints（endpoints 是全局配置，不属于「订阅的行」）。
- **不做列级权限**（如「只能看 msg 的 title 不能看 body」）。YAGNI。
- **不引入通配符规则**（如 `bili:*` 表示所有 bili 订阅）。用 `platforms` 字段
  即可表达「全部 bili」，不需要在 `subscription_refs` 里写通配。
- **不进 MessageStore**。行级过滤逻辑在路由层手写，不污染 store API（store 是
  纯存储层，不应该知道「token」这个概念）。
- **不进中间件**。行级过滤需要路由知道响应 shape（list 还是单条），不适合
  中间件统一处理；放在路由层最简单。
- **不改 scope 体系**。本 PR 不动 7 个 scope 常量，只在 `require_scopes` 之上
  叠一层 `get_resource_filter` 依赖。

## 4. 数据模型

### 4.1 `ResourceRules` dataclass（新增，`shared/config.py`）

```python
@dataclass
class ResourceRules:
    """token 行级过滤规则（spec §4）。

    所有字段 None 表示**不限制**该维度；空 list 表示**禁止一切**（与
    ``scopes == []`` 的「全权限」语义**相反**，见 §5.3）。

    - ``platforms``: 允许的平台 short name 列表（``"bili"`` / ``"xhs"`` /
      ``"weibo"``）。None = 不限平台；``[]`` = 拒绝所有平台。
    - ``subscription_refs``: 允许的订阅复合 key 列表，格式
      ``<platform_short>:<id>``（如 ``"bili:100"`` / ``"xhs:u456"``）。
      None = 不限订阅；``[]`` = 拒绝所有订阅。
    """

    platforms: list[str] | None = None
    subscription_refs: list[str] | None = None
```

### 4.2 `ApiTokenEntry` 加 `resource_rules` 字段

```python
@dataclass
class ApiTokenEntry:
    name: str
    token_hash: str
    created_at: float = 0.0
    scopes: list[str] = field(default_factory=list)
    resource_rules: ResourceRules = field(default_factory=ResourceRules)  # 新增
```

默认 `ResourceRules()`（两字段都 None）= 全权限，与现有 token 行为兼容。

### 4.3 `auth.toml` 嵌套 table 序列化

老格式（无 `[resource_rules]` 子表）继续兼容：

```toml
# 老格式（仍兼容，加载时 resource_rules 默认全权限）
[[api_tokens]]
name = "bot-1"
token_hash = "abc..."
created_at = 1717500000.0
scopes = []

# 新格式：受限 token
[[api_tokens]]
name = "bili-only"
token_hash = "def..."
created_at = 1717500000.0
scopes = ["messages:read"]
resource_rules.platforms = ["bili"]
resource_rules.subscription_refs = ["bili:100", "bili:200"]
```

`web/auth.py` 的 `load_auth_config` / `save_auth_config` 改造（line 84-99 /
102-132）：

- **读**：每个 token dict 取 `t.get("resource_rules", {})`，转 `ResourceRules`
  dataclass（两字段 `None` 默认）。
- **写**：非默认 `ResourceRules()` 时写 `[resource_rules]` 嵌套 table；默认
  值时**省略**（保持老 token 文件可读、diff 干净）。
- **tomlkit**：嵌套 table 用 `tomlkit.table()` + `nested = tomlkit.table()`
  + `entry["resource_rules"] = nested`，或 `tomlkit.item({"platforms": [...]})`
  一次构造。本 PR 用前者（更易控制空 list 序列化）。

### 4.4 `_dict_to_dataclass` 嵌套支持

现有 `shared/config.py:307-325` 的 `_dict_to_dataclass` 已支持嵌套 dataclass
（line 321-322 检查 `hasattr(field_type, "__dataclass_fields__")`）。但
`load_auth_config` **不走** `_dict_to_dataclass`（手写解析，line 84-93），所以
`ResourceRules` 解析需要在 `web/auth.py` 单独处理 —— 不复用 `_dict_to_dataclass`，
保持与现有 `scopes` 手写解析风格一致。

## 5. 行级过滤语义

### 5.1 过滤维度

两个维度 AND 组合：

```
allows(msg) = allows_platform(msg.platform) AND allows_subscription(msg.platform, msg.subscription_ref)
```

- **platform 维度（粗）**：token 只声明了 `platforms=["bili"]`，则 xhs/weibo
  的消息全部不可见。
- **subscription_ref 维度（细）**：token 声明了 `subscription_refs=["bili:100"]`，
  则只有 uid=100 这个 UP 的消息可见，其他 bili UP 的消息不可见。

### 5.2 AND 组合的语义细节

两个维度同时设置时是 AND（不是 OR）—— 必须同时满足。

| `platforms` | `subscription_refs` | 允许的消息 |
|-------------|---------------------|-----------|
| `None` | `None` | 全部（默认 / 全权限） |
| `["bili"]` | `None` | 所有 bili 平台消息 |
| `None` | `["bili:100"]` | uid=100 的消息（隐含 platform=bili） |
| `["bili", "xhs"]` | `["bili:100", "xhs:u456"]` | uid=100 的 bili 消息 **或** user=u456 的 xhs 消息 |

最后一行看似 OR，实际是「同维内 OR + 跨维 AND」：

```
allows(msg) =
    (msg.platform ∈ platforms_or_unrestricted)
    AND
    (make_ref(msg.platform, msg.subscription_ref) ∈ subs_or_unrestricted)
```

其中 `platforms_or_unrestricted = platforms if platforms is not None else ALL_PLATFORMS`，
subscription_refs 类似。

注意 subscription_ref 维度隐含 platform —— 因为 `<platform_short>:<id>` 自带
平台前缀，所以「`subscription_refs=["xhs:u456"]` 且 `platforms=["bili"]`」会
拒绝一切消息（xhs 订阅被 platforms=bili 卡掉，bili 消息又被 subscription_refs
卡掉）。这种组合在语义上虽然合法，但实际无意义，CLI 创建时应提示（不强制
拒绝）。

### 5.3 空 `ResourceRules` vs 空 list

| 字段值 | 含义 |
|--------|------|
| `ResourceRules()`（两字段 None） | **全权限**（向后兼容老 token） |
| `ResourceRules(platforms=[])` | **拒绝所有平台**（合理用于「只允许看订阅但禁平台」？不，AND 组合下空 list 会拒绝一切） |
| `ResourceRules(subscription_refs=[])` | **拒绝所有订阅** |

**注意**：这与 `scopes == []` = 全权限的语义**相反**。设计差异原因：

- scope 是「能做什么」，空 list = 没限制 = 全权限（向后兼容，老 token 没有
  scope 字段时被解读为全权限）
- resource_rules 是「能看到哪些资源」，已经在 dataclass 字段层面用 `None`
  表达「不限制」；空 list 表达「显式拒绝一切」是更直观的集合语义
  （`x ∈ []` 永远 False）

如果用户 CLI 误传空 list（如 `--resource-platform ""`），会在创建时被解读为
「拒绝一切」，导致 token 完全无用。CLI 层在 §8 提示但**不阻止**（管理员可能
真想创建一个「占位 token」暂时禁用所有访问）。

### 5.4 subscription_ref 复合 key 格式

统一写成 `<platform_short>:<id>`，与 `msg_id` 前缀风格完全一致：

| 平台 | platform_short | id 字段 | 示例 subscription_ref |
|------|----------------|---------|----------------------|
| bilibili | `bili` | `sub.uid`（str） | `bili:100` |
| xiaohongshu | `xhs` | `sub.user_id` | `xhs:u456` |
| weibo | `weibo` | `sub.user_id` | `weibo:u1` |

**为什么带前缀**：

1. 与 msg_id 风格一致（`bili:BV1xx`），运维一眼能看出归属
2. token 规则自带 platform 维度校验，避免「`subscription_refs=["100"]`」这种
   跨平台二义性（bili uid=100 还是 weibo uid=100？）
3. 与本 spec §5.2 「subscription_ref 维度隐含 platform」的过滤逻辑天然契合

**MessageRecord.subscription_ref 实际值**（detector 注入）**不带前缀**
（`platforms/bilibili/handlers.py:56` 注入 `str(sub.uid)` 即 `"100"`）。
路由层过滤时需要用 `f"{msg.platform}:{msg.subscription_ref}"` 拼出复合 key
再比对 token 的 `subscription_refs`。**不改 detector 注入逻辑**（保持 store
内部数据格式不变，向后兼容 messages.json）。

## 6. 过滤层架构

### 6.1 两层 DAG：`require_scopes`（L1）+ `get_resource_filter`（L2）

```
HTTP Request
    │
    ▼
┌─────────────────────────────────────────┐
│ Security(require_scopes, scopes=[...])  │  L1: scope 校验（#103 已有）
│  - 401 身份错                            │
│  - 403 缺 scope                          │
│  通过 → 返回 token name                  │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ get_resource_filter(...)                 │  L2: 行级过滤（#106 新增）
│  - Security(require_scopes) 拿 token name│
│  - 查 token.resource_rules → 构造 filter │
│  通过 → 返回 TokenResourceFilter         │
└─────────────────────────────────────────┘
    │
    ▼
Route handler
    │
    ├─ GET 路由：用 filter 过滤响应
    └─ 写入路由：用 filter 拒绝越权 id（404）
```

### 6.2 `TokenResourceFilter` dataclass + 工具方法

新增 `api/resource_filter.py`（薄模块，纯逻辑无 IO）：

```python
@dataclass(frozen=True)
class TokenResourceFilter:
    """token 行级过滤视图（spec §6）。

    不可变（frozen），从 token ``ResourceRules`` 一次性构造，路由层调用
    ``allows_*`` 方法判断。None 字段（``platforms`` / ``subscription_refs``）
    表示不限制。
    """

    platforms: frozenset[str] | None
    subscription_refs: frozenset[str] | None

    @classmethod
    def from_token(cls, token: ApiTokenEntry) -> TokenResourceFilter:
        """从 token 的 ResourceRules 构造。"""
        rules = token.resource_rules
        return cls(
            platforms=frozenset(rules.platforms) if rules.platforms is not None else None,
            subscription_refs=(
                frozenset(rules.subscription_refs)
                if rules.subscription_refs is not None
                else None
            ),
        )

    @classmethod
    def unrestricted(cls) -> TokenResourceFilter:
        """全权限视图（无任何限制）。"""
        return cls(platforms=None, subscription_refs=None)

    def allows_platform(self, platform: str) -> bool:
        """platform 是否可见。"""
        if self.platforms is None:
            return True
        return platform in self.platforms

    def allows_subscription(self, platform: str, subscription_ref: str) -> bool:
        """订阅是否可见。subscription_ref 是原始 detector 注入值（不带前缀）。"""
        if self.subscription_refs is None:
            return True
        # 拼复合 key 比对（spec §5.4）
        composite = f"{platform}:{subscription_ref}"
        return composite in self.subscription_refs

    def allows_message(self, msg: MessageRecord) -> bool:
        """消息是否可见（platform + subscription_ref AND 组合，spec §5.2）。"""
        return self.allows_platform(msg.platform) and self.allows_subscription(
            msg.platform, msg.subscription_ref
        )
```

### 6.3 FastAPI 依赖：`get_resource_filter`

新增 `api/auth.py`（追加，不重构 `require_scopes`）：

```python
async def get_resource_filter(
    token_name: str = Security(require_scopes, scopes=[]),
) -> TokenResourceFilter:
    """FastAPI 依赖：拿到当前 token 的行级过滤视图（spec §6）。

    - 通过 ``Security(require_scopes, scopes=[])`` 复用 scope 校验链路
      （``scopes=[]`` 表示本依赖不要求额外 scope，路由层在 ``@router.get``
      装饰器里仍可独立声明 scope）
    - 找不到 token（理论上不会发生，require_scopes 已校验）→ 返回 unrestricted
    - 找到 token → ``TokenResourceFilter.from_token`` 构造视图
    """
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if entry.name == token_name:
            return TokenResourceFilter.from_token(entry)
    return TokenResourceFilter.unrestricted()
```

**关键**：`get_resource_filter` 自身不声明 scope（`scopes=[]`），路由层的
`Security(require_scopes, scopes=[...])` 与 `Depends(get_resource_filter)` 在
**同一个路由**上叠加 —— FastAPI 会先跑 `require_scopes`（401/403），通过后
`get_resource_filter` 内部再次 `Security(require_scopes)` 拿到 token name
（此时不会重复 401/403，因为同一个 request 内 FastAPI 缓存依赖结果）。

**或者**更直接的写法（避免重复声明 Security）：

```python
@router.get("/messages")
async def list_messages(
    request: Request,
    filter: TokenResourceFilter = Security(get_resource_filter, scopes=["messages:read"]),
):
    ...
```

把 `get_resource_filter` 直接当 Security 依赖，FastAPI 会把它当 `require_scopes`
的「下游」调用（即 `get_resource_filter` 内部声明对 `require_scopes` 的依赖）。
本 spec 推荐这种写法 —— 一个依赖同时管 scope + 行级过滤，路由签名简洁。

实现层 `get_resource_filter` 改写：

```python
async def get_resource_filter(
    security_scopes: SecurityScopes,
    request: Request,
) -> TokenResourceFilter:
    """兼任 scope 校验 + 行级过滤视图构造（spec §6.3 推荐写法）。"""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    plain = auth[len("Bearer "):]
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if _verify_token(plain, entry.token_hash):
            for required in security_scopes.scopes:
                if not token_has_scope(entry, required):
                    raise HTTPException(
                        status_code=403,
                        detail=f"insufficient scope: requires {required}",
                    )
            return TokenResourceFilter.from_token(entry)
    raise HTTPException(status_code=401, detail="invalid or missing token")
```

这种写法把 `require_scopes` 的逻辑直接内联到 `get_resource_filter`，让一个
依赖函数同时承担两层职责。**缺点**：与 `require_scopes` 有逻辑重复（401/403
处理代码两份）。**缓解**：把 401/403 抽成私有 helper `_authenticate_and_check_scope`
供两个 public 函数复用。最终采用哪种写法在 plan 阶段决定（plan Task 3 给出
最终签名）。

## 7. 路由影响面

| 文件:行 | 方法 路径 | scope（已有） | 行级过滤（新增） |
|--------|----------|--------------|------------------|
| `messages.py:95` | GET `/messages` | `messages:read` | 用 `filter.allows_message` 过滤 list |
| `messages.py:146` | GET `/messages/{msg_id}` | `messages:read` | `filter.allows_message(rec)` 为 False → 404 |
| `messages.py:166` | POST `/messages/rerun` | `messages:write` | 越权 msg_id 视为不存在；全部越权 → 404（spec §8.1） |
| `messages.py:271` | POST `/messages/fetch` | `messages:write` | 按 msg_id 前缀过滤 platform；越权 id 报 404（spec §8.2） |
| `subscriptions.py:39` | GET `/subscriptions` | `subscriptions:read` | 按 platform + 订阅 id 过滤 |
| `subscriptions.py:50` | POST `/subscriptions` | `subscriptions:write` | **不过滤**（写入是「创建新订阅」，新订阅 id 未知） |
| `subscriptions.py:72` | DELETE `/subscriptions/{p}/{id}` | `subscriptions:write` | 越权 → 200 + `success=False, message="未找到"`（与现有 404 语义对齐，不暴露存在性） |
| `subscriptions.py:94` | POST `/subscriptions/{p}/{id}/endpoints` | `subscriptions:write` | 越权 → 200 + `success=False, message="未找到订阅"` |
| `subscriptions.py:121` | DELETE `/subscriptions/{p}/{id}/endpoints/{ep}` | `subscriptions:write` | 同上 |
| `check.py` | 全部 | — | **不动**（check 是全平台流水线，行级过滤无意义） |

### 7.1 GET `/messages` 过滤实现

```python
@router.get("/messages", response_model=MessageListResponse)
async def list_messages(
    request: Request,
    since: str | None = Query(None),
    ...
    platform: str | None = Query(None),  # 用户主动过滤
    ...
    filt: TokenResourceFilter = Security(get_resource_filter, scopes=["messages:read"]),
) -> MessageListResponse:
    ...
    matched = store.query_messages(since=since_ts, title=title, author=author, platform=platform, phase=phase_enum)
    # 行级过滤：在用户主动过滤结果之上叠加
    matched = [m for m in matched if filt.allows_message(m)]
    return MessageListResponse(messages=[_record_to_out(m) for m in matched], count=len(matched))
```

用户主动 `?platform=bili` 与 token 规则 AND（自动叠加）。

### 7.2 GET `/messages/{msg_id}` 越权 → 404

```python
rec = store.get_message(msg_id)
if rec is None or not filt.allows_message(rec):
    raise HTTPException(status_code=404, detail="message not found")
```

注意：**不区分**「不存在」和「存在但越权」—— 两者都 404，不暴露存在性
（spec §1 隐私原则）。

### 7.3 GET `/subscriptions` 过滤实现

`list_subscriptions(platform=None, ...)` 返回 `dict[section, list[dict]]`，
section 是 TOML section 名（`bilibili` / `xiaohongshu` / `weibo`），不是
short name。过滤层要做映射：

```python
SECTION_TO_SHORT = {"bilibili": "bili", "xiaohongshu": "xhs", "weibo": "weibo"}
SHORT_TO_KEY_FIELD = {"bili": "uid", "xhs": "user_id", "weibo": "user_id"}

def _filter_subs(result, filt):
    out = {}
    for section, subs in result.items():
        short = SECTION_TO_SHORT[section]
        if not filt.allows_platform(short):
            continue
        if filt.subscription_refs is None:
            out[section] = subs
            continue
        key_field = SHORT_TO_KEY_FIELD[short]
        kept = []
        for s in subs:
            sub_id = str(s.get(key_field, ""))
            if f"{short}:{sub_id}" in filt.subscription_refs:
                kept.append(s)
        if kept:
            out[section] = kept
    return out
```

## 8. 写入路由一致性

### 8.1 `POST /messages/rerun` 越权 → 404

现状（`messages.py:211`）：`existing = [m for m in (...)]` 拿到存在的消息，
`reset_count = len(existing)`，`== 0` → 404。

行级过滤叠加：

```python
existing = [
    m for m in (store.get_message(mid) for mid in body.msg_ids)
    if m is not None and filt.allows_message(m)
]
reset_count = len(existing)
if reset_count == 0:
    raise HTTPException(status_code=404, detail="message not found")
```

**部分越权语义**：用户传 5 个 msg_id，3 个越权 2 个合法 → 当前实现会跑
合法的 2 个（reset_count=2）。**这是预期的**：rerun 是「尽力而为」操作，
越权 id 被静默忽略（与 GET 过滤语义一致），只在**全部**越权时报 404。

如果未来需要「越权一个就 404」的严格模式，挂新 issue。本 spec 选宽松模式：
避免「误传一个越权 id 就让整个 rerun 失败」的运维体验。

### 8.2 `POST /messages/fetch` 按 platform 前缀过滤

fetch 处理外部 id（如 `bili:BV1xx`），消息可能还没入库（不存在于 store）。
因此**不能用** `filt.allows_message`（rec 不存在）。

策略：按 msg_id 前缀（即 platform short）过滤：

```python
allowed_ids = [mid for mid in body.msg_ids if _msg_id_platform_allowed(mid, filt)]
if not allowed_ids:
    raise HTTPException(status_code=404, detail="message not found")
# 用 allowed_ids 替换 body.msg_ids 走后续流程
```

其中 `_msg_id_platform_allowed`：

```python
def _msg_id_platform_allowed(msg_id: str, filt: TokenResourceFilter) -> bool:
    """msg_id 形如 'bili:BV1xx'，按前缀判 platform 可见性。"""
    short = msg_id.split(":", 1)[0] if ":" in msg_id else ""
    return filt.allows_platform(short)
```

**注意**：fetch 阶段无法做 subscription_ref 维度过滤（消息还没入库，detector
还没注入 subscription_ref），所以 fetch 的行级过滤**只看 platform 维度**。
这是已知限制，记录在 §11 风险表。

### 8.3 `DELETE /subscriptions/{p}/{id}` 越权 → 200 + `success=False`

现有 `remove_subscription` 返回 `(False, "未找到: ...")` 是业务正常响应（200）。
越权处理与「未找到」语义合并 —— 不暴露存在性：

```python
if not filt.allows_subscription(platform_short, identifier):
    return SubscriptionRemoveResponse(success=False, message="未找到: 订阅不存在或无权访问")
```

或者更简洁：让 `remove_subscription` 在底层判断，但这会污染业务层。本 spec 选
路由层判断（与 spec §3 「不进 MessageStore / 业务层」一致）。

### 8.4 `POST /subscriptions` 不过滤

写入是「创建新订阅」，新订阅 id 是调用方提供的，不存在「越权」概念 ——
只要 token 有 `subscriptions:write` scope，就能创建任意平台的订阅。

**潜在风险**：管理员用 `bili-only` token 创建 xhs 订阅。缓解：管理员在创建
token 时应该明确「这个 token 只管 bili」，但 PR 不强制 —— 创建订阅本身需要
`subscriptions:write` scope，已有最小权限保护。如果未来需要按 `platforms`
字段限制写入平台，挂新 issue。

## 9. CLI 扩展

### 9.1 `token create` 加 multi-flag

```bash
# 全权限（默认）
python -m api.token_tool create bot-1

# 仅 bili 平台
python -m api.token_tool create bili-bot \
    --scope messages:read \
    --resource-platform bili

# 仅 bili uid=100 / uid=200
python -m api.token_tool create up-bot \
    --scope messages:read \
    --resource-sub bili:100 --resource-sub bili:200

# bili 平台 + 指定订阅（AND 组合）
python -m api.token_tool create strict-bot \
    --scope messages:read \
    --resource-platform bili \
    --resource-sub bili:100
```

Click 写法：

```python
@click.option(
    "--resource-platform",
    "resource_platforms",
    multiple=True,
    help="限制 token 可访问平台（可多次：--resource-platform bili --resource-platform xhs）。"
    "合法值: bili, xhs, weibo。不指定 = 不限平台。",
)
@click.option(
    "--resource-sub",
    "resource_subs",
    multiple=True,
    help="限制 token 可访问订阅（复合 key <platform_short>:<id>，可多次）。"
    "如 --resource-sub bili:100 --resource-sub xhs:u456。不指定 = 不限订阅。",
)
def create(name, force, scopes, resource_platforms, resource_subs):
    ...
```

**校验**：

- `--resource-platform bili` 必须是 `{bili, xhs, weibo}` 之一，否则退出码非 0
- `--resource-sub xxx:yyy` 必须形如 `<short>:<id>`，且 `short` 合法，否则退出码非 0
- 不校验订阅实际存在（CLI 创建时订阅可能尚未创建）

### 9.2 `token list` 显示规则

```
API Tokens
 Name         Hash (前 8)   Scopes              Resource Rules
 bot-1        a1b2c3d4      (unrestricted)      (unrestricted)
 bili-bot     e5f6a7b8      messages:read       platforms=[bili]
 up-bot       9c8d7e6f      messages:read       subs=[bili:100, bili:200]
 strict-bot   1a2b3c4d      messages:read       platforms=[bili] subs=[bili:100]
```

`ResourceRules()` 默认（两字段 None）→ 显示 `(unrestricted)`。

### 9.3 CLI 不受规则约束

`api/token_tool.py` 是本地管理员 CLI，不通过 HTTP、不通过 `get_resource_filter`。
理由与 #103 §8.3 一致 —— 「能跑 CLI = 能 vim auth.toml = 是管理员」。

## 10. 测试策略

### 10.1 新增 fixture：`row_filtered_client`

与 #103 的 `scoped_client` 解耦，独立 fixture 控制行级规则：

```python
@pytest.fixture
async def row_filtered_client(
    tmp_path, monkeypatch, request
) -> AsyncClient:
    """带指定 resource_rules 的 client。

    request.param 是 dict，形如
        {"scopes": ["messages:read"], "platforms": ["bili"], "subscription_refs": ["bili:100"]}
    缺省字段 = None（不限）。
    """
    params = request.param
    scopes = params.get("scopes", [])
    platforms = params.get("platforms")
    subs = params.get("subscription_refs")
    ...
    plain = create_token(
        "row-bot",
        scopes=scopes,
        resource_rules=ResourceRules(platforms=platforms, subscription_refs=subs),
    )
    ...
```

`create_token` 需要扩展接受 `resource_rules` 参数（plan Task 1）。

### 10.2 单元测试（`tests/test_resource_filter.py`）

新增 `TokenResourceFilter` 的纯逻辑测试：

- `from_token(token_with_no_rules)` → `unrestricted()`
- `allows_platform("xhs")` 当 `platforms=["bili"]` → False
- `allows_subscription("bili", "100")` 当 `subscription_refs=["bili:100"]` → True
- `allows_subscription("bili", "200")` 当 `subscription_refs=["bili:100"]` → False
- `allows_message(msg_bili_100)` 当 `platforms=["bili"], subs=["bili:100"]` → True
- `allows_message(msg_bili_200)` 当 `platforms=["bili"], subs=["bili:100"]` → False
- `allows_message(msg_xhs_u456)` 当 `platforms=["bili"]` → False（跨平台）
- `platforms=[]` → 任何平台都 False（拒绝一切）
- `subscription_refs=[]` → 任何订阅都 False

### 10.3 集成测试：GET 路由行级过滤

每条路由加 `test_*_row_filtered_*`：

- **list_messages**：token 只允许 bili，response 只含 bili 消息
- **list_messages**：token 允许 `bili:100`，response 只含 uid=100 的消息
- **get_message**：token 不允许该 msg 的平台 → 404（不暴露存在性）
- **get_message**：token 不允许该订阅 → 404
- **list_subs**：token 只允许 bili → response 只含 bilibili section

### 10.4 集成测试：写入路由越权 → 404 / success=False

- **rerun**：全部 msg_id 越权 → 404
- **rerun**：部分越权 → 202，越权 id 被静默忽略（reset_count 不含越权）
- **fetch**：msg_id 前缀平台被禁 → 404
- **delete_sub**：越权 → 200 + `success=False`
- **bind/unbind endpoint**：越权 → 200 + `success=False`

### 10.5 兼容性测试

- 老 auth.toml（无 `resource_rules` 字段）→ 加载后 `resource_rules == ResourceRules()`
  → 全权限 → 现有所有测试无需改动继续通过
- 老 token + 新路由 → 行级过滤返回 `unrestricted()`，所有消息可见

### 10.6 CLI 测试

- `create bot --resource-platform bili` → auth.toml 落盘 `resource_rules.platforms = ["bili"]`
- `create bot --resource-sub bili:100 --resource-sub xhs:u456` → 落盘 subscription_refs
- `create bot --resource-platform invalid` → 退出码非 0
- `create bot --resource-sub invalid_format` → 退出码非 0
- `list` 输出含 Resource Rules 列

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| `subscription_refs=[]`（空 list）= 拒绝一切，与 `scopes=[]` = 全权限语义冲突引起误解 | docstring + CLI 提示明确「None = 不限，[] = 拒绝」；§5.3 单独章节说明 |
| `platforms=["bili"], subs=["xhs:u456"]` 这种无意义组合静默拒绝一切 | CLI 创建时检查并 warning（不强制拒绝） |
| fetch 路由无法做 subscription_ref 维度过滤（消息未入库） | §8.2 已记录；只做 platform 维度过滤，subscription_ref 维度在消息入库后由 rerun 路径补 |
| 路由层手写过滤散落各处，维护成本 | `TokenResourceFilter` 抽象集中逻辑；路由只调 `allows_*` 三方法 |
| `get_resource_filter` 内联 `require_scopes` 逻辑导致重复 | 抽 `_authenticate_and_check_scope` 私有 helper（plan Task 3 决定） |
| 老 token + 新路由静默获得全权限，行级过滤形同虚设 | 与 #103 「空 scope = 全权限」过渡期策略一致；CLI 创建时主动提示 |
| tomlkit 嵌套 table 在 `save_auth_config` 写出格式异常（空 table / 字段顺序） | plan Task 1 加单元测试覆盖 4 种 ResourceRules 形态的 round-trip |
| OpenAPI docs 在新写法下可能不渲染 scope（FastAPI Security 嵌套依赖差异） | 集成测试加 `test_openapi_docs_include_scopes` 不变即可验证 |
| 部分越权 rerun 静默忽略越权 id，调用方可能误以为全部成功 | response 的 `reset_count` 已是实际处理的数量；运维需自己对比 |

## 12. 验证清单

实现完成后必须通过：

```bash
uv run ruff check .
uv run pyright
uv run pytest -x tests/test_resource_filter.py -v
uv run pytest -x tests/test_api_auth.py -v
uv run pytest -x tests/test_api_token_tool.py -v
uv run pytest -x tests/test_api_messages.py -v
uv run pytest -x tests/test_api_subscriptions.py -v
uv run pytest -x tests/test_api_fetch.py -v
uv run pytest -x                                     # 全量回归
```

手动验证：

```bash
# 1. 创建 bili-only token
python -m api.token_tool create bili-bot \
    --scope messages:read --resource-platform bili

# 2. 创建订阅限定 token
python -m api.token_tool create up-bot \
    --scope messages:read --resource-sub bili:100

# 3. list 应显示 resource rules
python -m api.token_tool list

# 4. 用 bili-bot 拉消息 → 只返回 bili 平台
curl http://localhost:8000/api/v1/messages \
    -H "Authorization: Bearer <bili-bot-token>"

# 5. 用 up-bot 拉消息 → 只返回 uid=100 的消息
curl http://localhost:8000/api/v1/messages \
    -H "Authorization: Bearer <up-bot-token>"

# 6. 用 up-bot 拉订阅 → 只返回 bili uid=100
curl http://localhost:8000/api/v1/subscriptions \
    -H "Authorization: Bearer <up-bot-token>"

# 7. 越权 rerun → 部分越权被忽略
curl -X POST http://localhost:8000/api/v1/messages/rerun \
    -H "Authorization: Bearer <bili-bot-token>" \
    -H "Content-Type: application/json" \
    -d '{"msg_ids": ["bili:BV1xx", "xhs:note1"], "from_phase": "detected"}'
# 预期 202 + reset_count=1（xhs:note1 被静默过滤）

# 8. 全部越权 rerun → 404
curl -X POST http://localhost:8000/api/v1/messages/rerun \
    -H "Authorization: Bearer <bili-bot-token>" \
    -H "Content-Type: application/json" \
    -d '{"msg_ids": ["xhs:note1", "xhs:note2"], "from_phase": "detected"}'
# 预期 404 + {"detail": "message not found"}
```

## 13. 未来工作（非本 PR）

挂独立 issue：

- **Web UI token 管理**：与 #103 Web UI 同期做，可视化勾选 platform /
  subscription_refs
- **endpoint 行级绑定**：让 token 只能看到/操作特定 endpoint
- **列级权限**：token 只能看 msg.title 不能看 msg.body
- **通配符规则**：`bili:*` 表示全部 bili 订阅（当前用 `platforms=["bili"]`
  已够）
- **行级审计日志**：记录每个 token 访问过哪些行
- **fetch 路由 subscription_ref 维度过滤**：需要 fetch 后 detector 注入完
  subscription_ref 再二次校验，复杂度高
