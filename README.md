# Trawler

多平台创作者内容追更自动化工作流。监控 B站、小红书、微博的内容更新，自动下载、语音转写、AI 总结并推送通知。

## 功能特性

- **多平台监控** — 订阅 B站 UP 主（视频+动态）、小红书博主、微博用户
- **自动下载** — 视频下载（bilibili-api + yt-dlp）、图文笔记下载
- **语音转写** — 基于 faster-whisper 的离线语音识别，自动语言检测
- **AI 摘要** — 自动生成内容摘要与关键词（CodeBuddy / OpenAI / Ollama / 本地回退）
- **评论高亮** — 提取热门评论同步展示
- **推送通知** — 通过 Gotify 发送 Markdown 格式通知
- **二维码登录** — B站/小红书 QR 扫码登录，Token 自动刷新
- **TOML 配置** — 统一配置文件，环境变量覆盖
- **去重机制** — JSON 持久化集合存储，避免重复处理

## 快速开始

### 前置依赖

- Python >=3.14
- FFmpeg（音频提取用）
- Gotify 服务端（可选，推送通知用）

### 安装

```bash
# 创建虚拟环境
uv venv --python 3.14

# 安装 trawler（含 dev 依赖）
uv pip install -e ".[dev]"

# 可选：小红书 API 支持
uv pip install -e ".[xhs]"

# 可选：语音转写支持
uv pip install -e ".[transcribe]"
```

### 配置

创建 `config.toml`（参考 `config.toml.example`）：

```toml
[bilibili]
  [bilibili.subscriptions]
  uid = 123456
  name = "UP主名称"
```

### 使用

```bash
# B站二维码登录
trawler login --platform bili

# 全平台内容检查
trawler check --platform all

# 指定平台
trawler check --platform bili
trawler check --platform xhs

# 查看登录状态
trawler token status
```

## 配置说明

配置采用 TOML 文件驱动，环境变量可覆盖同名字段。

**层级结构：**

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

**常用环境变量：**

| 变量 | 作用 |
|---|---|
| `FEEDFLOW_GOTIFY_URL` | Gotify 服务地址 |
| `FEEDFLOW_GOTIFY_TOKEN_BILI` | B站 Gotify Token |
| `FEEDFLOW_GOTIFY_TOKEN_XHS` | 小红书 Gotify Token |
| `FEEDFLOW_GOTIFY_TOKEN_WEIBO` | 微博 Gotify Token |
| `FEEDFLOW_XHS_COOKIE` | 小红书 Cookie |
| `FEEDFLOW_WEIBO_COOKIE` | 微博 Cookie |
| `FEEDFLOW_LLM_API_KEY` | LLM API 密钥 |
| `FEEDFLOW_LLM_API_BASE` | LLM API 地址 |

## 项目结构

```
trawler/
├── core/                 # 流程编排层
│   ├── pipeline.py       # 工作流管道
│   ├── transcriber.py    # 语音转写 (faster-whisper)
│   ├── summarizer.py     # AI 摘要与关键词
│   ├── formatter.py      # 消息格式化
│   └── notifier.py       # Gotify 推送通知
├── platforms/
│   ├── bilibili/         # B站: auth, monitor, comments, downloader
│   ├── xiaohongshu/      # 小红书: auth, monitor, comments, signer, downloader
│   └── weibo/            # 微博: auth, monitor, comments, downloader, API
├── shared/
│   ├── config.py         # TOML 配置驱动
│   ├── protocols.py      # 数据模型与行为契约 (dataclass + Protocol)
│   ├── downloader.py     # 共享下载工具
│   ├── http.py           # 共享 HTTP 客户端
│   └── auth/             # 统一认证基础设施 (QR 登录, Token 存储, 定时刷新)
├── run_check.py          # CLI 入口 (Click)
└── tests/                # 测试套件 (pytest, asyncio)
```

### 管道流程

```
检测到更新 → 下载内容 → 语音转写 → AI 摘要 → 推送通知 → 标记完成
                      ↕                    ↕
                评论高亮提取           关键词提取
```

### 支持平台

| 平台 | 内容类型 | 认证方式 | 监控方式 |
|---|---|---|---|
| Bilibili | 视频、动态 | 二维码登录 | RSS / API |
| 小红书 | 图文笔记 | Cookie / 二维码 | API |
| 微博 | 帖子 | Cookie | API |

## 开发

```bash
uv run ruff check .       # 代码检查
uv run ruff format .      # 格式化
uv run pyright .          # 类型检查
uv run pytest -x          # 测试（失败即停）
```

### 新增平台

1. 创建 `platforms/<name>/__init__.py`（仅 docstring）+ `auth.py` + `monitor.py` + `comments.py`
2. 在 `shared/protocols.py` 添加数据模型
3. 在 `shared/config.py` 添加平台配置 dataclass
4. 接入 `core/pipeline.py` + `run_check.py`

## License

MIT
