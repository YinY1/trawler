# XHS Auth 迁移到 ReaJason/xhs 库 — 异步包装方案

| 项 | 值 |
|---|---|
| 日期 | 2026-06-26 |
| 状态 | Approved (用户已确认全部设计决策) |
| 作者 | brainstorming session |
| 相关文件 | `platforms/xiaohongshu/auth.py`, `platforms/xiaohongshu/client.py`, `web/routes/auth.py` |

---

## 1. 背景与问题

### 1.1 当前 bug

XHS QR 扫码登录失败。手机扫码后，服务端返回 HTTP 471（captcha challenge / 风控），前端永远显示 waiting。

### 1.2 根因分析

经真机日志诊断 + ReaJason/xhs 上游对比，确认三个独立 bug：

1. **字段名错误**：`poll_qr_status` 读 `codeStatus`（camelCase），真实字段是 `code_status`（snake_case）。导致状态永远读不到。
2. **缺 gid cookie**：生产代码完全不生成 `gid` / `gid.sign`，只被动等服务端下发（鸡生蛋问题）。这两个是反爬核心 cookie。
3. **fetch_sec_cookies 是瞎猜的**：`/api/sec/v1/scripting` 和 `/api/sec/v1/shield/webprofile` 的 payload 字段名是开发者推测，`profileData=""` 几乎肯定错，线上两个 API 都被服务端拒，sec cookies 永远拿不到。

### 1.3 为什么不补丁而重构

- 项目 `pyproject.toml` 早已声明 `xhs>=0.1.9`（ReaJason 的完整 HTTP 客户端库），但 PR #13 自己用 aiohttp 重写了 HTTP 层，引入了上述所有 bug。
- ReaJason/xhs 是活跃维护、真实抓包、用户验证过的实现。它的 `update_session_cookies_from_cookie` 自动补 `gid` + `gid.sign`，内置 `sign()` 算法和项目现有 `signer.py` 底层都是同一个 xhshow 库。
- 最小补丁（硬编码 gid）治标不治本，sec API 那部分仍是瞎猜，长期还会被风控。

---

## 2. 设计决策（用户已拍板）

| 决策点 | 选择 | 理由 |
|---|---|---|
| 重构范围 | **只包 auth**，monitor/comments/search/downloader 不动 | 只有 auth 坏，其他 4 个模块能跑，最小改动 |
| 签名策略 | **用 xhs 库内置 `sign()`** | 底层同 xhshow，上游已验证 auth 全流程 |
| 异常处理 | **转译到项目现有异常体系** | `web/routes/auth.py` 依赖 `CaptchaError` 等，无感知 |
| 包装位置 | **新建 `async_xhs_wrapper.py`** | 不和 `client.py`（aiohttp）混 |
| auth.py 改动粒度 | **重写 `XhsAuthenticator`** | 现有辅助方法大部分作废 |
| sec_poison_id / websectiga | **不管** | xhs 库不管但 example 能跑通，证明非必需 |
| a1 生成 | **保留 `generate_a1()`** | 避免 xhs 库硬编码 fallback a1 |
| get_user_nickname | **用 xhs 库 `get_self_info()`** | 替代现有 aiohttp 调用 |

---

## 3. 架构

### 3.1 模块边界

```
┌──────────────────────────────────────────────────────────────────┐
│  Web 层 (不动)                                                    │
│  web/routes/auth.py ─────► BaseAuthenticator (协议, 不动)         │
└──────────────────────────┬───────────────────────────────────────┘
                           │ 依赖 XhsAuthenticator 实现
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  Platform 层 — xiaohongshu                                        │
│  platforms/xiaohongshu/auth.py                                    │
│    └─ class XhsAuthenticator (重写)                                │
│         │ generate_qr_code / poll_qr_status / get_tokens          │
│         │ refresh_tokens / validate_tokens / get_user_nickname    │
│         │                                                         │
│         ▼  依赖                                                    │
│  platforms/xiaohongshu/async_xhs_wrapper.py (新建, ~80 行)        │
│    └─ class AsyncXhsClient                                         │
│         get_qrcode / check_qrcode / activate / get_self_info      │
│         cookie (property) / close                                  │
│         │  内部 asyncio.to_thread 包同步 xhs 库                    │
└─────────┼─────────────────────────────────────────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────────────┐
│  第三方库                                                          │
│  xhs (ReaJason) ─ XhsClient (同步, requests.Session)              │
│    update_session_cookies_from_cookie / sign / 真实抓包实现        │
└──────────────────────────────────────────────────────────────────┘

          ───────── 不动的模块 ─────────
          client.py        (aiohttp HTTP 层, monitor/comments/search 用)
          signer.py        (xhshow sign, monitor 用)
          monitor.py       comments.py   search.py   downloader.py
```

### 3.2 关键原则

- `async_xhs_wrapper.py` 只暴露 auth 需要的 6 个方法（`get_qrcode` / `check_qrcode` / `activate` / `get_self_info` / `cookie` / `close`），内部用 `asyncio.to_thread` 包 xhs 库。**不暴露 `requests.Session`**。
- `auth.py` 保留 `BaseAuthenticator` 协议（`generate_qr_code` / `poll_qr_status` / `get_tokens` / `refresh_tokens` / `validate_tokens` / `get_user_nickname`）。删掉 `_ensure_client` / `fetch_sec_cookies` 调用链。
- `client.py` 完全不动。`signer.py` 完全不动。`web/routes/auth.py` 完全不动。

---

## 4. QR 登录数据流

### 4.1 `generate_qr_code()`

```
generate_qr_code()
  │
  ├─ a1 = generate_a1()              # 保留项目现有算法
  ├─ web_id = generate_web_id(a1)    # = MD5(a1)
  ├─ init_cookie = f"a1={a1};webId={web_id}"
  ├─ client = AsyncXhsClient(cookie=init_cookie)
  │    └─ xhs 库 __init__ 内自动补 gid + gid.sign
  ├─ qr = await client.get_qrcode()  # 返回 {qr_id, code, url, multi_flag}
  ├─ 缓存 client + qr_id + code 到实例
  └─ return QRCodeResult(qr_url=qr["url"], qr_key=qr["qr_id"], expires_in=180)
```

### 4.2 `poll_qr_status(qr_key)`

```
poll_qr_status(qr_key)
  │
  ├─ result = await client.check_qrcode(qr_id, code)
  ├─ 字段名 code_status (snake_case!)    # ← 关键修复点
  ├─ code_st = result.get("code_status", 0)
  └─ 2 → SUCCESS
     1 → SCANNED
     3 → EXPIRED
     其他 → WAITING
```

### 4.3 `get_tokens(qr_key)` — SUCCESS 后

```
get_tokens(qr_key)
  │
  ├─ await client.activate()              # 拿到 web_session 写入 cookie jar
  ├─ full_cookie_str = client.cookie
  ├─ cookie_dict = parse_cookie_str(full_cookie_str)
  └─ return PlatformTokens(platform="xhs", cookies=cookie_dict, ...)
```

### 4.4 `get_user_nickname(tokens)`

```
get_user_nickname(tokens)
  │
  ├─ client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
  ├─ info = await client.get_self_info()
  └─ return info.get("nickname")
```

### 4.5 `refresh_tokens` / `validate_tokens`

- 简化：都用 `AsyncXhsClient`，设 cookie 后访问 `get_self_info` 做 probe。
- xhs 库没有 refresh 概念，cookie 过期只能重登录。`refresh_tokens` 实际等价于 `validate`（成功返回原 tokens，失败抛异常）。

---

## 5. 异常转译策略

### 5.1 装饰器

在 `auth.py` 内定义 `_wrap_xhs_call` 装饰器（**不新建文件**，私有辅助），把 xhs 库异常转译到项目现有体系。

### 5.2 异常映射表

| xhs 库异常 | 项目异常 |
|---|---|
| `NeedVerifyError` | `CaptchaError` |
| `IPBlockError` | `IpBlockError` |
| `SignError` | `RetryableError` |
| `DataFetchError` | `DataError` |
| `RequestException` | `RetryableError` |

### 5.3 应用位置

- **装饰器用于**：`generate_qr_code` / `get_tokens` / `refresh_tokens` / `validate_tokens`（调用方会 except 并报错）
- **try/except 兜底**：`poll_qr_status`（返回 WAITING），`get_user_nickname`（返回 None）

---

## 6. 测试策略

### 6.1 现有测试处理

| 测试文件 | 处理方式 |
|---|---|
| `test_xhs_signer.py` (166 行, 真绿) | 不动 |
| `test_xhs_client.py` | 不动 `client.py` 测试。删掉和 auth 相关的 `check_qrcode_status` 测试字段名（已废） |
| `test_xhs_authenticator.py` (248 行, 大量假绿) | 整体重写 |

### 6.2 重写 `test_xhs_authenticator.py`

原则：**测编排，不测库**。

- `TestGenerateQrCode`：mock `AsyncXhsClient.get_qrcode`，断言 `QRCodeResult` 字段、断言 `generate_a1` 被调用并传给 client。
- `TestPollQrStatus`：mock `check_qrcode` 返回 `{code_status: 2}`，断言 SUCCESS；测 0/1/3；测缺失字段默认 WAITING。
- `TestGetTokens`：mock `activate` 成功，mock `client.cookie` 返回 `"a1=..;web_session=.."`，断言 `PlatformTokens.cookies` 含 `web_session`。
- `TestExceptionTranslation`（新）：mock 抛 `NeedVerifyError` → `CaptchaError`，`IPBlock` → `IpBlock` 等。
- `TestGetUserNickname`：mock `get_self_info` 返回 `{nickname:"xx"}`，mock 抛异常 → 返回 None。

### 6.3 新增 `test_async_xhs_wrapper.py`

- 不测 xhs 库内部。
- 只测 `asyncio.to_thread` 包装正确性：mock `xhs.XhsClient` 类，断言 wrapper 调对应方法、返回值透传、异常抛出。
- 6 个测试，每个方法一个。

### 6.4 不做的测试

- 不真连 XHS 服务器。
- 不 mock xhs 库内部 HTTP 层。
- 不复现"代码和 mock 用同一字段名"的循环自证（PR #40 的教训）。

---

## 7. 依赖与配置

### 7.1 依赖

- `pyproject.toml`：把 `xhs` 从 `[xhs]` optional extra 移到核心 `dependencies`。
- 装 `xhs==0.2.13` → 拉纯 Python 的 `requests` + `urllib3` + `charset-normalizer`（实测不依赖 lxml，无 C 扩展）。
- `requests` 和 `aiohttp` 并存，不冲突。

### 7.2 配置

无变更。`shared/config.py` 的 `xiaohongshu.auth.cookie` 字段语义不变。

### 7.3 回滚

`git revert` 这一个 PR 即可。`client.py` 没动，monitor 不受影响。

---

## 8. 实现顺序（给 writing-plans 的输入）

| Phase | 内容 |
|---|---|
| Phase 1 | 装 xhs 依赖 + 建 `async_xhs_wrapper.py` + `test_async_xhs_wrapper.py` |
| Phase 2 | 重写 `auth.py` `XhsAuthenticator` + 重写 `test_xhs_authenticator.py` |
| Phase 3 | 删 `client.py` 里 auth 专用的废方法（`create_qrcode` / `check_qrcode_status` / `fetch_sec_cookies`）及对应测试 |
| Phase 4 | 全量验证（`ruff` / `pyright` / `pytest` 509 全过）+ 真机扫码验证 |

---

## 9. 风险与限制

- **a1 生命周期**：xhs 库会在 a1 缺失时用硬编码 fallback。我们主动传 `generate_a1()` 生成的 a1 规避。但所有用户都用同一算法生成的 a1，模式可能被识别。短期可接受。
- **signer.py 与内置 sign 并存**：auth 走内置 sign，monitor 走 `signer.py`。两者算法一致（xhshow），短期可接受。长期想统一再说。
- **refresh_tokens 语义弱化**：xhs 库没 refresh 概念，`refresh_tokens` 实际退化成 validate-only。
- **真机验证依赖**：单元测试不能完全证明风控不再触发，必须 Phase 4 真机扫码确认。
