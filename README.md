# Trawler

Multi-platform creator content trawler. Monitor Bilibili, Xiaohongshu, and Weibo for new content — auto download, transcribe, summarize, and push notifications.

<!-- README-I18N:START -->

**English** | [汉语](./README.zh.md)

<!-- README-I18N:END -->

## Features

- **Multi-platform monitoring** — Subscribe to Bilibili UP主 (RSS/API), Xiaohongshu bloggers, and Weibo users
- **Auto download** — Video download via yt-dlp, note/image download for Xiaohongshu
- **Speech-to-text** — SenseVoice (ModelScope) voice recognition with auto language detection
- **AI summarization** — AI-generated summaries and keyword extraction (CodeBuddy / OpenAI / Ollama / local fallback)
- **Push notifications** — Gotify-based notification with Markdown formatting
- **Comment highlights** — Extract top comments for Bilibili and Xiaohongshu
- **QR login** — Bilibili QR code authentication with auto token refresh
- **TOML config** — TOML-driven configuration with environment variable override
- **Dedup built-in** — JSON-backed set store prevents duplicate processing

## Quick Start

> **For AI agents:** Skip to the [Agent Installation Guide](#for-llm-agents).

### Prerequisites

- Python 3.12+
- FFmpeg (for audio extraction)
- Gotify server (optional, for push notifications)

### Installation

```bash
pip install trawler

# Optional: Xiaohongshu support
pip install trawler[xhs]
```

### Configuration

Create `config.toml` (see `config.toml.example`):

```toml
[bilibili]
  [bilibili.subscriptions]
  uid = 123456
  name = "UP主名称"
```

### Usage

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

## Architecture

```
trawler/
├── core/              # Orchestration layer
│   ├── pipeline.py    # Workflow pipeline
│   ├── notifier.py    # Gotify push notifications
│   ├── summarizer.py  # AI summary & keyword extraction
│   └── transcriber.py # Speech-to-text (SenseVoice)
├── platforms/
│   ├── bilibili/      # B站: auth, monitor, comments, dynamic, rss
│   └── xiaohongshu/   # 小红书: auth, monitor, comments, downloader, parser
├── shared/
│   ├── auth/          # Shared auth infrastructure (QR, token store, scheduler)
│   ├── config.py      # TOML-driven configuration
│   ├── protocols.py   # Data models & behavior contracts
│   ├── downloader.py  # Shared download utilities
│   └── http.py        # Shared aiohttp session
├── run_check.py       # CLI entry point (Click)
└── tests/             # Test suite (pytest, 8 modules)
```

### Pipeline

Each content item goes through a consistent pipeline:

```
Content Detected → Download → Transcribe → Summarize → Notify → Mark Done
                    ↕                    ↕
              Comment Highlights    Keyword Extraction
```

### Supported Platforms

| Platform | Content Type | Auth | Monitor Mode |
|---|---|---|---|
| Bilibili | Videos, Dynamics | QR login | RSS / API |
| Xiaohongshu | Notes | Cookie | API |
| Weibo | Posts | Cookie | API |

## For LLM Agents

### Repository Overview

Trawler is a Python 3.12 async project with this structure:

- **`run_check.py`** — Click CLI entry point with `login`, `token`, `check` commands
- **`core/`** — Orchestration logic (pipeline, notifier, summarizer, transcriber)
- **`platforms/`** — Platform adapters (bilibili/, xiaohongshu/)
- **`shared/`** — Config, data models (protocols.py), auth infrastructure, HTTP client
- **`tests/`** — pytest tests (8 modules, asyncio mode)

### Key Design Decisions

1. **Pure orchestration in `core/pipeline.py`** — no business logic, only wiring
2. **All cross-module contracts in `shared/protocols.py`** — dataclasses + Protocols
3. **TOML config with env override** — `Config` dataclass hierarchy, env vars take priority
4. **JsonSetStore for dedup** — `mark_known()` is memory-only, `save()` writes to disk
5. **AI fallback chain** — CodeBuddy → OpenAI → Ollama → local TF-IDF extraction
6. **RSS-first, API fallback** for Bilibili; API-only for Xiaohongshu/Weibo

### Development

```bash
ruff check .          # lint
ruff format .         # format
pyright .             # type check
pytest -x             # test (fail fast)
```

### Adding a New Platform

1. Create `platforms/<name>/__init__.py` (docstring only) + `auth.py` + `monitor.py` + `comments.py`
2. Add data models to `shared/protocols.py`
3. Add platform config dataclass to `shared/config.py`
4. Wire into `core/pipeline.py` + `run_check.py`

### Configuration Model

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

### Environment Variables

| Variable | Overrides |
|---|---|
| `FEEDFLOW_GOTIFY_URL` | Gotify server URL |
| `FEEDFLOW_GOTIFY_TOKEN_BILI` | Bilibili Gotify token |
| `FEEDFLOW_GOTIFY_TOKEN_XHS` | Xiaohongshu Gotify token |
| `FEEDFLOW_GOTIFY_TOKEN_WEIBO` | Weibo Gotify token |
| `FEEDFLOW_XHS_COOKIE` | Xiaohongshu cookie |
| `FEEDFLOW_WEIBO_COOKIE` | Weibo cookie |
| `FEEDFLOW_LLM_API_KEY` | LLM API key |
| `FEEDFLOW_LLM_API_BASE` | LLM API base URL |
