# Plan: 修复 XHS 扫码登录回归 — qrcode/create payload 字段类型错误

**日期**: 2026-06-25
**范围**: `platforms/xiaohongshu/client.py`、`tests/test_xhs_client.py`、`tests/test_xhs_authenticator.py`
**作者**: @oracle (writing-plans)
**状态**: draft（已含调研证据，待 review）
**分支**: `fix/xhs-qrtype`（基于 master）

---

## 1. 背景

### 1.1 Bug 现场

用户在 web 登录管理页注销 xhs 后重新扫码，立即报错（**二维码还没生成**，不影响已登录账号）：

```
00:32:11 [INFO] web.routes.auth: 🔑 xhs 生成二维码...
00:32:11 [INFO] trawler.xiaohongshu.auth: 🔑 XhsAuthenticator 生成二维码...
00:32:12 [ERROR] web.app: 💥 未处理异常: GET /auth/qr/xhs — HTTP 400

shared.exceptions.DataError: HTTP 400: {
  "code":-1,
  "success":false,
  "msg":"parse: put \"qr_login\" to field qrType, err: strconv.ParseInt: parsing \"qr_login\": invalid syntax",
  "data":null
}
```

### 1.2 调用链（已确认）

```
web/routes/auth.py:243  auth_qr → auth.generate_qr_code()
platforms/xhs/auth.py:115  generate_qr_code → client.create_qrcode(init_cookies)
platforms/xhs/client.py:339  create_qrcode → self._request("POST", "/api/sns/web/v1/login/qrcode/create", json=payload)
platforms/xiaohongshu/client.py:203  _request → raise DataError(...)
web/app.py:201  unhandled_exception_handler → 500 {"detail":"内部错误"}
```

注：bug 现场日志里看到的 "HTTP 400" 是 `logger.exception` 拼接的 `exc` 文本（来自 DataError 的 message），不是 HTTP 响应码。实际 HTTP 响应是 500（`unhandled_exception_handler` 固定返回 500）。

### 1.3 根因

`platforms/xiaohongshu/client.py:334-339` 的 payload：

```python
payload = {
    "qr_type": "qr_login",   # ← 服务端要 int，收到 "qr_login" → ParseInt 失败
    "qr_style": "default",
    "scene": "login",
}
```

服务端 Go 代码：`strconv.ParseInt("qr_login")` 失败 → 返回 400 + `parse: put "qr_login" to field qrType, err: ...`。

**为什么测试没拦住**：`tests/test_xhs_client.py:272-281` 的 `test_create_qrcode` 只 mock 返回值并断言 `result["qr_id"]`，**完全没断言 payload 内容**。`tests/test_xhs_authenticator.py:36-76` 也只 mock `create_qrcode` 的返回值/副作用，不触及 payload。所以 payload 字段是否正确，测试零覆盖。

---

## 2. 调研证据

### 2.1 仓库内自证（最强证据）

`docs/superpowers/plans/2026-06-13-xhs-qr-login-phase-3.md:811`（项目 Phase-3 QR 登录调研 plan，作者 @explorer，基于真实抓包）：

```python
# Step 3: Generate QR code
api = XHS_QR_CREATE_API
data = {"qr_type": 1}                  # ← 整数 1
sign = get_xhs_sign(api, data, ...)
```

→ **Phase-3 调研时抓到的真实 XHS web 端 payload 就是 `{"qr_type": 1}`，且只有这一个字段。**

### 2.2 外部权威库交叉验证

**ReaJason/xhs**（业界最常用的 XHS Python SDK，`xhs/core.py`）：

```python
def get_qrcode(self):
    uri = "/api/sns/web/v1/login/qrcode/create"
    data = {}
    return self.post(uri, data)
```

→ 服务端接受 **空 payload**，`qr_type` 完全可省略。

### 2.3 服务端报错自证

```
parse: put "qr_login" to field qrType, err: strconv.ParseInt: parsing "qr_login": invalid syntax
```

Go 的 `strconv.ParseInt` 只接受数字字符串（`"1"`、`"2"`、`"-1"`）。Python `json` 序列化整数 `1` 到 Go 后端会被 `json.Unmarshal` 反序列化为 `float64`，再 `fmt.Sprintf` 成 `"1"` → `ParseInt` 通过。字符串 `"qr_login"` 永远失败。

### 2.4 git blame 追溯

`platforms/xiaohongshu/client.py` 的 payload 是 commit `4e49434`（PR #13，2026-06-15 "improve HTTP layer"）**新写的**，并非从已删除的 `vendor/spider_xhs` 抄来。`"qr_login"` 是个**臆造值**——既不符合 Phase-3 调研结论（整数 1），也不符合 ReaJason 实战（省略）。

### 2.5 兼容字段判断（`qr_style` / `scene`）

| 字段 | 当前值 | 服务端报错 | 判断 |
|---|---|---|---|
| `qr_type` | `"qr_login"` | 明确要 int | **必改** |
| `qr_style` | `"default"` | 无（服务端未触达，因 qr_type 先挂） | 类型未知；Phase-3 抓包和 ReaJason **都没有此字段**，删掉最安全 |
| `scene` | `"login"` | 无 | 同上，Phase-3/ReaJason 均无，删除 |

服务端是 Go struct binding，未声明字段通常静默忽略（不会报错），但**保留就是保留未知行为风险**。最小改动 + 最小假设 = 删掉这两个字段。

---

## 3. 关键决策（已锁定，理由附后）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | `qr_type` 取值 | **`1`**（整数） | 仓库内 Phase-3 抓包铁证。ReaJason 用空 payload 也 OK，但保留 `qr_type:1` 更贴近 web 端真实流量，未来若服务端开始强制要求该字段也不会破 |
| D2 | 是否删 `qr_style` / `scene` | **删** | 三条证据（Phase-3 抓包、ReaJason、服务端报错）都指向"无此字段"。保留即保留未知风险 |
| D3 | 修复范围 | **仅 `client.py` 一处 + 新增 payload 断言测试** | 最小改动。`auth.py`/`web/routes/auth.py` 不动——它们只是透传 |
| D4 | web 端是否同时加 try/except 返回友好提示 | **不加**（列入"后续可选"） | (1) YAGNI：bug 修了就不再 500；(2) `unhandled_exception_handler` 已统一兜底；(3) `web/routes/auth.py:256` 已 `except Exception: ... raise` 做清理，逻辑完整；(4) 改动越少 PR 越易 review。仅当后续真出现"用户反复扫码失败且看不清原因"的反馈，再独立 PR 加 422 + 友好提示 |
| D5 | 测试策略 | **TDD**：先写一条失败测试（断言新 payload），跑红 → 改 client.py → 跑绿 | 复现 bug 的同时固化契约。新测试必须断言 `payload == {"qr_type": 1}`，**而非**只断言返回值 |
| D6 | 是否加 `qr_style`/`scene` 删除的回归断言 | **不需要** | D5 的 payload 全等断言已经隐含覆盖（少字段也是契约的一部分）。过度加断言反而脆弱 |
| D7 | 是否升级 `_request` 的错误信息（暴露响应体） | **不** | `_request` 已经在 `DataError` message 里包含响应体前 200 字符（`client.py:203`），对调试足够。本次 bug 现场就靠它定位的 |
| D8 | 是否改 `authenticator.py` 的 `_qr_code`/`expires_in` 等 | **不** | 与本 bug 无关。`QRCodeResult` 的字段映射已正确 |

---

## 4. 文件清单

| 文件 | 操作 | 改动 | 估计行数 |
|---|---|---|---|
| `platforms/xiaohongshu/client.py` | 改 | `create_qrcode` 的 payload 三行改一行 | -2 / +1 |
| `tests/test_xhs_client.py` | 改 | 新增 `test_create_qrcode_payload`，断言 `json=payload`；保留原 `test_create_qrcode` | +18 |
| `tests/test_xhs_authenticator.py` | 改（可选） | 不强制。若做：在 `test_returns_qr_code_result` 加注释说明 payload 契约在 client 层测试 | +0~3 |

**净改动**：约 -2 / +19，纯 bugfix，无 API/数据/配置变化。

---

## 5. 任务分解（TDD）

### Task 1 — 先写失败测试（红）

在 `tests/test_xhs_client.py` 中新增：

```python
async def test_create_qrcode_payload_is_qr_type_int(self, client, mock_session):
    """Regression: qr_type must be integer 1, not string "qr_login".

    Server (Go) does strconv.ParseInt on qrType; sending "qr_login" returns
    HTTP 400 "parse: put \"qr_login\" to field qrType". See
    docs/superpowers/plans/2026-06-13-xhs-qr-login-phase-3.md:811 for the
    captured real value.
    """
    resp = _mock_json_response(
        200, {"success": True, "data": {"qr_id": "q1", "qr_url": "u", "code": "c"}}
    )
    mock_session.request.return_value = resp

    with patch("platforms.xiaohongshu.client.get_xhs_sign"):
        await client.create_qrcode({"a1": "init"})

    # Inspect the actual JSON body sent over the wire.
    _call = mock_session.request.call_args
    assert _call.kwargs["json"] == {"qr_type": 1}
```

**验证（红）**：`uv run pytest tests/test_xhs_client.py::TestXhsClient::test_create_qrcode_payload_is_qr_type_int -x`

预期：`AssertionError: assert {'qr_type': 'qr_login', 'qr_style': 'default', 'scene': 'login'} == {'qr_type': 1}`

### Task 2 — 修复 client.py（绿）

`platforms/xiaohongshu/client.py:334-339`：

```diff
         try:
-            payload = {
-                "qr_type": "qr_login",
-                "qr_style": "default",
-                "scene": "login",
-            }
+            payload = {"qr_type": 1}
             return await self._request("POST", "/api/sns/web/v1/login/qrcode/create", json=payload)
```

**验证（绿）**：
```bash
uv run pytest tests/test_xhs_client.py::TestXhsClient::test_create_qrcode_payload_is_qr_type_int -x
uv run pytest tests/test_xhs_client.py tests/test_xhs_authenticator.py -x
```

### Task 3 — 静态检查

```bash
uv run ruff check platforms/xiaohongshu/client.py tests/test_xhs_client.py
uv run ruff format platforms/xiaohongshu/client.py tests/test_xhs_client.py
uv run pyright platforms/xiaohongshu/client.py tests/test_xhs_client.py
```

预期：零新增问题。

### Task 4 — 全量回归

```bash
uv run pytest -x
```

预期：全绿。

### Task 5 — 手动真实扫码验证（必做）

单元测试 mock 了 HTTP，**无法验证服务端是否真接受新 payload**。这一步是 bug 的最终判据。

清单：

1. `git checkout fix/xhs-qrtype`
2. 启动 web：`uv run python run_web.py`（或 `uv run uvicorn web.app:app`）
3. 浏览器打开登录管理页，点击 xhs 的"重新扫码"
4. **预期 A**：二维码图片正常渲染（不再白屏/报错）
5. 用手机小红书 App 扫码 → 确认
6. **预期 B**：登录成功，`data/tokens/xhs.json` 写入新 cookie
7. **预期 C**：`uv run trawler check --platform xhs` 跑一次抓取，确认新 cookie 可用

**若预期 A 失败**（二维码仍不渲染）：服务端可能进一步变更协议 → 看日志新错误信息，**不在本 PR 范围硬扩**，记录新 bug。

**若预期 A 成功但 B 失败**（扫码后状态轮询报错）：属于**独立的 verify/状态轮询 bug**，不在本 plan 范围（见风险 R2）。

---

## 6. 验证步骤（提交前完整清单）

```bash
# 0. 分支检查
git branch --show-current          # 必须是 fix/xhs-qrtype，不是 master

# 1. 静态
uv run ruff check .
uv run ruff format --check .
uv run pyright .

# 2. 单测
uv run pytest -x

# 3. 手动扫码（Task 5 完整清单）

# 4. 完成后：commit + push + 开 PR
#    PR 标题：fix(xhs): correct qr_type to integer 1 in qrcode/create payload
#    PR 正文引用本 plan + bug 现场日志
```

---

## 7. 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | XHS 服务端未来再次变更协议（如要求 `qr_type: 2` 或新增强制字段） | 扫码再次 400 | 在 `create_qrcode` docstring 里写明"payload 基于抓包 + 服务端 Go 反序列化策略，未来可能再变"；保持本函数单一职责便于定位 |
| R2 | 扫码成功但 `check_qrcode_status` 轮询 / 新 cookie 验证失败 | 用户以为"扫码修好了"但实际仍无法用 | 这是**独立 bug**。本 plan 只保证二维码能生成。在 PR 描述明确写"本 PR 仅修 create 端，verify/轮询问题另行跟踪" |
| R3 | `qr_type: 1` 在某些场景下被服务端拒（例如不同 `scene`） | 二维码渲染成功但扫描后无响应 | Phase-3 抓包证据来自生产环境登录场景，与当前用例一致。ReaJason 空 payload 也能过，说明服务端对该字段宽容。低概率 |
| R4 | web 路由仍把任何异常转成 500 → 用户看不清具体错误 | 体验差，但属于**既有架构决策**（统一兜底） | 不在本 PR 范围。若未来 XHS 协议再变，用户会再看到"内部错误"——届时再考虑 D4 |
| R5 | 其他平台（bilibili/weibo）的 QR 流程是否同病 | 仅 xhs | 仅 xhs 走自建 HTTP；bilibili 用 `bilbili_api` 库，weibo 走另一路径。不相关 |

---

## 8. 后续可选清理（**不在本 PR 范围**）

- `web/routes/auth.py:243` 加 try/except，把 `DataError` 转成 422 + `{"detail": "<platform-specific 中文提示>"}`，前端 toast 显示。仅当用户反馈"扫码失败看不清原因"时做。
- `docs/superpowers/plans/2026-06-13-xhs-qr-login-phase-3.md` 已是历史归档，无需同步更新。

---

## 9. 摘要

- **task_count**: 5（写测试 → 改 client → 静态检查 → 全量回归 → 手动扫码）
- **scope**: 仅 `platforms/xiaohongshu/client.py` + 1 个新测试
- **key_decisions**:
  - `qr_type` = 整数 `1`（仓库 Phase-3 抓包铁证）
  - 删 `qr_style` / `scene`（无证据支持）
  - 不改 web 路由（YAGNI，500 兜底已存在）
  - TDD：测试必须断言 `payload == {"qr_type": 1}` 全等，而非仅返回值
- **estimated_steps**: 实现约 10 分钟；手动扫码验证 5-10 分钟
