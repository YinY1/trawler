# HTTP API 文档

Trawler 暴露一套面向机器调用的 JSON HTTP API，前缀统一为 `/api/v1`，**与 Web UI 共享同一个 FastAPI 服务（同端口）**。Web UI 走 session cookie + CSRF，API 走 Bearer token 鉴权，两套鉴权链路完全隔离。

这套 API 主要为 bot / 外部脚本 / 自动化集成而设计：通过 HTTP 手动触发检查、查询消息、批量重跑、管理订阅，而不必依赖 cron 或 Web UI 点击。

- 基础路径：`http://<host>:<port>/api/v1`
- 内容类型：`application/json`（SSE 端点除外，见 [SSE 说明](#sse-说明)）
- 默认端口：`8080`（与 Web UI 一致，由 `[web]` 配置决定）

---

## 认证

除 `GET /health` 之外，所有 API 端点都要求在请求头携带 Bearer token：

```
Authorization: Bearer <token>
```

### 生成 token

通过项目自带的 CLI 工具生成（在服务器本地运行）：

```bash
# 生成新 token，明文仅打印一次（存储为 SHA-256 hash，无法恢复）
uv run python -m api.token_tool create <name>

# 同名 token 已存在时强制覆盖
uv run python -m api.token_tool create <name> --force

# 列出所有 token（只显示 hash 前 8 位）
uv run python -m api.token_tool list

# 按名字撤销 token
uv run python -m api.token_tool revoke <name>
```

token 明文**只在创建时打印一次**，后续无法查看；存储的是 SHA-256 hash。校验时走常量时间比对（`hmac.compare_digest`），防 timing attack。

### 鉴权失败响应

无 header、scheme 非 `Bearer`、或 token 不匹配，统一返回：

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{"detail": "invalid or missing token"}
```

---

## 端点清单

| Method   | Path                                              | 说明                       | 鉴权 |
| -------- | ------------------------------------------------- | -------------------------- | ---- |
| `GET`    | `/api/v1/health`                                  | 健康检查（探活）           | 否   |
| `POST`   | `/api/v1/check/run`                               | 触发一次检查（全量/手动）  | 是   |
| `GET`    | `/api/v1/check/status`                            | 当前 run 的状态快照        | 是   |
| `GET`    | `/api/v1/check/stream`                            | SSE 日志实时流             | 是   |
| `GET`    | `/api/v1/messages`                                | 多维度筛选消息             | 是   |
| `GET`    | `/api/v1/messages/{msg_id}`                       | 单条消息详情               | 是   |
| `POST`   | `/api/v1/messages/rerun`                          | 批量重跑指定消息           | 是   |
| `GET`    | `/api/v1/subscriptions`                           | 列出订阅（可选平台过滤）   | 是   |
| `POST`   | `/api/v1/subscriptions`                           | 添加订阅                   | 是   |
| `DELETE` | `/api/v1/subscriptions/{platform}/{identifier}`   | 删除订阅                   | 是   |

---

## 端点详细说明

### GET /api/v1/health

健康检查端点，无鉴权。供监控探活 / 容器健康检查使用。

- **鉴权**：否
- **请求参数**：无
- **响应（200）**：

```json
{
  "status": "ok",
  "version": "0.x.y"
}
```

- **curl**：

```bash
curl http://localhost:8080/api/v1/health
```

---

### POST /api/v1/check/run

触发一次检查。全量模式走 detector 全流程；手动模式按筛选条件从既有消息中重跑某一段。

- **鉴权**：是
- **请求体**（`CheckRunRequest`）：

| 字段           | 类型    | 默认值     | 说明                                                                 |
| -------------- | ------- | ---------- | -------------------------------------------------------------------- |
| `mode`         | string  | `"full"`   | `"full"` 全量检查；`"manual"` 按 `since/title/author/reset_phase` 筛选既有消息重跑 |
| `platform`     | string  | `"all"`    | 平台过滤（`bili` / `xhs` / `weibo` / `all`）                         |
| `since`        | string? | `null`     | manual 模式筛选：unix 时间戳或 `24h` / `7d` / `30m` / `2026-06-01`   |
| `title`        | string? | `null`     | manual 模式筛选：标题模糊匹配                                        |
| `author`       | string? | `null`     | manual 模式筛选：作者匹配                                            |
| `reset_phase`  | string? | `null`     | manual 模式重跑起点，Phase 枚举名（如 `summarized`）                 |
| `skip_push`    | bool    | `true`     | 是否跳过推送通知（手动重跑默认 `true`，避免重复打扰订阅者）          |

- **响应（202 Accepted）**：

```json
{
  "status": "started",
  "task_id": "f47ac10b58cc4372a5670e02b2c3d479",
  "mode": "full"
}
```

- **响应（409 Conflict）** — 已有 run 在跑：

```json
{
  "status": "already_running",
  "task_id": "f47ac10b58cc4372a5670e02b2c3d479"
}
```

> 注：409 响应走扁平 shape（`status`/`task_id`），不是 FastAPI 默认的 `{"detail": ...}` 包装。

- **响应（422 Unprocessable Entity）** — manual 模式无任何筛选参数 / `reset_phase` 非法 / `since` 解析失败：

```json
{"detail": "manual 模式必须提供 since/title/author/reset_phase 中至少一项"}
```

- **curl**：

```bash
# 全量检查
curl -X POST http://localhost:8080/api/v1/check/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode": "full", "platform": "all"}'

# 手动模式：重跑最近 24h 的 bili 消息
curl -X POST http://localhost:8080/api/v1/check/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode": "manual", "platform": "bili", "since": "24h", "reset_phase": "summarized"}'
```

---

### GET /api/v1/check/status

当前 run 的状态快照。无 run 在跑时返回 `running=false` + `started_at=null`，但 `log_history` 会保留最近一次 run 的日志（供刚触发完的客户端轮询）。

- **鉴权**：是
- **请求参数**：无
- **响应（200）**：

```json
{
  "running": true,
  "processed_count": 12,
  "started_at": 1751700000.0,
  "log_history": [
    {"type": "log", "message": "🔍 开始检查...", "time": "10:00:01"},
    {"type": "log", "message": "✓ 找到 3 条新消息", "time": "10:00:05"}
  ]
}
```

字段说明：

| 字段              | 类型           | 说明                                                          |
| ----------------- | -------------- | ------------------------------------------------------------- |
| `running`         | bool           | 是否正在跑                                                    |
| `processed_count` | int            | 已处理计数                                                    |
| `started_at`      | float \| null  | 当前 run 起始 unix 时间戳；无 run 在跑时为 `null`             |
| `log_history`     | list[object]   | 日志条目数组，内部 `_ts` 字段已被 strip，客户端不会看到       |

- **curl**：

```bash
curl http://localhost:8080/api/v1/check/status \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/v1/check/stream

SSE（Server-Sent Events）日志实时流。触发 run 后订阅本端点，可实时拿到 log/done/error 事件。

- **鉴权**：是（**必须用 Bearer header**，详见 [SSE 说明](#sse-说明)）
- **请求参数**：无
- **响应**：`text/event-stream`，事件 shape 与 `log_history` 单条一致（`type`/`message`/`time`）。run 结束时下发 EOF 并断流。
- **行为**：bot 触发 run 时，浏览器开着的 Web UI 也会看到同一份 SSE 流（特性，非 bug；Web UI 与 API 共享 `state.subscribers`）。
- **curl**：

```bash
curl -N http://localhost:8080/api/v1/check/stream \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/v1/messages

多维度筛选消息，所有参数 AND 组合，缺省不过滤。

- **鉴权**：是
- **请求参数**（query）：

| 参数       | 类型    | 默认   | 说明                                                                            |
| ---------- | ------- | ------ | ------------------------------------------------------------------------------- |
| `since`    | string? | `null` | 起始时间，unix 时间戳或 `24h` / `7d` / `30m` / `2026-06-01`                     |
| `title`    | string? | `null` | 标题模糊匹配                                                                    |
| `author`   | string? | `null` | 作者匹配                                                                        |
| `platform` | string? | `null` | 平台过滤（`bili` / `xhs` / `weibo`）                                            |
| `phase`    | string? | `null` | Phase 枚举名匹配，大小写不敏感（如 `summarized` / `notified` / `error`）        |

- **响应（200）**：

```json
{
  "messages": [
    {
      "msg_id": "bili_12345",
      "platform": "bili",
      "content_type": "VIDEO",
      "phase": "SUMMARIZED",
      "pubdate": 1751600000,
      "title": "示例视频",
      "author": "示例UP主",
      "created_at": 1751600100.0,
      "updated_at": 1751600200.0,
      "error": "",
      "dynamic_text": "",
      "subscription_ref": "12345",
      "xsec_token": "",
      "body": "",
      "summary": "AI 摘要内容...",
      "retry_count": 0,
      "last_error": "",
      "permanent_error": false
    }
  ],
  "count": 1
}
```

> `content_type` / `phase` 序列化为枚举名（如 `"VIDEO"` / `"SUMMARIZED"`），不是整数 enum 值。

- **响应（422）** — `phase` 非法或 `since` 无法解析：

```json
{"detail": "无法解析 since 值: 'xxx'（支持格式: 24h / 7d / 30m / 2026-06-01 / unix 时间戳）"}
```

- **curl**：

```bash
# 最近 7 天所有平台的消息
curl "http://localhost:8080/api/v1/messages?since=7d" \
  -H "Authorization: Bearer $TOKEN"

# 仅 bili 平台、summarized 阶段
curl "http://localhost:8080/api/v1/messages?platform=bili&phase=summarized" \
  -H "Authorization: Bearer $TOKEN"
```

---

### GET /api/v1/messages/{msg_id}

单条消息详情。

- **鉴权**：是
- **路径参数**：`msg_id` — 消息 ID
- **响应（200）**：单个 `MessageOut` 对象（shape 与 [GET /messages](#get-apiv1messages) 列表内元素一致）
- **响应（404）**：

```json
{"detail": "message not found"}
```

- **curl**：

```bash
curl http://localhost:8080/api/v1/messages/bili_12345 \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/v1/messages/rerun

批量重跑指定消息。从指定 phase 重新跑 pipeline（download/transcribe/summarize/notify）。

- **鉴权**：是
- **请求体**（`RerunRequest`）：

| 字段          | 类型         | 默认值       | 说明                                                          |
| ------------- | ------------ | ------------ | ------------------------------------------------------------- |
| `msg_ids`     | list[string] | —            | 必须非空（空数组返回 422）                                    |
| `from_phase`  | string       | `"summarized"` | 重跑起点，Phase 枚举名                                        |
| `skip_push`   | bool         | `true`       | 是否跳过推送通知（默认 `true`，避免重复打扰订阅者）          |

- **响应（202 Accepted）**：

```json
{
  "status": "started",
  "task_id": "f47ac10b58cc4372a5670e02b2c3d479",
  "reset_count": 3
}
```

- **响应（409 Conflict）** — 已有 run 在跑（与 `/check/run` 共享锁，详见 [互斥语义](#互斥语义)）：

```json
{
  "status": "already_running",
  "task_id": "f47ac10b58cc4372a5670e02b2c3d479"
}
```

- **响应（422 Unprocessable Entity）** — `msg_ids` 为空或 `from_phase` 非法：

```json
{"detail": "msg_ids 不能为空"}
```

- **响应（404 Not Found）** — 全部 `msg_id` 都不存在：

```json
{"detail": "message not found"}
```

- **curl**：

```bash
curl -X POST http://localhost:8080/api/v1/messages/rerun \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"msg_ids": ["bili_12345", "bili_67890"], "from_phase": "summarized"}'
```

---

### GET /api/v1/subscriptions

列出订阅，可选平台过滤。

- **鉴权**：是
- **请求参数**（query）：

| 参数       | 类型    | 默认   | 说明                              |
| ---------- | ------- | ------ | --------------------------------- |
| `platform` | string? | `null` | 按平台过滤（`bili` / `xhs` / `weibo`） |

- **响应（200）** — 透传 `list_subscriptions` 的原始 dict，section 为平台 key，entry 结构由 `config/subscriptions.toml` 决定：

```json
{
  "platforms": {
    "bilibili": [
      {"uid": "12345", "name": "示例UP主", "notify_endpoints": []}
    ],
    "xiaohongshu": [
      {"user_id": "abc", "name": "示例博主"}
    ]
  }
}
```

- **curl**：

```bash
curl "http://localhost:8080/api/v1/subscriptions?platform=bili" \
  -H "Authorization: Bearer $TOKEN"
```

---

### POST /api/v1/subscriptions

添加订阅。

- **鉴权**：是
- **请求体**（`SubscriptionAddRequest`）：

| 字段         | 类型   | 说明                                       |
| ------------ | ------ | ------------------------------------------ |
| `platform`   | string | 平台名（`bili` / `xhs` / `weibo`）         |
| `identifier` | string | 订阅标识（uid / user_id，API 层统一为 str） |
| `name`       | string | 显示名                                     |

- **响应（200）** — 成功 / 业务失败共用同一 shape，调用方靠 `success` 字段判断（重复 / 无效平台是业务可恢复态，不映射成 4xx）：

```json
{"success": true, "message": "已添加: 示例UP主"}
```

```json
{"success": false, "message": "已存在: 12345"}
```

- **curl**：

```bash
curl -X POST http://localhost:8080/api/v1/subscriptions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"platform": "bili", "identifier": "12345", "name": "示例UP主"}'
```

---

### DELETE /api/v1/subscriptions/{platform}/{identifier}

删除订阅。

- **鉴权**：是
- **路径参数**：

| 参数         | 说明                            |
| ------------ | ------------------------------- |
| `platform`   | 平台名（`bili` / `xhs` / `weibo`） |
| `identifier` | 订阅标识（uid / user_id）        |

- **响应（200）** — 未找到也返回 200 + `success=false`（与 add 的"已存在"对称）：

```json
{"success": true, "message": "已删除: 12345"}
```

```json
{"success": false, "message": "未找到: 12345"}
```

- **curl**：

```bash
curl -X DELETE http://localhost:8080/api/v1/subscriptions/bili/12345 \
  -H "Authorization: Bearer $TOKEN"
```

---

## 错误处理

所有错误响应统一为 JSON。除少数扁平 shape 外，遵循 FastAPI 默认的 `{"detail": "..."}` 包装。

| 状态码 | 触发场景                                                                 | 响应 shape                                                    |
| ------ | ------------------------------------------------------------------------ | ------------------------------------------------------------- |
| 401    | 缺失 / 错误的 Bearer token                                               | `{"detail": "invalid or missing token"}`                      |
| 404    | 消息 / 路径不存在                                                        | `{"detail": "message not found"}`                            |
| 409    | 检查/重跑互斥锁被占用（见 [互斥语义](#互斥语义)）                       | `{"status": "already_running", "task_id": "..."}` (扁平)     |
| 422    | 参数校验失败（pydantic body、`since`/`phase` 解析、manual 模式无筛选等） | `{"detail": "..."}` 或 FastAPI 校验错误数组                   |
| 500    | 服务端未捕获异常                                                         | `{"detail": "Internal Server Error"}`                        |

> 注：`/check/run` 和 `/messages/rerun` 的 409 走扁平 shape（`status`/`task_id`），方便调用方拿到当前 task_id 后转去查 `/check/status`。其余错误均为 `{"detail": ...}`。

---

## 互斥语义

`POST /check/run`、`POST /messages/rerun`、Web UI 的 `POST /check/run` 与 Web UI 的 `POST /messages/batch-reprocess` **四方共享同一把锁**（`app.state.check_running`）。

这是**有意为之的特性（feature）**：detector 并发跑两份会重复处理同一批新消息并写同一份 `MessageStore`，导致重复推送、快照覆盖、去重失效。所以任意时刻只允许一个 run 在跑。

并发触发的第二个请求会立即拿到 409：

```json
{"status": "already_running", "task_id": "f47ac10b58cc4372a5670e02b2c3d479"}
```

调用方拿到 `task_id` 后可以：

1. 轮询 `GET /check/status` 等当前 run 结束
2. 订阅 `GET /check/stream` 实时跟踪进度
3. 决定是否重试触发

bot 集成时建议实现**指数退避重试**，或在收到 409 后直接转去查询既有 run 的状态而非盲目重试。

---

## SSE 说明

`GET /check/stream` 走 Server-Sent Events 协议（`text/event-stream`）。

**浏览器 EventSource API 的限制**：浏览器的 `EventSource` 不支持自定义请求头，因此无法在请求里带 `Authorization: Bearer` header。这意味着**浏览器侧无法直接消费这个 SSE 端点**（鉴权会直接 401）。

**bot / 服务端集成友好**：用 `httpx` / `requests` / `curl` / Node fetch / 任何能自定义 header 的 HTTP 客户端都能正常订阅。例如：

```bash
# curl 订阅 SSE 流
curl -N http://localhost:8080/api/v1/check/stream \
  -H "Authorization: Bearer $TOKEN"
```

```python
# httpx 异步订阅示例
import httpx

async with httpx.AsyncClient() as client:
    async with client.stream(
        "GET",
        "http://localhost:8080/api/v1/check/stream",
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        async for line in resp.aiter_lines():
            print(line)
```

如果需要从浏览器消费日志流，请走 Web UI 自带的 SSE 端点（`GET /check/stream`，session 鉴权），而非本 API 端点。
