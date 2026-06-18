# 全局日志增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 trawler 项目中关键操作（token 续期、订阅增删、pipeline 处理、配置加载、CLI 操作）的日志空白，使用户在 Web UI 和 CLI 下都能看到清晰的进度 trace。

**Architecture:** 在现有 logging 设施上直接补充 `logger.info/warning/error` 调用。Web 端已通过 `QueueLogHandler` + `LogBus` 将 logging 桥接到 `/logs` SSE 页面。新增 logger 调用会自动出现在 Web 日志页，无需改动基础设施。CLI 端通过 `setup_logging()` 输出到控制台 + 文件。

**Tech Stack:** Python 3.12, `logging` 标准库, `QueueLogHandler` (web/logging_bridge.py), `LogBus` fan-out

---

## Level 规范

| Level | 用途 | 例子 |
|-------|------|------|
| **DEBUG** | 极细粒度调试 | HTTP 请求/响应详情、配置解析细节、下载 chunk 进度 |
| **INFO** | 正常进度（用户可见） | "开始检查 bili"、"消息 BV1X → DOWNLOADED"、"token 续期成功"、"订阅已添加: UP主名" |
| **WARNING** | 可恢复异常 | "评论获取失败"、"转写跳过"、"token 即将过期" |
| **ERROR** | 不可恢复异常 | "下载失败: ..."、"AI 调用失败"、"配置加载失败" |

所有新增 logger 调用必须使用当前模块的 `logger = logging.getLogger(__name__)` 实例。消息格式保持 emoji 前缀风格：

```
INFO: ⬇ 下载... → ✓ 下载完成
INFO: bili:BV1X... → DOWNLOADED ✓
INFO: 🔑 bilibili token 续期成功
WARNING: ⚠️  评论获取失败: ...
ERROR: ✗ 摘要生成失败: ...
```

---

## 文件映射清单

| 文件 | 当前日志状态 | 需要增强的模块 |
|------|-------------|---------------|
| `core/engine.py` | 仅 error handler 有 log | `process_message()` 缺 phase 推进 log |
| `core/pipeline.py` | 有入口/出口 log | 缺 token 续期步骤 log |
| `web/routes/auth.py` | 无任何 logger 调用 | `auth_qr()` / `auth_poll()` 全程缺 log |
| `web/routes/subscriptions.py` | 无任何 logger 调用 | add/remove/search 全程缺 log |
| `core/subscription_cli.py` | add/remove 仅有成功 log | 缺入口/失败 log |
| `shared/config.py` | 无任何 logger 调用 | `load_config()` 全程缺 log |
| `shared/auth/scheduler.py` | 有较好的 log | 缺入口 log（开始检查 xx 平台） |
| `shared/downloader.py` | 有开始/结束 log | 缺下载进度 log |
| `run_check.py` | 全部用 `console.print` | login/token refresh/subscription 操作缺 logger 调用 |
| `platforms/*/auth.py` | 有部分 log | 各平台 QR/refresh/validate 方法补全缺失点 |

---

## 日志空白区清单

### Task 1: pipeline 每条消息各阶段推进时打 info

**文件:** `core/engine.py:90-128`
**函数:** `PipelineEngine.process_message()`
**现状:** 从 `start_idx` 循环推进 phase，只有在 handler 找不到时报 error；每步推进后不打印任何进度。
**建议改动:**

- 循环开始时打印：`INFO  "▶ 处理消息 {msg.platform}:{msg.msg_id} ({msg.title})"`
- 每个 `next_phase` 推进成功时打印：`INFO  "{msg.platform}:{msg.msg_id} → {next_phase.name} ✓"`
- phase 推进失败的 handler 已有 `logger.error`，不变

**Level:** INFO
**示例输出:**
```
INFO  ▶ 处理消息 bili:bili:BV1XX... (视频标题)
INFO  bili:BV1XX... → DOWNLOADED ✓
INFO  bili:BV1XX... → TRANSCRIBED ✓
INFO  bili:BV1XX... → SUMMARIZED ✓
INFO  bili:BV1XX... → PUSHED ✓
```

---

### Task 2: token 续期全流程打 info（Web 端）

**文件:** `web/routes/auth.py:62-119`
**函数:** `auth_qr()` / `auth_poll()`

**现状:** 两个函数完全无 `logger.*` 调用。用户点"续期"后在页面能看到前端反馈，但后端无任何 trace。

**auth_qr() — 行 63:**
- 生成 QR: `INFO  "🔑 {platform_key} 生成二维码..."`
- 成功: `INFO  "🔑 {platform_key} 二维码生成成功 (qr_key={qr_key})"`
- 异常: `WARNING/ERROR` 已在函数外 catch，`auth_poll` 的 `except` 里补充 logger

**auth_poll() — 行 82:**
- 入口: `INFO  "🔑 {platform_key} 轮询扫码状态..."`
- no_session: `WARNING  "{platform_key} 无有效 QR session"`
- 异常: `WARNING  "{platform_key} 轮询异常: {exc}"`（现有代码 return 了但没有 log）
- SUCCESS → get_tokens: `INFO  "🔑 {platform_key} 扫码成功，获取凭证..."`
- 保存成功: `INFO  "🔑 {platform_key} 凭证已保存"`
- 保存失败: `WARNING  "{platform_key} 凭证保存失败: {exc}"`

**Level:** INFO / WARNING

---

### Task 3: token 续期全流程打 info（CLI 端）

**文件:** `run_check.py:231-330`
**函数:** `_refresh_single_platform()`

**现状:** 全部使用 `console.print`，无 `logger.*`。在 Web 模式下（带 QueueLogHandler），`console.print` 不会出现在 `/logs` 页面。

**建议改动:** 在每个 `console.print` 旁边加对应的 `logger.info/warning/error`：
- 入口: `logger.info("🔑 %s Token 续期开始...", platform)`
- 成功: `logger.info("🔑 %s Token 续期成功", platform)`
- 失败: `logger.warning("🔑 %s Token 续期失败: %s", platform, exc)`
- 过期/未配置: `logger.warning("🔑 %s token 已过期或未配置", platform)`

保留 `console.print`（CLI 交互需要），补充 logger 使 Web 日志页可见。

**Level:** INFO / WARNING

---

### Task 4: 订阅增删打 info

**文件 1:** `core/subscription_cli.py`
- `add_subscription()` — 行 120-159: 目前只有成功后有 logger（行 158）。缺：
  - 入口: `logger.info("📋 添加订阅: %s/%s = %s", section, key, typed_id)`
  - 重复: `logger.warning("📋 订阅已存在: %s", name)`
- `remove_subscription()` — 行 162-218: 目前只有成功后有 logger（行 217）。缺：
  - 入口: `logger.info("📋 删除订阅: %s/%s = %s", section, key, typed_id)`
  - 未找到: `logger.warning("📋 未找到订阅: %s/%s = %s", section, key, typed_id)`
  - 文件不存在: `logger.warning("📋 订阅文件不存在")`

**文件 2:** `web/routes/subscriptions.py` — 全部函数无 logger：
- `subscriptions_add()` — 行 35: `logger.info("📋 Web 添加订阅: %s/%s = %s", platform, identifier, name)`
- `subscriptions_remove()` — 行 47: `logger.info("📋 Web 删除订阅: %s/%s", platform, identifier)`
- `subscriptions_search()` — 行 58: `logger.info("📋 Web 搜索: %s / %s", platform, name)`

**Level:** INFO / WARNING

---

### Task 5: CLI 登录全流程打 info

**文件:** `run_check.py:67-114`
**函数:** `login` 命令

**现状:** 全部使用 `console.print`，无 `logger.*`。在 Web 模式下用户看不到登录进度。

**建议改动:**
- 开始: `logger.info("🔑 %s 登录流程启动...", platform)`
- 等待扫码: `logger.info("🔑 %s 等待扫码...", platform)`
- 已扫码: `logger.info("🔑 %s 已扫码，等待确认", platform)`
- 成功: `logger.info("🔑 %s 登录成功", platform)`
- 过期: `logger.warning("🔑 %s 二维码已过期", platform)`
- 失败: `logger.warning("🔑 %s 登录失败: %s", platform, exc)`

---

### Task 6: 配置加载打 info

**文件:** `shared/config.py:310-364`
**函数:** `load_config()`

**现状:** 无任何 logger 调用。加载过程中文档/报错不可追踪。

**建议改动:**
- 入口: `logger.info("⚙️ 加载配置: %s", path)`
- cookies.toml 找到: `logger.info("⚙️ 合并凭证: %s", cookies_path)`
- subscriptions.toml 找到: `logger.info("⚙️ 合并订阅: %s", subs_path)`
- 检测到旧 yaml: `logger.warning("⚙️ 检测到旧版 config.yaml，请迁移至 TOML")`
- 完成: `logger.info("⚙️ 配置加载完成")`

**Level:** INFO / WARNING

---

### Task 7: run_check_once 的 token 续期步骤增强

**文件:** `core/pipeline.py:98-116`
**函数:** `run_check_once()`

**现状:** 行 116 的 token 续期循环无 step log；行 98 打了版本号。用户看到 "Trawler v0.1.0" 后可能长时间无日志（在等 token 续期），导致用户以为卡住了。

**建议改动:**
- 行 115 循环内: `logger.info("🔑 检查 %s token 状态...", pdef.auth_name)` 在每个平台续期前打印
- 如果只有一个平台、串行路径（行 141），也加上同样的 log

**Level:** INFO

---

### Task 8: 平台 authenticator 中补全缺失日志

**现状:** 各平台 auth 模块已有部分 logger，但以下场景缺失：

**bilibili/auth.py:**
- `refresh_tokens()` 行 151-156: 缺少 refresh_token 时的入口 log
- `check_refresh()` 返回 False（无需刷新）时应该有 `logger.info` 标记进度
- `validate_tokens()` 没有入口/出口 log

**xiaohongshu/auth.py:**
- `generate_qr_code()` 行 105-122: 无任何 log
- `poll_qr_status()` 行 124-143: 无任何 log
- `get_tokens()` 行 145-162: 无任何 log
- `refresh_tokens()` 行 195-210: 无入口/出口 log

**weibo/auth.py:**
- `generate_qr_code()` 行 110-133: 无任何 log
- `poll_qr_status()` 行 135-183: 只有 warning 时有 log
- `get_tokens()` 行 185-249: 无任何 log（失败时有 raise，但无 trace）
- `refresh_tokens()` 行 251-293: 只有 keepalive 失败时有 warning

**建议改动:**
每个平台每个关键方法加入口/出口 INFO log。示例:
```python
# generate_qr_code 入口
logger.info("🔑 %s 生成二维码...", self.__class__.__name__)
# poll_qr_status 状态变化
logger.info("🔑 %s 扫码状态: %s", self.__class__.__name__, status.status)
# refresh_tokens 入口
logger.info("🔑 %s 续期 token...", self.__class__.__name__)
# validate_tokens 结果
logger.info("🔑 %s token 有效性: %s", self.__class__.__name__, valid)
```

**Level:** INFO / WARNING

---

### Task 9: 下载器增强进度日志

**文件:** `shared/downloader.py`

**现状:** 有开始/结束 log（行 234、275），但下载过程中（大文件）无进度日志。用户长时间看不到反馈。

**建议改动:**
- `_download_bili_video()` 行 219-224: 每接收 100 个 chunk 打一条 DEBUG log（track 进度）
- 或者更简单：下载完成时打 `downloaded X bytes` 信息

但考虑到 `downloader.py` 的音频文件通常不大（<50MB），可以只增强完成时的日志增加文件大小信息：
- 行 233: `logger.info("⬇ 下载完成: %s -> %s (%.1f MB)", display_name, filepath.name, filepath.stat().st_size / 1024 / 1024)`

**Level:** INFO

---

## 验证清单

1. **lint**: `uv run ruff check .` — 无新增问题
2. **type check**: `uv run pyright .` — 无新增 error（所有改动只加 `logger.*()` 调用，不需要类型标注变更）
3. **test**: `uv run pytest -x` — 全部通过
4. **Web 日志页验证**:
   - 启动 web: `uv run trawler-web`（或相应命令）
   - 打开 `/logs` 页面
   - 在 `/auth` 页面点 B 站"续期"按钮 → 看 `/logs` 是否看到 `🔑 bili 生成二维码...` 等日志
   - 在 `/subscriptions` 页面增删订阅 → 看日志是否出现
   - 在 `/check` 页面触发检查 → 看每条消息的阶段推进日志
5. **CLI 验证**:
   - `uv run trawler check --platform bili --verbose` — 观察阶段推进日志
   - `uv run trawler token refresh --platform bili` — 观察 `🔑` 日志
   - `uv run trawler subscription add --platform bili --id 123 --name "test"` — 观察 `📋` 日志
6. **文件日志验证**: 检查 `data/trawler.log` 中是否包含所有新增日志

---

## 执行顺序建议

1. **Task 1** (pipeline phase 推进) — 核心用户可见改进，每次 check 都会触发
2. **Task 6** (配置加载) — 基础，几乎所有操作都经过此
3. **Task 2 + 3** (token 续期 Web + CLI) — 用户明确提到的痛点
4. **Task 4** (订阅增删) — 常用操作
5. **Task 5** (CLI 登录) — 使用频率较低但仍需补齐
6. **Task 7** (run_check_once 步骤) — 小改动，配合 Task 1
7. **Task 8** (平台 authenticator) — 覆盖边缘场景
8. **Task 9** (下载器增强) — 影响最小，放最后

共 **8 个 Tasks**，43 个单步改动点，预计总耗时约 25 分钟。
