# Trawler

Multi-platform creator content trawler. Monitor Bilibili, Xiaohongshu, and Weibo for new content — auto download, transcribe, summarize, and push notifications.

<!-- README-I18N:START -->

**English** | [汉语](./README.zh.md)

<!-- README-I18N:END -->

## Features

- **Multi-platform monitoring** — Subscribe to Bilibili UP主 (RSS/API), Xiaohongshu bloggers, and Weibo users
- **Auto download** — Video/audio download via bilibili_api, note/image download for Xiaohongshu
- **Speech-to-text** — faster-whisper voice recognition with auto language detection
- **AI summarization** — AI-generated summaries and keyword extraction (OpenAI / Ollama / local fallback, configurable via Web UI)
- **Push notifications** — Gotify-based notification with Markdown, multi-endpoint fan-out
- **Web UI** — FastAPI + HTMX dashboard (monitor, check, logs, settings, auth)
- **Comment highlights** — Extract top comments for Bilibili and Xiaohongshu
- **QR login** — Bilibili QR code authentication with auto token refresh
- **TOML config** — TOML-driven configuration with environment variable override
- **Dedup built-in** — JSON-backed set store prevents duplicate processing

## Quick Start

> **For AI agents:** Skip to the [Agent Installation Guide](#for-llm-agents).

### Prerequisites

- Python 3.14+
- FFmpeg (for audio extraction)
- Gotify server (optional, for push notifications)

### Installation

```bash
# Install with dev tools (recommended)
uv venv --python 3.14
uv pip install -e ".[dev]"

# Optional: Xiaohongshu support
uv pip install -e ".[xhs]"
```

### Configuration

Copy `config/config.toml.example` to `config/config.toml` and edit:

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
interval_minutes = 3
watch_dynamic = true

[xiaohongshu]
enabled = false

[weibo]
enabled = false
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

Trawler is a Python 3.14 async project (uv-managed) with this structure:

- **`run_check.py`** — Click CLI entry point with `login`, `token`, `check` commands
- **`core/`** — Orchestration logic (pipeline, notifier, summarizer, transcriber)
- **`platforms/`** — Platform adapters (bilibili/, xiaohongshu/, weibo/)
- **`shared/`** — Config, data models (protocols.py), auth infrastructure, HTTP client
- **`tests/`** — pytest tests (asyncio mode)

### Key Design Decisions

1. **Pure orchestration in `core/pipeline.py`** — no business logic, only wiring
2. **All cross-module contracts in `shared/protocols.py`** — dataclasses + Protocols
3. **TOML config with env override** — `Config` dataclass hierarchy, env vars take priority
4. **JsonSetStore for dedup** — `mark_known()` is memory-only, `save()` writes to disk
5. **AI fallback chain** — OpenAI → Ollama → local TF-IDF extraction
6. **RSS-first, API fallback** for Bilibili; API-only for Xiaohongshu/Weibo
7. **HTMX-driven Web UI** — FastAPI + HTMX + Jinja2, inline editing without JS frameworks

### Development

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyright               # type check (uses include paths)
uv run pytest -x             # test (fail fast)
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
| `TRAWLER_BILI_SESSDATA` | Bilibili session cookie |
| `TRAWLER_BILI_REFRESH_TOKEN` | Bilibili refresh token |
| `TRAWLER_XHS_COOKIE` | Xiaohongshu cookie |
| `TRAWLER_XHS_DOWNLOADER_API` | Xiaohongshu download API endpoint |
| `TRAWLER_WEIBO_COOKIE` | Weibo cookie |
| `TRAWLER_LLM_API_KEY` | LLM API key |
| `TRAWLER_LLM_API_BASE` | LLM API base URL |
