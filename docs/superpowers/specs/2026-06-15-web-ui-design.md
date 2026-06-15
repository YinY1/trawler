# Web UI Design — FastAPI + HTMX

为 Trawler 添加 Web 管理界面。零侵入，不改现有业务逻辑。

## Status

Approved. All sections confirmed by user.

## 架构

```
Browser ──HTTP/SSE──▶ FastAPI ──async──▶ trawler core
                        │
                   Jinja2/HTMX
                    模板渲染
```

FastAPI 是薄 Web wrapper，不改变 trawler 核心逻辑。所有现有 async 函数通过 `async def` endpoint 直接调用，不做二次封装层。

## 异步改造

当前几个同步函数改为 `async def`，CLI 层用 `asyncio.run()`，Web 层直接 `await`：

| 函数 | 现状态 | 改后 |
|---|---|---|
| `search_by_name()` | sync 壳 + `asyncio.run()` | `async def`，去掉壳 |
| `list_subscriptions()` | sync | `async def` |
| `add_subscription()` | sync | `async def` |
| `remove_subscription()` | sync | `async def` |
| `load_config()` | sync | `async def` |
| `update_auth_section()` | sync | `async def` |

文件 I/O 操作小且少，直接用 sync 读写包裹在 `async def` 中，不引入 `aiofiles`。

## 项目结构

```
trawler/
├── web/
│   ├── __init__.py
│   ├── app.py              # FastAPI 应用 + 生命周期 + 路由注册
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── dashboard.py     # GET /
│   │   ├── subscriptions.py # GET/POST/DELETE /subscriptions
│   │   ├── check.py         # POST /check + SSE /check/stream
│   │   ├── auth.py          # GET /login/{platform} + GET /login/poll
│   │   └── settings.py      # GET/POST /settings
│   ├── templates/
│   │   ├── base.html        # sidebar + main 布局
│   │   ├── dashboard.html
│   │   ├── subscriptions.html
│   │   ├── check.html
│   │   ├── login.html
│   │   └── settings.html
│   └── static/
│       └── app.css
├── core/
│   ├── pipeline.py          # run_check_once + log_callback 参数
│   ├── subscription_cli.py  # async def 化
│   └── ...
├── run_check.py             # CLI 不变，Click handler 内 asyncio.run()
├── run_web.py               # uvicorn 启动入口
└── pyproject.toml           # 追加 [web] extras
```

## 路由

| Route | 方法 | 功能 | 调用方 |
|---|---|---|---|
| `/` | GET | Dashboard：消息统计 + 最新消息 | `MessageStore`, `load_config` |
| `/subscriptions` | GET | 订阅列表 | `list_subscriptions` |
| `/subscriptions/add` | POST | 搜索 + 添加订阅 | `search_by_name`, `add_subscription` |
| `/subscriptions/remove` | POST | 删除订阅 | `remove_subscription` |
| `/check` | GET | 检查页面 | — |
| `/check/run` | POST | 后台触发检查 | `run_check_once` via background task |
| `/check/stream` | GET (SSE) | 流式推送检查日志 | log_callback → asyncio.Queue → SSE |
| `/auth` | GET | 登录管理页面 | `load_config` |
| `/auth/qr/{platform}` | GET | 获取 QR 码图片 | `generate_qr_code` + `qrcode` 库渲染 |
| `/auth/poll/{platform}` | GET | 轮询扫码状态 | `poll_qr_status` |
| `/settings` | GET/POST | 查看/更新配置 | `load_config` + 写 TOML |

## 关键实现细节

### log_callback

`run_check_once` 加可选参数：

```python
log_callback: Callable[[str, str], None] | None = None
# (event_type: "log" | "done" | "error", message: str) -> None
```

Web 层用 `asyncio.Queue` 对接，SSE endpoint 从 queue 读取并推送。

### QR 登录

Web 层不调用 `qr_login()` 做长轮询。改为三步：

1. `GET /auth/qr/{platform}` → `generate_qr_code()` → 渲染 QR → 返回图片 bytes
2. 前端 `<img src="/auth/qr/bili">` 展示 QR，`setInterval` 轮询 `poll`
3. `GET /auth/poll/{platform}` → `poll_qr_status()` → 返回 status

### 后台 task

`/check/run` 用 `asyncio.create_task()` 启动 `run_check_once`。SSE endpoint 通过 `asyncio.Queue` 接收日志。当前项目只有单用户，不需要任务持久化或队列持久化。

### 模板

HTMX `hx-get` / `hx-post` / `hx-target` 做局部刷新。base.html 是 sidebar + main 双栏布局，不写 JS。

## 配置写回

`/settings` POST 将表单数据写回 `config/config.toml`。使用 `tomllib` 读取 + `tomli-w`/string replace 写回（与现有 `subscription_cli.py` 风格一致）。

## 测试

- 每个路由对应 `tests/test_web_*.py`
- 用 `httpx.AsyncClient` 测试
- `/check/run` mock `asyncio.create_task` 防止真实运行

## 依赖

```toml
[project.optional-dependencies]
web = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "jinja2>=3.1.0",
    "aiofiles>=24.1.0",
    "httpx>=0.27.0",
    "python-multipart>=0.0.12",
]
```

## 入口

```python
# run_web.py
import uvicorn
from web.app import app

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=8080, reload=True)
```
