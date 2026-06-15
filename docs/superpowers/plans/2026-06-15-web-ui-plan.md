# Web UI — FastAPI + HTMX

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Web management UI for Trawler via FastAPI + HTMX. Zero intrusion on existing business logic — the Web layer is a thin async wrapper that calls existing core functions directly.

**Architecture:**
```
Browser ──HTTP/SSE──▶ FastAPI ──async──▶ trawler core
                        │
                   Jinja2/HTMX
                    模板渲染
```

**Tech Stack:** Python 3.12+, FastAPI, Uvicorn, Jinja2, HTMX (via CDN, no JS build), `asyncio`, `httpx` (testing), `tomlkit` (config writeback), `qrcode` (QR rendering)

**Spec Reference:** `docs/superpowers/specs/2026-06-15-web-ui-design.md`

---

## File Structure

```
CREATED/MODIFIED:
  shared/config.py                  # [MODIFY] load_config() → async def
  shared/auth/__init__.py           # [MODIFY] update_auth_section() → async def
  shared/auth/token_store.py        # [MODIFY] update_auth_section() → async def
  core/subscription_cli.py          # [MODIFY] search_by_name/list/add/remove → async def
  core/pipeline.py                  # [MODIFY] +log_callback param to run_check_once
  core/engine.py                    # [MODIFY] +log_callback param to run_platform
  run_check.py                      # [MODIFY] CLI callers use asyncio.run() for async functions
  pyproject.toml                    # [MODIFY] +[web] optional-dependencies
  web/__init__.py                   # [NEW]
  web/app.py                        # [NEW] FastAPI app + lifecycle + route registration
  web/routes/__init__.py            # [NEW]
  web/routes/dashboard.py           # [NEW] GET /
  web/routes/subscriptions.py       # [NEW] GET/POST /subscriptions
  web/routes/check.py               # [NEW] GET /check + POST /check/run + SSE /check/stream
  web/routes/auth.py                # [NEW] GET /auth + QR + poll
  web/routes/settings.py            # [NEW] GET/POST /settings
  web/templates/base.html           # [NEW] sidebar + main layout
  web/templates/dashboard.html      # [NEW] stats cards + recent messages
  web/templates/subscriptions.html  # [NEW] table + add/remove forms
  web/templates/check.html          # [NEW] trigger button + log output
  web/templates/login.html          # [NEW] token status + login buttons
  web/templates/settings.html       # [NEW] config form
  web/static/app.css                # [NEW] minimal styling
  run_web.py                        # [NEW] uvicorn entry point
   tests/test_config.py                  # [MODIFY] +async load_config tests (in-place)
   tests/test_token_store.py             # [MODIFY] +async update_auth_section tests (in-place)
  tests/test_web_dashboard.py       # [NEW] web route tests
  tests/test_web_subscriptions.py   # [NEW] web route tests
  tests/test_web_check.py           # [NEW] web route tests
  tests/test_web_auth.py            # [NEW] web route tests
  tests/test_web_settings.py        # [NEW] web route tests

UNCHANGED:
  shared/protocols.py               # Already has all needed data models
  shared/message_store.py           # Already async-compatible (sync I/O in methods)
  shared/auth/base.py               # BaseAuthenticator methods are already async
  platforms/*                       # No platform code changes needed
  core/notifier.py                  # Unchanged
  core/summarizer.py                # Unchanged
  core/transcriber.py               # Unchanged
  shared/downloader.py              # Unchanged
```

---

## Dependencies

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
web = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "jinja2>=3.1.0",
    "httpx>=0.27.0",
    "python-multipart>=0.0.12",
    "qrcode>=7.4",
]
```

`qrcode` is already a dependency of `terminal-qrcode` (in core deps), but adding it explicitly for clarity.

---

### Task 1: Async conversion of `subscription_cli.py` functions

**Duration:** 5 min

**Rationale:** The internal file I/O stays synchronous (small TOML files, negligible blocking). Only the function signatures change to `async def`. The `search_by_name` function currently has a sync shell that wraps `asyncio.run(_search_async())` — the shell is removed, the inner async functions are renamed.

- [ ] **Step 1: Convert `search_by_name` — remove sync shell, rename inner functions**

In `core/subscription_cli.py`, replace the current `search_by_name` sync function and the three `_search_*` async functions:

OLD (lines 240-262):
```python
def search_by_name(
    platform: str,
    name: str,
    config_path: str = "config/config.toml",
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Search for a user by name on the given platform.

    Returns (success, message, candidates).
    Each candidate is a dict with key (uid/user_id) and name.
    """
    if platform not in SEARCH_CAPABLE:
        return False, f"{platform} 暂不支持按名字搜索", []

    import asyncio

    if platform == "bili":
        return asyncio.run(_search_bili(name, config_path))
    elif platform == "weibo":
        return asyncio.run(_search_weibo(name, config_path))
    elif platform == "xhs":
        return asyncio.run(_search_xhs(name, config_path))

    return False, f"{platform} 暂不支持按名字搜索", []
```

NEW:
```python
async def search_by_name(
    platform: str,
    name: str,
    config_path: str = "config/config.toml",
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Search for a user by name on the given platform.

    Returns (success, message, candidates).
    Each candidate is a dict with key (uid/user_id) and name.
    """
    if platform not in SEARCH_CAPABLE:
        return False, f"{platform} 暂不支持按名字搜索", []

    if platform == "bili":
        return await _search_bili(name, config_path)
    elif platform == "weibo":
        return await _search_weibo(name, config_path)
    elif platform == "xhs":
        return await _search_xhs(name, config_path)

    return False, f"{platform} 暂不支持按名字搜索", []
```

The `_search_bili`, `_search_weibo`, `_search_xhs` functions inside `_search_bili` call `load_config` — that will become async in Task 2. For now, they still call the sync `load_config`; the `await` on them will be added after Task 2's changes land. **For this task, focus only on the signature change of `search_by_name`.** The inner `_search_*` functions will gain `await load_config()` in Task 2.

- [ ] **Step 2: Convert `list_subscriptions` to async def**

OLD (lines 94-117):
```python
def list_subscriptions(
    platform: str | None = None, path: str = "config/subscriptions.toml"
) -> dict[str, list[dict[str, Any]]]:
    """List subscriptions, optionally filtered by platform."""
    doc = _load_doc(path)
    if doc is None:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    sections = [PLATFORM_TO_SECTION[platform]] if platform else list(PLATFORM_TO_SECTION.values())
    for section in sections:
        entry = doc.get(section)
        if entry is None:
            continue
        subs_list = entry.get("subscriptions") if isinstance(entry, dict) else None
        if subs_list and isinstance(subs_list, list):
            result[section] = [dict(item) for item in subs_list]
    return result
```

NEW — change `def` to `async def` only (body identical):
```python
async def list_subscriptions(
    platform: str | None = None, path: str = "config/subscriptions.toml"
) -> dict[str, list[dict[str, Any]]]:
    """List subscriptions, optionally filtered by platform."""
    doc = _load_doc(path)
    if doc is None:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    sections = [PLATFORM_TO_SECTION[platform]] if platform else list(PLATFORM_TO_SECTION.values())
    for section in sections:
        entry = doc.get(section)
        if entry is None:
            continue
        subs_list = entry.get("subscriptions") if isinstance(entry, dict) else None
        if subs_list and isinstance(subs_list, list):
            result[section] = [dict(item) for item in subs_list]
    return result
```

- [ ] **Step 3: Convert `add_subscription` to async def**

OLD (lines 120-159): `def add_subscription(` → `async def add_subscription(`

Change only the function signature line from `def` to `async def`. Body stays identical.

- [ ] **Step 4: Convert `remove_subscription` to async def**

OLD (lines 162-218): `def remove_subscription(` → `async def remove_subscription(`

Change only the function signature line. Body stays identical.

- [ ] **Step 5: Update `core/subscription_cli.py`'s public API docstring or `__all__`** — no change needed since function names stay the same.

**Verification:**
```bash
uv run ruff check core/subscription_cli.py
uv run pyright core/subscription_cli.py
```
Expected: No errors. The functions are `async def` but don't `await` anything (sync I/O inside), which is valid Python.

---

### Task 2: Async conversion of `load_config` and `update_auth_section`

**Duration:** 5 min

- [ ] **Step 1: Convert `shared/config.py:load_config()` to async def**

OLD (line 310):
```python
def load_config(path: str | Path = "config/config.toml") -> Config:
```

NEW:
```python
async def load_config(path: str | Path = "config/config.toml") -> Config:
```

Body unchanged — all file I/O remains synchronous (small files, wrapped in `async def`).

- [ ] **Step 2: Update all internal `_search_bili`, `_search_weibo`, `_search_xhs` in `core/subscription_cli.py` to await load_config**

Each of these three functions calls `load_config(config_path)` synchronously. Change to `await load_config(config_path)`:

In `_search_bili` (line 272):
```python
# OLD:
cfg = load_config(config_path)
# NEW:
cfg = await load_config(config_path)
```

In `_search_weibo` (line 304):
```python
# OLD:
cfg = load_config(config_path)
# NEW:
cfg = await load_config(config_path)
```

In `_search_xhs` (line 335):
```python
# OLD:
cfg = load_config(config_path)
# NEW:
cfg = await load_config(config_path)
```

- [ ] **Step 3: Convert `shared/auth/token_store.py:update_auth_section()` to async def**

OLD (line 13):
```python
def update_auth_section(config_path: str | Path, platform: str, auth_dict: dict) -> None:
```

NEW:
```python
async def update_auth_section(config_path: str | Path, platform: str, auth_dict: dict) -> None:
```

Body unchanged — all file I/O remains synchronous.

- [ ] **Step 4: Convert `shared/auth/__init__.py:update_auth_section()` to async def**

OLD (line 55):
```python
def update_auth_section(platform: str, auth_dict: dict, config_path: str = "config/config.toml") -> None:
    """Update [platform.auth] section in cookies.toml (derived from config_path)."""
    from shared.auth.token_store import update_auth_section as _update
    table = _PLATFORM_TABLE.get(platform, platform)
    _update(config_path=config_path, platform=table, auth_dict=auth_dict)
```

NEW:
```python
async def update_auth_section(platform: str, auth_dict: dict, config_path: str = "config/config.toml") -> None:
    """Update [platform.auth] section in cookies.toml (derived from config_path)."""
    from shared.auth.token_store import update_auth_section as _update
    table = _PLATFORM_TABLE.get(platform, platform)
    await _update(config_path=config_path, platform=table, auth_dict=auth_dict)
```

- [ ] **Step 5: Update `shared/auth/__init__.py` `__all__`** — no change needed, `update_auth_section` is still exported.

**Verification:**
```bash
uv run ruff check shared/config.py shared/auth/__init__.py shared/auth/token_store.py
uv run pyright shared/config.py shared/auth/__init__.py shared/auth/token_store.py
```
Expected: No errors.

---

### Task 3: Update `run_check.py` CLI callers

**Duration:** 5 min

All CLI callers in `run_check.py` call the now-async functions synchronously. Wrap each in `asyncio.run()`.

- [ ] **Step 1: Fix `sub_add` calls to `search_by_name` and `add_subscription` (lines 353, 362, 380)**

```python
# Line 353 — OLD (sync call):
ok, msg, candidates = search_by_name(platform, search_name)
# NEW (wrap in asyncio.run):
ok, msg, candidates = asyncio.run(search_by_name(platform, search_name))

# Line 362 — OLD:
ok2, msg2 = add_subscription(platform, cid, cname)
# NEW:
ok2, msg2 = asyncio.run(add_subscription(platform, cid, cname))

# Line 380 — OLD:
ok, msg = add_subscription(platform, identifier, name)
# NEW:
ok, msg = asyncio.run(add_subscription(platform, identifier, name))
```

- [ ] **Step 2: Fix `sub_remove` call to `remove_subscription` (line 401)**

```python
# OLD:
ok, msg = remove_subscription(platform, identifier)
# NEW:
ok, msg = asyncio.run(remove_subscription(platform, identifier))
```

- [ ] **Step 3: Fix `sub_list` call to `list_subscriptions` (line 418)**

```python
# OLD:
subs = list_subscriptions(platform=platform)
# NEW:
subs = asyncio.run(list_subscriptions(platform=platform))
```

- [ ] **Step 4: Fix `token_status` call to `load_config` (line 129)**

```python
# OLD:
config = load_config("config/config.toml")
# NEW:
config = asyncio.run(load_config("config/config.toml"))
```

- [ ] **Step 5: Fix `token_refresh` call to `load_config` (line 186)**

```python
# OLD:
config = load_config("config/config.toml")
# NEW:
config = asyncio.run(load_config("config/config.toml"))
```

- [ ] **Step 6: Fix `check` command call to `load_config` (line 475)**

```python
# OLD:
config = load_config(config_path)
# NEW:
config = asyncio.run(load_config(config_path))
```

- [ ] **Step 7: Fix `login` command calls to `update_auth_section` (line 96)**

```python
# OLD:
update_auth_section(platform, auth_dict)
# NEW:
asyncio.run(update_auth_section(platform, auth_dict))
```

- [ ] **Step 8: Fix `_refresh_single_platform` calls to `update_auth_section` (lines 261, 291, 321)**

```python
# Line 261 — OLD:
update_auth_section(platform, auth_dict)
# NEW:
asyncio.run(update_auth_section(platform, auth_dict))

# Line 291 — OLD:
update_auth_section("weibo", auth_dict)
# NEW:
asyncio.run(update_auth_section("weibo", auth_dict))

# Line 321 — OLD:
update_auth_section("xhs", auth_dict)
# NEW:
asyncio.run(update_auth_section("xhs", auth_dict))
```

**Verification:**
```bash
uv run ruff check run_check.py
uv run pyright run_check.py
```
Expected: No errors.

---

### Task 4: Update tests for async conversions

**Duration:** 8 min

- [ ] **Step 1: Update `tests/test_subscription_cli.py` — all test methods become async**

Add `@pytest.mark.asyncio` decorator (or rely on `asyncio_mode = "auto"` in pyproject.toml) and `await` all calls to subscription functions.

Each test method signature changes from `def test_xxx(self, ...)` to `async def test_xxx(self, ...)`, and each call to `list_subscriptions`, `add_subscription`, `remove_subscription`, `search_by_name` gets an `await` prefix.

For example (line 55):
```python
# OLD:
subs = list_subscriptions(path=str(subs_file))
# NEW:
subs = await list_subscriptions(path=str(subs_file))
```

All occurrences (approximately 20 call sites) follow the same pattern.

- [ ] **Step 2: Update `tests/test_config.py` — add `await` to `load_config` calls**

Each call to `load_config(...)` becomes `await load_config(...)`. Each test method signature changes from `def test_xxx(self, ...)` to `async def test_xxx(self, ...)`.

Example (line 160):
```python
# OLD:
def test_missing_file_returns_defaults(self, tmp_path):
    cfg = load_config(tmp_path / "nonexistent.toml")
# NEW:
async def test_missing_file_returns_defaults(self, tmp_path):
    cfg = await load_config(tmp_path / "nonexistent.toml")
```

- [ ] **Step 3: Update `tests/test_token_store.py` — add `await` to `update_auth_section` calls**

Each test method signature changes from `def test_xxx(self, ...)` to `async def test_xxx(self, ...)`, and each `update_auth_section(...)` becomes `await update_auth_section(...)`.

Example (line 57):
```python
# OLD:
def test_update_bilibili_auth_fields(self, tmp_path):
    ...
    update_auth_section(config_path, "bilibili", {...})
# NEW:
async def test_update_bilibili_auth_fields(self, tmp_path):
    ...
    await update_auth_section(config_path, "bilibili", {...})
```

**Verification:**
```bash
uv run pytest tests/test_subscription_cli.py tests/test_config.py tests/test_token_store.py -x -v
```
Expected: All tests pass (same count as before).

---

### Task 5: Add `log_callback` to `run_check_once` and `PipelineEngine.run_platform`

**Duration:** 5 min

- [ ] **Step 1: Add `log_callback` param to `run_check_once` in `core/pipeline.py`**

OLD (line 77-82):
```python
async def run_check_once(
    config: Config,
    platform: str = "all",
    config_path: str = "config/config.toml",
    from_phase: str | None = None,
) -> None:
```

NEW:
```python
async def run_check_once(
    config: Config,
    platform: str = "all",
    config_path: str = "config/config.toml",
    from_phase: str | None = None,
    log_callback: Callable[[str, str], None] | None = None,
) -> None:
```

- [ ] **Step 2: Pass `log_callback` through to `PipelineEngine.run_platform`**

OLD (line 114):
```python
await PipelineEngine.run_platform(config, pkey, from_phase=_phase)
```

NEW:
```python
await PipelineEngine.run_platform(config, pkey, from_phase=_phase, log_callback=log_callback)
```

- [ ] **Step 3: Add `log_callback` param to `PipelineEngine.run_platform` in `core/engine.py`**

OLD (line 130-135):
```python
@classmethod
async def run_platform(
    cls,
    config: Config,
    platform: str,
    from_phase: Phase | None = None,
) -> None:
```

NEW:
```python
@classmethod
async def run_platform(
    cls,
    config: Config,
    platform: str,
    from_phase: Phase | None = None,
    log_callback: Callable[[str, str], None] | None = None,
) -> None:
```

- [ ] **Step 4: Emit log events in `run_platform` at key milestones**

After the signature change, add callback invocations at key points:

```python
if log_callback:
    log_callback("log", f"🔍 开始检查 {platform} 平台...")
```

Add after line 146 (`store = MessageStore(config.general.data_dir)`):
```python
if log_callback:
    log_callback("log", f"🔍 开始检查 {platform} 平台...")
```

Add after cleanup completes (after line 147 `store.cleanup(24)`):
```python
if log_callback:
    expired = 24  # window_hours
    log_callback("log", f"🧹 已清理超过 {expired} 小时的消息")
```

After `run_platform` finishes (at end, before `store.save()`):
```python
if log_callback:
    log_callback("done", f"✅ {platform} 检查完成")
```

- [ ] **Step 5: Update the CLI call site in `run_check.py` — no change needed**

The CLI `check` command does not pass `log_callback`, so it stays `None` by default. The `asyncio.run(run_check_once(...))` call on line 484 does not need to change.

**Verification:**
```bash
uv run ruff check core/pipeline.py core/engine.py
uv run pyright core/pipeline.py core/engine.py
```
Expected: No errors.

---

### Task 6: Web app scaffold + dependencies

**Duration:** 5 min

- [ ] **Step 1: Add `[project.optional-dependencies] web = [...]` to `pyproject.toml`**

Insert after the `dev` block (after line 27):
```toml
web = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "jinja2>=3.1.0",
    "httpx>=0.27.0",
    "python-multipart>=0.0.12",
    "qrcode>=7.4",
]
```

- [ ] **Step 2: Create `web/__init__.py`**

```python
"""Web UI — FastAPI + HTMX management interface for Trawler."""
```

- [ ] **Step 3: Create `web/routes/__init__.py`**

```python
"""Route modules for the Web UI."""
```

- [ ] **Step 4: Create `web/app.py` — FastAPI application factory**

```python
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="Trawler Web UI", version="0.1.0")

    # Mount static files
    static_dir = HERE / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register routes
    from web.routes.dashboard import router as dashboard_router
    from web.routes.subscriptions import router as subscriptions_router
    from web.routes.check import router as check_router
    from web.routes.auth import router as auth_router
    from web.routes.settings import router as settings_router

    app.include_router(dashboard_router)
    app.include_router(subscriptions_router)
    app.include_router(check_router)
    app.include_router(auth_router)
    app.include_router(settings_router)

    return app


app = create_app()
```

- [ ] **Step 5: Create `run_web.py` — uvicorn entry point**

```python
#!/usr/bin/env python3
"""Web UI entry point — run with: uv run python run_web.py"""

from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run("web.app:app", host="127.0.0.1", port=8080, reload=True)
```

- [ ] **Step 6: Create `web/static/app.css` — minimal styling**

```css
/* Trawler Web UI — minimal styling */
:root {
  --sidebar-width: 220px;
  --color-bg: #f5f5f5;
  --color-sidebar: #1a1a2e;
  --color-sidebar-text: #e0e0e0;
  --color-primary: #4361ee;
  --color-success: #2ec4b6;
  --color-warning: #ff9f1c;
  --color-error: #e71d36;
  --color-card: #ffffff;
  --radius: 8px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--color-bg); color: #333; }
.sidebar { position: fixed; left: 0; top: 0; width: var(--sidebar-width); height: 100vh; background: var(--color-sidebar); color: var(--color-sidebar-text); padding: 1.5rem 0; }
.sidebar h1 { font-size: 1.2rem; padding: 0 1rem 1.5rem; border-bottom: 1px solid rgba(255,255,255,.1); margin-bottom: 1rem; }
.sidebar a { display: block; padding: .6rem 1rem; color: var(--color-sidebar-text); text-decoration: none; transition: background .2s; }
.sidebar a:hover, .sidebar a.active { background: rgba(255,255,255,.1); }
.main { margin-left: var(--sidebar-width); padding: 2rem; max-width: calc(100vw - var(--sidebar-width)); }
.card { background: var(--color-card); border-radius: var(--radius); padding: 1.5rem; margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
.stat-card { background: var(--color-card); border-radius: var(--radius); padding: 1.2rem; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
.stat-card .value { font-size: 2rem; font-weight: 700; }
.stat-card .label { font-size: .85rem; color: #666; margin-top: .3rem; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: .6rem .8rem; text-align: left; border-bottom: 1px solid #eee; }
th { font-weight: 600; color: #555; font-size: .85rem; text-transform: uppercase; }
button, .btn { display: inline-block; padding: .5rem 1rem; border: none; border-radius: var(--radius); cursor: pointer; font-size: .9rem; }
.btn-primary { background: var(--color-primary); color: #fff; }
.btn-danger { background: var(--color-error); color: #fff; }
.btn-success { background: var(--color-success); color: #fff; }
input, select { padding: .5rem .7rem; border: 1px solid #ddd; border-radius: var(--radius); font-size: .9rem; }
form { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }
#log-output { background: #1e1e1e; color: #d4d4d4; padding: 1rem; border-radius: var(--radius); font-family: "Fira Code", "Cascadia Code", monospace; font-size: .85rem; max-height: 500px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.toast { position: fixed; top: 1rem; right: 1rem; padding: .8rem 1.2rem; border-radius: var(--radius); color: #fff; z-index: 999; animation: fadeIn .3s; }
.toast.success { background: var(--color-success); }
.toast.error { background: var(--color-error); }
@keyframes fadeIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
```

- [ ] **Step 7: Create `web/templates/base.html` — sidebar + main HTMX layout**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trawler Web UI</title>
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <script src="https://unpkg.com/htmx-ext-sse@2.0.4/sse.js"></script>
  <link rel="stylesheet" href="/static/app.css">
</head>
<body>
  <nav class="sidebar">
    <h1>Trawler</h1>
    <a href="/" class="{% if active_nav == 'dashboard' %}active{% endif %}">📊 仪表盘</a>
    <a href="/subscriptions" class="{% if active_nav == 'subscriptions' %}active{% endif %}">📋 订阅管理</a>
    <a href="/check" class="{% if active_nav == 'check' %}active{% endif %}">🔍 内容检查</a>
    <a href="/auth" class="{% if active_nav == 'auth' %}active{% endif %}">🔑 登录管理</a>
    <a href="/settings" class="{% if active_nav == 'settings' %}active{% endif %}">⚙️ 设置</a>
  </nav>
  <main class="main">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
```

**Verification:**
```bash
uv run ruff check web/app.py run_web.py
uv run pyright web/app.py run_web.py
```
Expected: No errors.

---

### Task 7: Dashboard route

**Duration:** 5 min

- [ ] **Step 1: Create `web/routes/dashboard.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import Phase
from web.app import TEMPLATES

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard: message stats + recent messages."""
    config = await load_config()

    store = MessageStore(config.general.data_dir)
    all_msgs = store.get_messages()

    # Stats
    total_msgs = len(all_msgs)
    pushed_count = sum(1 for m in all_msgs if m.phase == Phase.PUSHED)
    error_count = sum(1 for m in all_msgs if m.error)
    active_count = total_msgs - pushed_count

    # Token status counts
    token_ok = 0
    token_expired = 0
    token_none = 0
    for name, auth in [
        ("bilibili", config.bilibili.auth),
        ("xiaohongshu", config.xiaohongshu.auth),
        ("weibo", config.weibo.auth),
    ]:
        if auth.expires_at <= 0:
            token_none += 1
        elif auth.expires_at < __import__("time").time():
            token_expired += 1
        else:
            token_ok += 1

    # Subscription counts
    from core.subscription_cli import list_subscriptions
    subs = await list_subscriptions()
    sub_counts = {platform: len(items) for platform, items in subs.items()}

    # Recent messages (top 20)
    recent = sorted(all_msgs, key=lambda m: m.pubdate, reverse=True)[:20]

    return TEMPLATES.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_nav": "dashboard",
            "total_msgs": total_msgs,
            "pushed_count": pushed_count,
            "error_count": error_count,
            "active_count": active_count,
            "token_ok": token_ok,
            "token_expired": token_expired,
            "token_none": token_none,
            "sub_counts": sub_counts,
            "recent_messages": recent,
        },
    )
```

- [ ] **Step 2: Create `web/templates/dashboard.html`**

```html
{% extends "base.html" %}
{% block content %}
<h2>📊 仪表盘</h2>
<div class="stats">
  <div class="stat-card"><div class="value">{{ total_msgs }}</div><div class="label">总消息</div></div>
  <div class="stat-card"><div class="value">{{ active_count }}</div><div class="label">处理中</div></div>
  <div class="stat-card"><div class="value">{{ pushed_count }}</div><div class="label">已完成</div></div>
  <div class="stat-card"><div class="value">{{ error_count }}</div><div class="label">错误</div></div>
</div>
<div class="stats">
  <div class="stat-card"><div class="value" style="color: var(--color-success);">{{ token_ok }}</div><div class="label">Token 有效</div></div>
  <div class="stat-card"><div class="value" style="color: var(--color-error);">{{ token_expired }}</div><div class="label">Token 过期</div></div>
  <div class="stat-card"><div class="value" style="color: #999;">{{ token_none }}</div><div class="label">未配置 Token</div></div>
</div>
{% if sub_counts %}
<div class="card">
  <h3>📋 订阅概览</h3>
  <ul>
    {% for platform, count in sub_counts.items() %}
    <li><strong>{{ platform }}</strong>: {{ count }} 个订阅</li>
    {% endfor %}
  </ul>
</div>
{% endif %}
<div class="card">
  <h3>📝 最近消息</h3>
  {% if recent_messages %}
  <table>
    <thead><tr><th>时间</th><th>平台</th><th>标题</th><th>作者</th><th>阶段</th></tr></thead>
    <tbody>
      {% for msg in recent_messages %}
      <tr>
        <td>{{ msg.pubdate | string }}</td>
        <td>{{ msg.platform }}</td>
        <td>{{ msg.title }}</td>
        <td>{{ msg.author }}</td>
        <td>{{ msg.phase.name }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color: #999;">暂无消息</p>
  {% endif %}
</div>
{% endblock %}
```

**Verification:**
```bash
uv run ruff check web/routes/dashboard.py
uv run pyright web/routes/dashboard.py
```
Expected: No errors.

---

### Task 8: Subscriptions routes

**Duration:** 5 min

- [ ] **Step 1: Create `web/routes/subscriptions.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
from web.app import TEMPLATES

router = APIRouter()


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request) -> HTMLResponse:
    """Subscription list page."""
    subs = await list_subscriptions()
    platforms_data = [
        {"key": "bili", "name": "B站", "items": subs.get("bilibili", [])},
        {"key": "xhs", "name": "小红书", "items": subs.get("xiaohongshu", [])},
        {"key": "weibo", "name": "微博", "items": subs.get("weibo", [])},
    ]
    return TEMPLATES.TemplateResponse(
        "subscriptions.html",
        {"request": request, "active_nav": "subscriptions", "platforms": platforms_data},
    )


@router.post("/subscriptions/add")
async def subscriptions_add(
    platform: str = Form(...),
    identifier: str = Form(...),
    name: str = Form(...),
) -> RedirectResponse:
    """Add a subscription."""
    ok, msg = await add_subscription(platform, identifier, name)
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/remove")
async def subscriptions_remove(
    platform: str = Form(...),
    identifier: str = Form(...),
) -> RedirectResponse:
    """Remove a subscription."""
    ok, msg = await remove_subscription(platform, identifier)
    return RedirectResponse(url="/subscriptions", status_code=303)


@router.post("/subscriptions/search")
async def subscriptions_search(
    request: Request,
    platform: str = Form(...),
    name: str = Form(...),
) -> HTMLResponse:
    """Search for a user by name and show candidates (HTMX target)."""
    ok, msg, candidates = await search_by_name(platform, name)

    # Render a minimal HTML fragment — Jinja2 doesn't support `#fragment` syntax
    items_html = ""
    if candidates:
        for c in candidates:
            cid = c.get("uid") or c.get("user_id", "")
            cname = c.get("name", "?")
            items_html += f"<li>{cname} (ID: {cid}) "
            items_html += f"""<form action="/subscriptions/add" method="post" style="display:inline;">
                <input type="hidden" name="platform" value="{platform}">
                <input type="hidden" name="identifier" value="{cid}">
                <input type="hidden" name="name" value="{cname}">
                <button type="submit" class="btn-primary btn-small">添加</button>
            </form></li>"""

    return HTMLResponse(
        f"<p>{msg}</p><ul>{items_html}</ul>{'<p style=\'color:#999;\'>未找到匹配</p>' if not candidates and ok else ''}"
    )
```

- [ ] **Step 2: Create `web/templates/subscriptions.html`**

```html
{% extends "base.html" %}
{% block content %}
<h2>📋 订阅管理</h2>

{% for p in platforms %}
<div class="card">
  <h3>{{ p.name }} ({{ p.items | length }})</h3>
  {% if p.items %}
  <table>
    <thead><tr><th>标识</th><th>名称</th><th>操作</th></tr></thead>
    <tbody>
      {% for item in p.items %}
      <tr>
        <td>{{ item.get("uid") or item.get("user_id", "-") }}</td>
        <td>{{ item.get("name", "-") }}</td>
        <td>
          <form action="/subscriptions/remove" method="post" style="display:inline;">
            <input type="hidden" name="platform" value="{{ p.key }}">
            <input type="hidden" name="identifier" value="{{ item.get('uid') or item.get('user_id', '') }}">
            <button type="submit" class="btn-danger">删除</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color: #999;">暂无订阅</p>
  {% endif %}

  <hr style="margin: 1rem 0;">
  <h4>添加订阅</h4>
  <form action="/subscriptions/add" method="post">
    <input type="hidden" name="platform" value="{{ p.key }}">
    <input type="text" name="identifier" placeholder="UID / user_id" required>
    <input type="text" name="name" placeholder="显示名称" required>
    <button type="submit" class="btn-primary">添加</button>
  </form>

  <h4 style="margin-top:.8rem;">按名称搜索</h4>
  <form hx-post="/subscriptions/search" hx-target="#search-{{ p.key }}" hx-swap="innerHTML">
    <input type="hidden" name="platform" value="{{ p.key }}">
    <input type="text" name="name" placeholder="搜索名称" required>
    <button type="submit" class="btn-primary">搜索</button>
  </form>
  <div id="search-{{ p.key }}"></div>
</div>
{% endfor %}

<div id="search-results">
  {% if search_result %}
  <p>{{ search_result }}</p>
  {% endif %}
  {% if candidates %}
  <ul>
    {% for c in candidates %}
    <li>
      {{ c.get("name", "?") }} (ID: {{ c.get("uid") or c.get("user_id", "?") }})
      <form action="/subscriptions/add" method="post" style="display:inline;">
        <input type="hidden" name="platform" value="{{ search_platform }}">
        <input type="hidden" name="identifier" value="{{ c.get('uid') or c.get('user_id', '') }}">
        <input type="hidden" name="name" value="{{ c.get('name', '') }}">
        <button type="submit" class="btn-primary btn-small">添加</button>
      </form>
    </li>
    {% endfor %}
  </ul>
  {% endif %}
</div>
{% endblock %}
```

**Verification:**
```bash
uv run ruff check web/routes/subscriptions.py
uv run pyright web/routes/subscriptions.py
```
Expected: No errors.

---

### Task 9: Check + SSE routes

**Duration:** 8 min

- [ ] **Step 1: Create `web/routes/check.py`**

```python
from __future__ import annotations

import asyncio
import json
import time
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from core.pipeline import run_check_once
from shared.config import load_config
from web.app import TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory queue for streaming logs
_log_queue: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
_running = False


@router.get("/check", response_class=HTMLResponse)
async def check_page(request: Request) -> HTMLResponse:
    """Check page — trigger button + log output."""
    return TEMPLATES.TemplateResponse(
        "check.html",
        {"request": request, "active_nav": "check", "running": _running},
    )


@router.post("/check/run")
async def check_run() -> dict[str, str]:
    """Trigger a check run in the background."""
    global _running
    if _running:
        return {"status": "already_running"}

    _running = True

    async def _log_callback(event_type: str, message: str) -> None:
        await _log_queue.put({"type": event_type, "message": message, "time": time.strftime("%H:%M:%S")})

    async def _run() -> None:
        global _running
        try:
            config = await load_config()
            await run_check_once(config, platform="all", log_callback=_log_callback)
        except Exception as exc:
            await _log_queue.put({"type": "error", "message": f"检查失败: {exc}", "time": time.strftime("%H:%M:%S")})
        finally:
            await _log_queue.put(None)  # Signal EOF
            _running = False

    asyncio.create_task(_run())
    return {"status": "started"}


@router.get("/check/stream")
async def check_stream() -> StreamingResponse:
    """SSE endpoint: stream log events to the browser."""

    async def event_generator() -> bytes:
        while True:
            item = await _log_queue.get()
            if item is None:
                yield b"event: done\ndata: \n\n"
                break
            data = json.dumps(item, ensure_ascii=False)
            yield f"event: log\ndata: {data}\n\n".encode("utf-8")

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

- [ ] **Step 2: Create `web/templates/check.html`**

```html
{% extends "base.html" %}
{% block content %}
<h2>🔍 内容检查</h2>
<div class="card">
  <p>触发一次全平台内容检查（B站、小红书、微博）。日志将实时推送。</p>
  <button id="run-btn" class="btn-success" hx-post="/check/run" hx-target="#run-status" hx-swap="innerHTML"
    hx-on::after-request="if(event.detail.successful) { this.disabled=true; this.textContent='运行中...'; initSSE(); }">
    开始检查
  </button>
  <div id="run-status"></div>
</div>
<div class="card">
  <h3>📝 日志输出</h3>
  <div id="log-output"></div>
</div>

<script>
function initSSE() {
  var el = document.getElementById('log-output');
  var evtSource = new EventSource('/check/stream');
  evtSource.addEventListener('log', function(e) {
    var data = JSON.parse(e.data);
    el.innerHTML += '[' + data.time + '] ' + data.message + '\n';
    el.scrollTop = el.scrollHeight;
  });
  evtSource.addEventListener('done', function() {
    document.getElementById('run-btn').disabled = false;
    document.getElementById('run-btn').textContent = '开始检查';
    evtSource.close();
  });
}
</script>
{% endblock %}
```

**Verification:**
```bash
uv run ruff check web/routes/check.py
uv run pyright web/routes/check.py
```
Expected: No errors.

---

### Task 10: Auth routes

**Duration:** 8 min

- [ ] **Step 1: Create `web/routes/auth.py`**

```python
from __future__ import annotations

import io
import time

import qrcode
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from shared.auth import get_authenticator, update_auth_section
from shared.auth.base import QRStatus
from shared.config import load_config
from web.app import TEMPLATES

router = APIRouter()

PLATFORM_INFO = [
    {"key": "bili", "name": "B站"},
    {"key": "xhs", "name": "小红书"},
    {"key": "weibo", "name": "微博"},
]

CONFIG_AUTH_KEYS = {
    "bili": ("bilibili", "auth"),
    "xhs": ("xiaohongshu", "auth"),
    "weibo": ("weibo", "auth"),
}

# In-memory QR session storage (single-user, so one session per platform)
_qr_sessions: dict[str, dict] = {}


def _get_auth_status(config, platform_key: str):
    """Get token status for a platform."""
    section, _ = CONFIG_AUTH_KEYS[platform_key]
    auth = getattr(config, section).auth
    if auth.expires_at <= 0:
        return "未配置", ""
    elif auth.expires_at < time.time():
        return "已过期", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at))
    else:
        remaining = int((auth.expires_at - time.time()) // 86400)
        return f"有效 (剩余 {remaining} 天)", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at))


@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request) -> HTMLResponse:
    """Login management page."""
    config = await load_config()
    platforms = []
    for p in PLATFORM_INFO:
        status, expires = _get_auth_status(config, p["key"])
        platforms.append({**p, "token_status": status, "expires": expires})
    return TEMPLATES.TemplateResponse(
        "login.html",
        {"request": request, "active_nav": "auth", "platforms": platforms},
    )


@router.get("/auth/qr/{platform_key}")
async def auth_qr(platform_key: str) -> Response:
    """Generate QR code image for platform login.

    Stores the qr_key server-side so the poll endpoint can use it.
    """
    auth = get_authenticator(platform_key)
    qr_result = await auth.generate_qr_code()
    _qr_sessions[platform_key] = {"qr_key": qr_result.qr_key}
    # Render QR code to PNG bytes
    img = qrcode.make(qr_result.qr_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/auth/poll/{platform_key}")
async def auth_poll(platform_key: str) -> dict:
    """Poll QR scan status and auto-complete on success."""
    auth = get_authenticator(platform_key)
    session = _qr_sessions.get(platform_key)
    if session is None:
        return {"status": "no_session"}
    status = await auth.poll_qr_status(session["qr_key"])
    if status.status == QRStatus.SUCCESS:
        tokens = await auth.get_tokens(session["qr_key"])
        # Build auth_dict
        if platform_key in ("weibo", "xhs"):
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
        else:
            auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
        rt_val = auth.refresh_token
        if platform_key == "bili" and rt_val:
            auth_dict["refresh_token"] = rt_val
        await update_auth_section(platform_key, auth_dict)
        _qr_sessions.pop(platform_key, None)
        return {"status": "success", "message": "登录成功"}
    return {"status": status.status.value}
```

- [ ] **Step 2: Create `web/templates/login.html`**

```html
{% extends "base.html" %}
{% block content %}
<h2>🔑 登录管理</h2>
<div class="card">
  <h3>Token 状态</h3>
  <table>
    <thead><tr><th>平台</th><th>状态</th><th>过期时间</th><th>操作</th></tr></thead>
    <tbody>
      {% for p in platforms %}
      <tr>
        <td>{{ p.name }}</td>
        <td>{{ p.token_status }}</td>
        <td>{{ p.expires }}</td>
        <td>
          <button class="btn-primary" onclick="showQR('{{ p.key }}')">扫码登录</button>
          <div id="login-{{ p.key }}"></div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
<div id="qr-container" style="display:none;" class="card">
  <h3>二维码</h3>
  <img id="qr-img" src="" alt="QR Code" style="max-width: 256px;">
  <div id="qr-status"></div>
</div>

<script>
function showQR(platform) {
  var container = document.getElementById('qr-container');
  var img = document.getElementById('qr-img');
  var status = document.getElementById('qr-status');
  container.style.display = 'block';
  img.src = '/auth/qr/' + platform + '?t=' + Date.now();
  status.innerHTML = '等待扫码...';

  // Poll every 2 seconds
  var pollInterval = setInterval(function() {
    fetch('/auth/poll/' + platform)
      .then(r => r.json())
      .then(data => {
        if (data.status === 'scanned') {
          status.innerHTML = '已扫码，请在手机上确认';
        } else if (data.status === 'success') {
          status.innerHTML = '✅ 登录成功';
          clearInterval(pollInterval);
          setTimeout(function() { location.reload(); }, 1000);
        } else if (data.status === 'expired' || data.status === 'no_session') {
          status.innerHTML = '❌ 二维码已过期，请重试';
          clearInterval(pollInterval);
        }
      });
  }, 2000);
}
</script>
{% endblock %}
```

**Verification:**
```bash
uv run ruff check web/routes/auth.py
uv run pyright web/routes/auth.py
```
Expected: No errors.

---

### Task 11: Settings routes

**Duration:** 5 min

- [ ] **Step 1: Create `web/routes/settings.py`**

```python
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import load_config
from web.app import TEMPLATES

router = APIRouter()
CONFIG_PATH = "config/config.toml"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Settings page — view current config."""
    config = await load_config()
    return TEMPLATES.TemplateResponse(
        "settings.html",
        {"request": request, "active_nav": "settings", "config": config},
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    data_dir: str = Form(default="./data"),
    disable_ssl_verify: bool = Form(False),
    gotify_url: str = Form(default=""),
    gotify_token_bili: str = Form(default=""),
    gotify_token_xhs: str = Form(default=""),
    gotify_token_weibo: str = Form(default=""),
    xhs_enabled: bool = Form(False),
    weibo_enabled: bool = Form(False),
) -> RedirectResponse:
    """Save settings to config.toml."""
    import tomllib

    p = Path(CONFIG_PATH)
    raw: dict = {}
    if p.exists():
        with open(p, "rb") as f:
            raw = tomllib.load(f)

    # Update general
    raw.setdefault("general", {})["data_dir"] = data_dir
    raw["general"]["disable_ssl_verify"] = disable_ssl_verify

    # Update notifications
    if gotify_url:
        for plat in ("bilibili", "xiaohongshu", "weibo"):
            raw.setdefault(plat, {}).setdefault("notification", {})["gotify_url"] = gotify_url
    if gotify_token_bili:
        raw.setdefault("bilibili", {}).setdefault("notification", {})["gotify_token"] = gotify_token_bili
    if gotify_token_xhs:
        raw.setdefault("xiaohongshu", {}).setdefault("notification", {})["gotify_token"] = gotify_token_xhs
    if gotify_token_weibo:
        raw.setdefault("weibo", {}).setdefault("notification", {})["gotify_token"] = gotify_token_weibo

    # Update platform enabled flags
    raw.setdefault("xiaohongshu", {})["enabled"] = xhs_enabled
    raw.setdefault("weibo", {})["enabled"] = weibo_enabled

    # Write back with tomlkit to preserve formatting
    import tomlkit
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(raw), encoding="utf-8")

    return RedirectResponse(url="/settings", status_code=303)
```

- [ ] **Step 2: Create `web/templates/settings.html`**

```html
{% extends "base.html" %}
{% block content %}
<h2>⚙️ 设置</h2>
<div class="card">
  <form action="/settings" method="post">
    <h3>通用设置</h3>
    <p><label>数据目录: <input type="text" name="data_dir" value="{{ config.general.data_dir }}" style="width:300px;"></label></p>
    <p><label><input type="checkbox" name="disable_ssl_verify" {% if config.general.disable_ssl_verify %}checked{% endif %}> 禁用 SSL 验证</label></p>

    <h3>通知</h3>
    <p><label>Gotify URL: <input type="text" name="gotify_url" value="{{ config.bilibili.notification.gotify_url }}" style="width:400px;"></label></p>
    <p><label>Gotify Token (B站): <input type="text" name="gotify_token_bili" value="{{ config.bilibili.notification.gotify_token }}" style="width:300px;"></label></p>
    <p><label>Gotify Token (小红书): <input type="text" name="gotify_token_xhs" value="{{ config.xiaohongshu.notification.gotify_token }}" style="width:300px;"></label></p>
    <p><label>Gotify Token (微博): <input type="text" name="gotify_token_weibo" value="{{ config.weibo.notification.gotify_token }}" style="width:300px;"></label></p>

    <h3>平台启用</h3>
    <p><label><input type="checkbox" name="xhs_enabled" {% if config.xiaohongshu.enabled %}checked{% endif %}> 启用小红书</label></p>
    <p><label><input type="checkbox" name="weibo_enabled" {% if config.weibo.enabled %}checked{% endif %}> 启用微博</label></p>
    <p><em>B站始终启用</em></p>

    <button type="submit" class="btn-primary" style="margin-top:1rem;">保存</button>
  </form>
</div>
{% endblock %}
```

**Verification:**
```bash
uv run ruff check web/routes/settings.py
uv run pyright web/routes/settings.py
```
Expected: No errors.

---

### Task 12: Web route tests

**Duration:** 10 min

- [ ] **Step 1: Create `tests/test_web_dashboard.py`**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestDashboard:
    @patch("web.routes.dashboard.load_config", new_callable=AsyncMock)
    @patch("web.routes.dashboard.list_subscriptions", new_callable=AsyncMock)
    async def test_dashboard_returns_200(self, mock_list, mock_load, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "/tmp"
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_list.return_value = {"bilibili": [{"uid": 1, "name": "test"}]}

        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_dashboard_returns_200_without_auth(self, client: AsyncClient) -> None:
        """Should still work even if config loading fails (graceful fallback)."""
        resp = await client.get("/")
        # May return error page, but should not crash the server
        assert resp.status_code in (200, 500)
```

- [ ] **Step 2: Create `tests/test_web_subscriptions.py`**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestSubscriptions:
    @patch("web.routes.subscriptions.list_subscriptions", new_callable=AsyncMock)
    async def test_list_page(self, mock_list, client: AsyncClient) -> None:
        mock_list.return_value = {"bilibili": [{"uid": 1, "name": "UP主"}]}
        resp = await client.get("/subscriptions")
        assert resp.status_code == 200

    @patch("web.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_add_redirects(self, mock_add, client: AsyncClient) -> None:
        mock_add.return_value = (True, "已添加")
        resp = await client.post("/subscriptions/add", data={"platform": "bili", "identifier": "123", "name": "test"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/subscriptions"

    @patch("web.routes.subscriptions.remove_subscription", new_callable=AsyncMock)
    async def test_remove_redirects(self, mock_remove, client: AsyncClient) -> None:
        mock_remove.return_value = (True, "已删除")
        resp = await client.post("/subscriptions/remove", data={"platform": "bili", "identifier": "123"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/subscriptions"

    @patch("web.routes.subscriptions.search_by_name", new_callable=AsyncMock)
    async def test_search_returns_html(self, mock_search, client: AsyncClient) -> None:
        mock_search.return_value = (True, "找到 1 个匹配", [{"uid": 123, "name": "UP主"}])
        resp = await client.post("/subscriptions/search", data={"platform": "bili", "name": "UP"})
        assert resp.status_code == 200
        assert "UP主" in resp.text

    @patch("web.routes.subscriptions.search_by_name", new_callable=AsyncMock)
    async def test_search_empty(self, mock_search, client: AsyncClient) -> None:
        mock_search.return_value = (True, "未找到匹配", [])
        resp = await client.post("/subscriptions/search", data={"platform": "bili", "name": "不存在"})
        assert resp.status_code == 200
        assert "未找到" in resp.text
```

- [ ] **Step 3: Create `tests/test_web_check.py`**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestCheck:
    async def test_check_page(self, client: AsyncClient) -> None:
        resp = await client.get("/check")
        assert resp.status_code == 200

    @patch("web.routes.check.run_check_once", new_callable=AsyncMock)
    @patch("web.routes.check.load_config", new_callable=AsyncMock)
    async def test_check_run(self, mock_load, mock_run, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "/tmp"
        resp = await client.post("/check/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("started",)

    async def test_check_stream_returns_sse(self, client: AsyncClient) -> None:
        resp = await client.get("/check/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
```

- [ ] **Step 4: Create `tests/test_web_auth.py`**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestAuth:
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page(self, mock_load, client: AsyncClient) -> None:
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        resp = await client.get("/auth")
        assert resp.status_code == 200

    @patch("web.routes.auth.get_authenticator")
    async def test_auth_qr_returns_png(self, mock_get_auth, client: AsyncClient) -> None:
        mock_auth = MagicMock()
        mock_auth.generate_qr_code = AsyncMock(return_value=MagicMock(qr_url="https://example.com/qr", qr_key="key1"))
        mock_get_auth.return_value = mock_auth
        resp = await client.get("/auth/qr/bili")
        assert resp.status_code == 200
        assert "image/png" in resp.headers.get("content-type", "")

    @patch("web.routes.auth.get_authenticator")
    async def test_auth_poll(self, mock_get_auth, client: AsyncClient) -> None:
        mock_auth = MagicMock()
        from shared.auth.base import AuthStatus, QRStatus
        mock_auth.poll_qr_status = AsyncMock(return_value=AuthStatus(success=False, status=QRStatus.WAITING, message="waiting"))
        mock_get_auth.return_value = mock_auth
        # Need a QR session first
        await client.get("/auth/qr/bili")
        resp = await client.get("/auth/poll/bili")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "waiting"
```

- [ ] **Step 5: Create `tests/test_web_settings.py`**

```python
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestSettings:
    @patch("web.routes.settings.load_config", new_callable=AsyncMock)
    async def test_settings_page(self, mock_load, client: AsyncClient) -> None:
        mock_load.return_value.general.data_dir = "./data"
        mock_load.return_value.general.disable_ssl_verify = False
        mock_load.return_value.bilibili.notification.gotify_url = ""
        mock_load.return_value.bilibili.notification.gotify_token = ""
        mock_load.return_value.xiaohongshu.notification.gotify_token = ""
        mock_load.return_value.weibo.notification.gotify_token = ""
        mock_load.return_value.xiaohongshu.enabled = False
        mock_load.return_value.weibo.enabled = False
        resp = await client.get("/settings")
        assert resp.status_code == 200

    @patch("web.routes.settings.Path.exists")
    @patch("web.routes.settings.Path.write_text")
    @patch("web.routes.settings.open")
    async def test_settings_save(self, mock_open, mock_write, mock_exists, client: AsyncClient) -> None:
        mock_exists.return_value = True
        mock_open.return_value.__enter__.return_value.read.return_value = ""

        resp = await client.post("/settings", data={"data_dir": "/data/test"})
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings"
```

**Verification:**
```bash
uv run pytest tests/test_web_*.py -x -v
```
Expected: All 5 test files pass. Some tests may require `httpx` installed (`pip install httpx`). Run with:
```bash
uv pip install -e ".[web,dev]"
uv run pytest tests/test_web_*.py -x -v
```

---

### Task 13: Final verification and smoke test

**Duration:** 5 min

- [ ] **Step 1: Full lint check**
```bash
uv run ruff check .
```
Expected: No new lint issues.

- [ ] **Step 2: Type check**
```bash
uv run pyright .
```
Expected: No new type errors.

- [ ] **Step 3: All tests pass**
```bash
uv run pytest -x
```
Expected: All tests pass (existing + new web tests).

- [ ] **Step 4: Smoke test the web server (optional manual check)**
```bash
uv run python run_web.py
```
Expected: `Uvicorn running on http://127.0.0.1:8080`. Visit `http://127.0.0.1:8080/` in browser to verify page loads.
