# 订阅通知端点 API 设计

- 日期: 2026-07-06
- 状态: Draft (待用户 review)
- 范围: `core/subscription_cli.py`、`web/routes/subscriptions.py`、`api/schemas.py`、`api/routes/subscriptions.py`、`tests/test_subscription_cli.py`

## 1. 背景

当前订阅管理存在两套通知端点（notify_endpoints）写入路径：

- **Web 端**：`web/routes/subscriptions.py:101-194` 已经实现 `POST /subscriptions/{p}/{id}/endpoints/add` 与 `remove`，但逻辑写死在 Web 路由层，未与 core 共享。
- **API 端（`/api/`）**：只暴露 add/remove 订阅本身（`POST /subscriptions`、`DELETE /subscriptions/{p}/{id}`），无法事后调整订阅的通知端点。bot 想换 gotify 通道只能调用 Web Form 接口（会被 CSRF 中间件拦截），或删了重加。

订阅条目的 schema 已就绪（`shared/config.py` 中 `BiliSubscription.notify_endpoints: list[str]` / `UserSubscription.notify_endpoints: list[str]`），TOML 落盘字段 `notify_endpoints = ["xxx"]` 也已被配置加载器识别。**缺的是 API 层暴露面和共享的 core 业务逻辑。**

## 2. 目标

1. 把"绑定/解绑 endpoint 到订阅"的业务逻辑抽到 `core/subscription_cli.py`，Web/API 共享同一份实现，杜绝逻辑漂移。
2. API 层补全 endpoint 管理端点：`POST /subscriptions/{p}/{id}/endpoints`、`DELETE /subscriptions/{p}/{id}/endpoints/{endpoint_name}`，与现有 add/remove 订阅端点对称。
3. 保留 `default_notify_endpoint` 语法糖：`POST /subscriptions` 一次调用同时完成订阅添加和默认 endpoint 绑定，方便 bot 单次调用。底层复用同一 core 函数。
4. endpoint 存在性校验在 core 层做，防止拼写错误造成静默丢推送。

## 3. 非目标

- 不引入 `[api].default_notify_endpoint` 配置兜底（YAGNI，bot 自己传更清晰）。
- 不改 `shared/config.py`（schema 已支持）。
- 不重构 Web 端的 toast_key 重定向语义（保留 UX）。
- 不动现有 `add_subscription` / `remove_subscription` 已有调用方的行为（新增参数有默认值，向后兼容）。

## 4. 设计

### 4.1 文件改动总览

```
core/subscription_cli.py
  + add_endpoint_to_subscription(...)
  + remove_endpoint_from_subscription(...)
  ~ add_subscription(...) 加可选参数 default_notify_endpoint

web/routes/subscriptions.py
  ~ subscription_endpoint_add/remove 改成调 core 函数（保留 toast_key 重定向）

api/schemas.py
  ~ SubscriptionAddRequest 加 default_notify_endpoint: str | None = None
  + EndpointBindRequest { endpoint_name: str }

api/routes/subscriptions.py
  + POST   /subscriptions/{platform}/{identifier}/endpoints
  + DELETE /subscriptions/{platform}/{identifier}/endpoints/{endpoint_name}
  ~ add_sub 透传 default_notify_endpoint

web/templates/base.html
  ~ TOAST_KEY_MAP 加 'subscription.endpoint_unknown': '通知端点不存在'

tests/test_subscription_cli.py
  + add/remove endpoint 用例 + add_subscription 语法糖用例（含回滚）
```

**总改动**：6 源文件 + 1 测试文件。`web/routes/subscriptions.py` 为重构（净行数减少约 60 行）。

### 4.2 core 层：新增函数签名

```python
async def add_endpoint_to_subscription(
    platform: str,
    identifier: int | str,
    endpoint_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """绑定 endpoint 到订阅。

    校验 endpoint_name 在 config.toml [[endpoints]] 中存在。
    返回值:
      (True, "已绑定: {endpoint_name}")     # 成功或已存在（幂等）
      (False, "未找到订阅")
      (False, "未知 endpoint: {endpoint_name}")
    """

async def remove_endpoint_from_subscription(
    platform: str,
    identifier: int | str,
    endpoint_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """解绑 endpoint。

    返回值:
      (True, "已解绑: {endpoint_name}")     # 成功或本来就没有（幂等）
      (False, "未找到订阅")
    """
```

**实现要点**：

- 复用 `add_subscription` 内的 `_load_doc` / `_key_value` / `PLATFORM_TO_SECTION` 帮助函数，保持风格一致。
- endpoint 存在性校验：调 `load_config("config/config.toml")`，检查 `endpoint_name in {ep.name for ep in cfg.endpoints}`。`config.toml` 路径硬编码（与现有 Web 路由一致）。
- 写入逻辑沿用 `web/routes/subscriptions.py:101-148` 现有的 tomlkit 操作模式：定位订阅条目 → 读取 `notify_endpoints` 列表（缺失视为 `[]`）→ 增删 → 写回。
- 幂等性：`add` 时 endpoint 已在列表中，直接返回 `True`；`remove` 时 endpoint 不在列表，也返回 `True`。

### 4.3 core 层：`add_subscription` 扩展

```python
async def add_subscription(
    platform: str,
    identifier: int | str,
    name: str,
    path: str = "config/subscriptions.toml",
    default_notify_endpoint: str | None = None,   # 新增
) -> tuple[bool, str]:
```

**行为**：

1. 沿用现有逻辑写入订阅条目（`new_entry["name"] = name` 后 `arr.append(new_entry)`）。
2. 如果 `default_notify_endpoint` 非 None：
   - 先 `p.write_text(...)` 落盘订阅条目（让 core 函数能找到它）。
   - 调用 `add_endpoint_to_subscription(platform, identifier, default_notify_endpoint, path)`。
   - 绑定失败（如 endpoint 不存在）→ **回滚**：删除刚加的条目，重写文件，返回 `(False, f"默认 endpoint 绑定失败: {msg}")`。
   - 绑定成功 → 返回 `(True, f"已添加: {name}")`。
3. `default_notify_endpoint` 为 None → 行为完全不变（向后兼容）。

**回滚的必要性**：避免半残数据（订阅已加但 endpoint 绑定失败，用户得手动清理）。回滚操作直接复用 `remove_subscription(platform, identifier, path)`。

### 4.4 api/schemas.py 扩展

```python
class SubscriptionAddRequest(BaseModel):
    """``POST /subscriptions`` 请求体。

    ``identifier`` 在 API 层统一为 str，``add_subscription`` 内部按平台转 int/str。
    ``default_notify_endpoint`` 可选，传入时会在添加订阅后绑定该 endpoint。
    """

    platform: str
    identifier: str
    name: str
    default_notify_endpoint: str | None = None


class EndpointBindRequest(BaseModel):
    """``POST /subscriptions/{p}/{id}/endpoints`` 请求体。"""

    endpoint_name: str
```

**响应模型复用**：`SubscriptionAddResponse { success: bool, message: str }`，不为 endpoint 端点新建 schema（YAGNI）。

### 4.5 api/routes/subscriptions.py 扩展

新增两个端点，与现有 `add_sub` / `remove_sub` 风格一致：

```python
@router.post(
    "/subscriptions/{platform}/{identifier}/endpoints",
    response_model=SubscriptionAddResponse,
)
async def bind_endpoint(
    platform: str,
    identifier: str,
    body: EndpointBindRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    """绑定 endpoint 到订阅。

    endpoint 不存在 / 订阅不存在 / 成功（含幂等）都返回 200 + success 字段。
    """
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
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    """解绑 endpoint。订阅不存在返回 success=False，其余（含幂等）返回 True。"""
    success, message = await remove_endpoint_from_subscription(
        platform, identifier, endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)
```

修改 `add_sub`：

```python
@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    success, message = await add_subscription(
        body.platform,
        body.identifier,
        body.name,
        default_notify_endpoint=body.default_notify_endpoint,
    )
    return SubscriptionAddResponse(success=success, message=message)
```

**路径参数约定**：`platform` 使用全名（`bilibili` / `xiaohongshu` / `weibo`），与现有 `DELETE /subscriptions/{platform}/{identifier}` 一致。`identifier` 在 API 层统一为 str，core 层按平台转 int/str。

**错误响应语义**：沿用现有风格，业务可恢复态（重复、未找到、未知 endpoint）都返回 200 + `success` 字段，不映射 4xx。

### 4.6 web/routes/subscriptions.py 重构

把 `subscription_endpoint_add` / `subscription_endpoint_remove` 函数体替换为调用 core 函数：

```python
@router.post("/subscriptions/{platform}/{identifier}/endpoints/add")
async def subscription_endpoint_add(
    platform: str,
    identifier: str,
    endpoint_name: str = Form(...),
) -> RedirectResponse:
    plat_name = _platform_key_to_name(platform)  # bili→bilibili 短名转换保留
    ok, _msg = await add_endpoint_to_subscription(plat_name, identifier, endpoint_name)
    if not ok and "未找到订阅" in _msg:
        toast_key = "subscription.not_found"
        t = "error"
    elif not ok:  # 未知 endpoint
        toast_key = "subscription.endpoint_unknown"
        t = "error"
    else:
        toast_key = "subscription.endpoint_added"
        t = "success"
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}", status_code=303
    )
```

**新增 toast_key**：`subscription.endpoint_unknown`（未知 endpoint 时的错误提示）。

**toast_key 翻译机制**：trawler **不用标准 i18n 框架**，文案写死在 `web/templates/base.html:128-139` 的前端 JS `TOAST_KEY_MAP` 字典里（key → 中文文案）。新增 toast_key **必须同步在 `TOAST_KEY_MAP` 加一行**，否则前端 fallback 显示空字符串：

```javascript
// web/templates/base.html:128-139 附近，TOAST_KEY_MAP 内追加：
'subscription.endpoint_unknown': '通知端点不存在',
```

**注意**：Web 路由路径仍是 `/subscriptions/{platform}/{identifier}/endpoints/add`（用 Web 短名 `bili/xhs/weibo`），与 API 路径 `/api/subscriptions/{platform}/...`（用全名）解耦。两条路径背后调用同一个 core 函数。

## 5. 数据流

### 5.1 bot 通过语法糖一次性 add+bind

```
bot → POST /api/subscriptions {platform, identifier, name, default_notify_endpoint}
       ↓
api/routes.add_sub → core.add_subscription(...)
                       ├─ 写订阅条目到 toml
                       ├─ default_notify_endpoint 非空:
                       │   ├─ core.add_endpoint_to_subscription(...)
                       │   │   ├─ load_config() 校验 endpoint 存在
                       │   │   ├─ 不存在 → 返回 (False, "未知 endpoint")
                       │   │   └─ 存在 → 写 notify_endpoints 到 toml
                       │   ├─ 绑定失败 → core.remove_subscription 回滚
                       │   └─ 返回 (True/False, msg)
                       └─ 返回 (True, "已添加: ...")
       ↓
返回 SubscriptionAddResponse
```

### 5.2 bot 事后调整 endpoint

```
bot → POST /api/subscriptions/{platform}/{id}/endpoints {endpoint_name}
       ↓
api/routes.bind_endpoint → core.add_endpoint_to_subscription(...)
                            ├─ load_config() 校验
                            ├─ 定位订阅条目
                            └─ 写 notify_endpoints
       ↓
返回 SubscriptionAddResponse
```

### 5.3 Web UI 绑定 endpoint

```
浏览器 → POST /subscriptions/{short}/{id}/endpoints/add (Form)
         ↓
web/routes.subscription_endpoint_add
  ├─ _platform_key_to_name 短名→全名
  ├─ core.add_endpoint_to_subscription(plat_name, ...)
  └─ 重定向 /subscriptions?toast_key=...
```

## 6. 错误处理

| 场景 | core 返回 | API 响应 | Web 重定向 |
|------|----------|---------|-----------|
| 绑定成功（首次/幂等） | `(True, "已绑定: xxx")` | 200 `success=True` | `endpoint_added` success |
| 未知 endpoint | `(False, "未知 endpoint: xxx")` | 200 `success=False` | `endpoint_unknown` error |
| 订阅不存在 | `(False, "未找到订阅")` | 200 `success=False` | `not_found` error |
| 解绑成功（首次/幂等） | `(True, "已解绑: xxx")` | 200 `success=True` | `endpoint_removed` success |
| 语法糖回滚 | `(False, "默认 endpoint 绑定失败: ...")` | 200 `success=False` | N/A（API only） |

## 7. 测试

### 7.1 core 单元测试（`tests/test_subscription_cli.py`）

沿用现有 tmp_path + toml fixture 模式：

```python
test_add_endpoint_to_subscription_ok              # 正常绑定
test_add_endpoint_to_subscription_idempotent      # 重复绑定返回 True
test_add_endpoint_to_subscription_unknown_ep      # 未知 endpoint 返回 False
test_add_endpoint_to_subscription_no_sub          # 订阅不存在返回 False
test_remove_endpoint_from_subscription_ok
test_remove_endpoint_from_subscription_idempotent # 不存在的 endpoint 也 True
test_remove_endpoint_from_subscription_no_sub
test_add_subscription_with_default_endpoint       # 语法糖 happy path
test_add_subscription_with_bad_default_endpoint   # endpoint 无效时回滚，订阅未加入
test_add_subscription_without_default_endpoint    # default_notify_endpoint=None 行为不变
```

`load_config` 在测试中需要 monkeypatch，注入预定义的 `cfg.endpoints`。

### 7.2 API 集成测试

在 `tests/test_api_subscriptions.py`（如存在，否则新建）追加：

```python
test_api_bind_endpoint_ok             # POST endpoints 200 success=True
test_api_bind_endpoint_unknown        # 未知 endpoint 200 success=False
test_api_bind_endpoint_no_sub         # 订阅不存在 200 success=False
test_api_unbind_endpoint_ok           # DELETE endpoints/{name} 200
test_api_add_subscription_with_default # 语法糖完整流程
```

## 8. 风险与权衡

| 风险 | 缓解 |
|------|------|
| `load_config` 是 async，core 函数签名也要 async | 已有先例，`add_subscription` 已是 async |
| tomlkit 写入时 inline_table vs table 格式差异 | 沿用 Web 路由现有做法，写入时统一转 list[str] |
| 语法糖回滚需要双次写盘 | 可接受，正确性优先；订阅添加是低频操作 |
| 新增 toast_key 漏加到 `TOAST_KEY_MAP` | spec §4.6 已明确要求同步改 `web/templates/base.html`，验证清单加 grep 检查 |

## 9. 验证清单

实现完成后必须通过：

```bash
uv run ruff check .
uv run pyright
uv run pytest -x tests/test_subscription_cli.py
uv run pytest -x tests/test_api_subscriptions.py  # 如存在
uv run pytest -x                                   # 全量回归
```

手动验证：

```bash
# 启动 API
uv run trawler serve  # 或类似入口

# 1. 语法糖 add+bind
curl -X POST http://localhost:8000/api/v1/subscriptions \
  -H "Authorization: Bearer xxx" \
  -H "Content-Type: application/json" \
  -d '{"platform":"bilibili","identifier":"123","name":"UP","default_notify_endpoint":"gotify-main"}'

# 2. 事后绑定
curl -X POST http://localhost:8000/api/v1/subscriptions/bilibili/123/endpoints \
  -H "Authorization: Bearer xxx" \
  -H "Content-Type: application/json" \
  -d '{"endpoint_name":"gotify-backup"}'

# 3. 解绑
curl -X DELETE http://localhost:8000/api/v1/subscriptions/bilibili/123/endpoints/gotify-main \
  -H "Authorization: Bearer xxx"

# 4. 检查 toml 落盘
cat config/subscriptions.toml

# 5. 确认 toast_key 已加到前端字典
grep 'subscription.endpoint_unknown' web/templates/base.html
```
