# Trawler

多平台创作者内容追更自动化工作流。监控 B站、小红书、微博的订阅内容，自动下载、转写、摘要并推送通知。

<!-- README-I18N:START -->

[English](./README.md) | **汉语**

<!-- README-I18N:END -->

## 功能特性

- **多平台监控** — 支持 B站（RSS/API）、小红书、微博的订阅检查
- **自动下载** — B站视频下载（yt-dlp）、小红书笔记/视频下载
- **语音转写** — 基于 SenseVoice 的自动语音识别，支持多语言
- **AI 摘要** — 自动生成内容摘要和关键词（支持 CodeBuddy / OpenAI / Ollama / 本地降级）
- **推送通知** — 基于 Gotify 的 Markdown 格式推送
- **评论亮点** — 提取 B站和小红书的热门评论
- **二维码登录** — B站扫码认证，支持自动续期
- **TOML 配置** — TOML 驱动配置，支持环境变量覆盖
- **去重机制** — 基于 JSON 的集合存储，避免重复处理

## 快速开始

> **AI 代理请看**：[开发者指南](#面向-llm-代理)

### 前置依赖

- Python 3.12+
- FFmpeg（用于音频提取）
- Gotify 服务器（可选，用于推送通知）

### 安装

```bash
pip install trawler

# 如需小红书支持
pip install trawler[xhs]
```

### 配置

创建 `config.toml`（参考 `config.toml.example`）：

```toml
[bilibili]
  [bilibili.subscriptions]
  uid = 123456
  name = "UP主名称"
```

### 用法

```bash
# 登录 B站（扫码）
trawler login --platform bili

# 检查新内容
trawler check

# 检查指定平台
trawler check --platform bili
trawler check --platform xhs

# 查看 Token 状态
trawler token status
```

## 架构

```
trawler/
├── core/              # 流程编排层
│   ├── pipeline.py    # 工作流流水线
│   ├── notifier.py    # Gotify 推送通知
│   ├── summarizer.py  # AI 摘要与关键词提取
│   └── transcriber.py # 语音转写（SenseVoice）
├── platforms/
│   ├── bilibili/      # B站：认证、监控、评论、动态、RSS
│   └── xiaohongshu/   # 小红书：认证、监控、评论、下载、解析
├── shared/
│   ├── auth/          # 公共认证基础设施（QR、Token 存储、调度器）
│   ├── config.py      # TOML 驱动配置
│   ├── protocols.py   # 数据模型与行为契约
│   ├── downloader.py  # 公共下载工具
│   └── http.py        # 公共 aiohttp 会话
├── run_check.py       # CLI 入口（Click）
└── tests/             # 测试套件（pytest，8 个模块）
```

### 数据流水线

每个内容项经过一致的流水线处理：

```
检测到内容 → 下载 → 转写 → 摘要 → 通知 → 标记完成
                ↕                ↕
         评论亮点提取       关键词提取
```

### 支持平台

| 平台 | 内容类型 | 认证方式 | 监控模式 |
|---|---|---|---|
| B站 | 视频、动态 | 扫码登录 | RSS / API |
| 小红书 | 笔记 | Cookie | API |
| 微博 | 帖子 | Cookie | API |

## 面向 LLM 代理

### 仓库概览

Trawler 是一个 Python 3.12 异步项目，结构如下：

- **`run_check.py`** — Click CLI 入口，包含 `login`、`token`、`check` 命令
- **`core/`** — 编排逻辑（流水线、通知、摘要、转写）
- **`platforms/`** — 平台适配层（bilibili/、xiaohongshu/）
- **`shared/`** — 配置、数据模型（protocols.py）、认证基础设施、HTTP 客户端
- **`tests/`** — pytest 测试（8 个模块，asyncio 模式）

### 关键设计决策

1. **纯编排 in `core/pipeline.py`** — 不含业务逻辑，只做流程编排
2. **所有跨模块契约 in `shared/protocols.py`** — dataclass + Protocol
3. **TOML 配置 + 环境变量覆盖** — `Config` dataclass 层级结构，环境变量优先
4. **JsonSetStore 去重** — `mark_known()` 仅内存操作，`save()` 写磁盘
5. **AI 降级链** — CodeBuddy → OpenAI → Ollama → 本地 TF-IDF 提取
6. **B站 RSS 优先、API 降级**；小红书/微博仅 API

### 开发

```bash
ruff check .          # 代码检查
ruff format .         # 代码格式化
pyright .             # 类型检查
pytest -x             # 测试（失败即停）
```

### 新增平台

1. 创建 `platforms/<name>/__init__.py`（仅 docstring）+ `auth.py` + `monitor.py` + `comments.py`
2. 在 `shared/protocols.py` 添加数据模型
3. 在 `shared/config.py` 添加平台配置 dataclass
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

| 变量 | 覆盖项 |
|---|---|
| `FEEDFLOW_GOTIFY_URL` | Gotify 服务器地址 |
| `FEEDFLOW_GOTIFY_TOKEN_BILI` | B站 Gotify Token |
| `FEEDFLOW_GOTIFY_TOKEN_XHS` | 小红书 Gotify Token |
| `FEEDFLOW_GOTIFY_TOKEN_WEIBO` | 微博 Gotify Token |
| `FEEDFLOW_XHS_COOKIE` | 小红书 Cookie |
| `FEEDFLOW_WEIBO_COOKIE` | 微博 Cookie |
| `FEEDFLOW_LLM_API_KEY` | LLM API 密钥 |
| `FEEDFLOW_LLM_API_BASE` | LLM API 地址 |
