# Trawler

多平台创作者内容抓取工具。监控 Bilibili、Xiaohongshu、Weibo 的新内容 —— 自动下载、转写、总结，并推送通知。

<!-- README-I18N:START -->

[English](./README.md) | **汉语**

<!-- README-I18N:END -->

## 功能特性

- **多平台监控** —— 订阅 Bilibili UP主（RSS/API）、Xiaohongshu 博主、Weibo 用户
- **自动下载** —— 通过 bilibili_api 下载视频/音频，Xiaohongshu 笔记/图片下载
- **语音转写** —— 基于 faster-whisper 的语音识别，自动检测语言
- **AI 总结** —— AI 生成摘要与关键词提取（OpenAI / Ollama / 本地兜底，可通过 Web UI 配置）
- **推送通知** —— 基于 Gotify 的通知，支持 Markdown，可扇出到多个端点
- **Web UI** —— FastAPI + HTMX 仪表盘（监控、检查、日志、设置、认证）
- **评论精选** —— 提取 Bilibili 与 Xiaohongshu 的热门评论
- **QR 登录** —— Bilibili 二维码登录，自动刷新 token
- **TOML 配置** —— TOML 驱动的配置，支持环境变量覆盖
- **内置去重** —— 基于 JSON 的集合存储，防止重复处理

## 快速开始

> **给 AI agent：** 直接跳到 [Agent 安装指南](#给-llm-agent-看)。

### 前置条件

- Python 3.14+
- FFmpeg（用于音频提取）
- Gotify 服务器（可选，用于推送通知）

### 安装

```bash
# Install with dev tools (recommended)
uv venv --python 3.14
uv pip install -e ".[dev]"

# Optional: Xiaohongshu support
uv pip install -e ".[xhs]"
```

### 配置

把 `config/config.toml.example` 复制为 `config/config.toml` 并编辑：

```toml
[general]
data_dir = "./data"

[download]
dir = "./downloads"
quality = "worst"
max_concurrent = 3

[transcribe]
model = "base"
language = "zh"

[analysis]
enabled = true

[bilibili.monitor]
mode = "rss"
watch_dynamic = true

[xiaohongshu]
enabled = false

[weibo]
enabled = false
```

### 使用

```bash
# Login to Bilibili (QR code)
trawler login --platform bili

# Check for new content
trawler check

# Check specific platform
trawler check --platform bili
trawler check --platform xhs

# Check token status
trawler token status
```

### 定时调度

Trawler 没有内置调度器 —— 检查由外部 cron 通过
[`cron_run.sh`](cron_run.sh) 驱动。例如：

```bash
# Run a check every 3 minutes
*/3 * * * * /path/to/trawler/cron_run.sh >> /path/to/trawler/cron.log 2>&1
```

### HTTP API

Trawler 还对外暴露面向机器的 JSON HTTP API（`/api/v1`），方便外部
bot / 脚本 / 自动化触发检查、查询消息、批量重跑、管理订阅 —— 无需依赖 cron
或 Web UI 点击。该 API 与 Web UI 共用同一个 FastAPI server（同端口），但
使用 Bearer token 鉴权。

完整端点参考、鉴权设置、curl 示例见 [HTTP API 文档](docs/api.md)。

## 架构

```
trawler/
├── core/              # Orchestration layer
│   ├── pipeline.py    # Workflow pipeline
│   ├── comments.py    # Cross-platform comment highlights
│   ├── engine.py      # Check engine (scheduler)
│   ├── formatter.py   # Notification message formatting
│   ├── notifiers/     # Push notification providers
│   │   ├── gotify.py  #   Gotify notifier
│   │   ├── telegram.py#   Telegram (stub)
│   │   └── email.py   #   Email (stub)
│   ├── summarizer.py  # AI summary & keyword extraction
│   ├── transcriber.py # Speech-to-text (faster-whisper)
│   └── subscription_cli.py # CLI subscription management
├── platforms/
│   ├── bilibili/      # B站: auth, monitor, comments, dynamic
│   ├── xiaohongshu/   # 小红书: auth, monitor, comments, downloader, parser
│   └── weibo/         # 微博: auth, monitor, comments, downloader, parser
├── shared/
│   ├── auth/          # Shared auth infrastructure (QR, token store, scheduler)
│   ├── config.py      # TOML-driven configuration
│   ├── protocols.py   # Data models & behavior contracts
│   ├── downloader.py  # Shared download utilities
│   └── http.py        # Shared aiohttp session
├── web/               # Web UI (FastAPI + HTMX + Jinja2)
│   ├── app.py         # FastAPI application
│   └── routes/        # Dashboard, check, settings, auth, endpoints, logs, subscriptions
├── run_check.py       # CLI entry point (Click)
└── tests/             # Test suite (pytest, 408+ tests)
```

### 流水线

每条内容都经过一致的流水线：

```
Content Detected → Download → Transcribe → Summarize → Notify → Mark Done
                    ↕                    ↕
              Comment Highlights    Keyword Extraction
```

### 支持平台

| 平台 | 内容类型 | 认证 | 监控方式 |
|---|---|---|---|
| Bilibili | 视频、动态 | QR 登录 | RSS / API |
| Xiaohongshu | 笔记 | Cookie | API |
| Weibo | 帖子 | Cookie | API |

## 给 LLM Agent 看

### 仓库概览

Trawler 是 Python 3.14 异步项目（uv 管理），结构如下：

- **`run_check.py`** —— Click CLI 入口，包含 `login`、`token`、`check` 命令
- **`core/`** —— 编排逻辑（pipeline、notifier、summarizer、transcriber）
- **`platforms/`** —— 平台适配器（bilibili/、xiaohongshu/、weibo/）
- **`shared/`** —— 配置、数据模型（protocols.py）、认证基础设施、HTTP client
- **`tests/`** —— pytest 测试（asyncio 模式）

### 关键设计决策

1. **`core/pipeline.py` 只做编排** —— 没有业务逻辑，只有装配
2. **所有跨模块契约都放在 `shared/protocols.py`** —— dataclass + Protocol
3. **TOML 配置 + 环境变量覆盖** —— `Config` dataclass 层级结构，环境变量优先
4. **用 MessageStore 管状态** —— `mark_phase()` 只在内存，`save()` 才落盘
5. **AI 兜底链** —— OpenAI → Ollama → 本地 TF-IDF 提取
6. **Bilibili 以 RSS 优先、API 兜底**；Xiaohongshu/Weibo 只用 API
7. **HTMX 驱动的 Web UI** —— FastAPI + HTMX + Jinja2，无需 JS 框架即可内联编辑

### 开发

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyright               # type check (uses include paths)
uv run pytest -x             # test (fail fast)
```

### 新增平台

1. 创建 `platforms/<name>/__init__.py`（仅 docstring）+ `auth.py` + `monitor.py` + `comments.py`
2. 把数据模型加到 `shared/protocols.py`
3. 在 `shared/config.py` 加平台配置 dataclass
4. 接入 `core/pipeline.py` + `run_check.py`

### 配置模型

```
Config
├── general (data_dir)
├── auth.renewal (min_interval, force_before)
├── download (dir, quality, format, max_concurrent)
├── transcribe (model, language, output_dir)
├── analysis (enabled, provider, api_base, api_key)
├── bilibili (auth, monitor, subscriptions, notification)
├── xiaohongshu (enabled, auth, monitor, subscriptions, notification)
└── weibo (enabled, auth, monitor, subscriptions, notification)
```

### 环境变量

| 变量 | 覆盖内容 |
|---|---|
| `TRAWLER_BILI_SESSDATA` | Bilibili session cookie |
| `TRAWLER_BILI_REFRESH_TOKEN` | Bilibili refresh token |
| `TRAWLER_BILI_JCT` | Bilibili CSRF token (bili_jct) |
| `TRAWLER_BILI_BUVID3` | Bilibili device id (buvid3) |
| `TRAWLER_BILI_DEDEUSERID` | Bilibili user id (DedeUserID) |
| `TRAWLER_XHS_COOKIE` | Xiaohongshu cookie |
| `TRAWLER_WEIBO_COOKIE` | Weibo cookie |
| `TRAWLER_LLM_API_KEY` | LLM API key |
| `TRAWLER_LLM_API_BASE` | LLM API base URL |
| `TRAWLER_LLM_MODEL` | LLM model name |
| `TRAWLER_LLM_PROVIDER` | LLM provider (openai / ollama) |
