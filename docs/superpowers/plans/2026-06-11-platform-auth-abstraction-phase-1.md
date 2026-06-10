# Platform Auth Abstraction — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish unified auth abstraction with TOML config, QR login for B站, token renewal, and CLI commands.

**Architecture:** Flat independent dataclasses for config/auth models. BaseAuthenticator ABC with qr_login() template method. Pure function should_renew() for renewal decisions. tomlkit for format-preserving config updates. Click group CLI with login/token/check subcommands.

**Tech Stack:** Python 3.12+, tomllib (stdlib), tomlkit>=0.13, qrcode>=7.4, bilibili-api-python, click, rich, pytest, pytest-asyncio

**Execution Order:**
1. Tasks 1-3: Auth base layer (shared/auth/base.py, qr_display.py)
2. Task 4: Config TOML rewrite (shared/config.py) 
3. Tasks 5-6: Scheduler + token_store (shared/auth/scheduler.py, token_store.py)
4. Task 7: BilibiliAuthenticator (platforms/bilibili/auth.py)
5. Task 8: Config path migration (10 files, batch A-D)
6. Tasks 9-11: CLI restructure + pyproject.toml + config.toml.example

---

### Task 1: `shared/auth/base.py` — Data Models + Errors + QRStatus

**Files:**
- Create: `shared/auth/__init__.py`
- Create: `shared/auth/base.py`
- Create: `tests/test_auth_base.py`

**Dependencies:** None (foundation module)

**What to implement:**

```python
# shared/auth/base.py
import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from collections.abc import Callable

class QRStatus(enum.StrEnum):
    WAITING = "waiting"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    SUCCESS = "success"

class AuthError(Exception): ...
class QRExpiredError(AuthError): ...
class NetworkError(AuthError): ...
class TokenInvalidError(AuthError): ...
class RefreshFailedError(AuthError): ...

@dataclass
class QRCodeResult:
    qr_url: str
    qr_key: str
    expires_in: int = 180

@dataclass
class AuthStatus:
    success: bool
    status: QRStatus
    message: str

@dataclass
class PlatformTokens:
    platform: str              # "bilibili" | "xiaohongshu" | "weibo"
    cookies: dict[str, str]
    obtained_at: float
    expires_at: float
```

```python
# shared/auth/__init__.py — re-exports (will extend in later tasks)
from shared.auth.base import (
    AuthError, AuthStatus, BaseAuthenticator, NetworkError,
    PlatformTokens, QRExpiredError, QRCodeResult, QRStatus,
    RefreshFailedError, TokenInvalidError,
)
from shared.auth.qr_display import display_qr_in_terminal
__all__ = [...]  # list all above
```

**Test outline (`tests/test_auth_base.py`):**
- QRStatus is StrEnum, has 5 members with correct string values
- QRCodeResult: creation, default expires_in=180
- AuthStatus: success/failure creation
- PlatformTokens: creation, is flat dataclass (no inheritance)
- Error hierarchy: all 4 specific errors are subclasses of AuthError, catchable via base

**Verify:** `pytest tests/test_auth_base.py -v` — all pass

**Commit:** `git commit -m "feat(auth): add QRStatus, data models, error hierarchy"`

---

### Task 2: `shared/auth/base.py` — BaseAuthenticator ABC

**Files:**
- Modify: `shared/auth/base.py` (append ABC)
- Modify: `tests/test_auth_base.py` (append tests)

**Dependencies:** Task 1

**What to implement (append to base.py):**

```python
import asyncio
import time

class BaseAuthenticator(ABC):
    @abstractmethod
    async def generate_qr_code(self) -> QRCodeResult: ...
    @abstractmethod
    async def poll_qr_status(self, qr_key: str) -> AuthStatus: ...
    @abstractmethod
    async def get_tokens(self, qr_key: str) -> PlatformTokens: ...
    @abstractmethod
    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens: ...
    @abstractmethod
    async def validate_tokens(self, tokens: PlatformTokens) -> bool: ...

    def supports_qr_login(self) -> bool:
        return True

    def supports_refresh(self) -> bool:
        return False

    async def qr_login(self, on_status: Callable[[AuthStatus], None] | None = None) -> PlatformTokens:
        # Lazy import to avoid circular dependency
        from shared.auth.qr_display import display_qr_in_terminal
        qr = await self.generate_qr_code()
        display_qr_in_terminal(qr.qr_url)
        deadline = time.monotonic() + qr.expires_in
        while time.monotonic() < deadline:
            status = await self.poll_qr_status(qr.qr_key)
            if on_status is not None:
                on_status(status)
            if status.status == QRStatus.SUCCESS:
                return await self.get_tokens(qr.qr_key)
            if status.status == QRStatus.EXPIRED:
                raise QRExpiredError("二维码已过期")
            await asyncio.sleep(2)
        raise QRExpiredError("二维码轮询超时")
```

Also create placeholder `shared/auth/qr_display.py`:
```python
def display_qr_in_terminal(url: str) -> None:
    print(f"[QR placeholder] {url}")
```

**Test outline (use `_DummyAuthenticator` with AsyncMock):**
- Cannot instantiate BaseAuthenticator directly (abstract)
- supports_qr_login() default True, supports_refresh() default False
- qr_login: success flow (WAITING → SCANNED → SUCCESS → get_tokens called)
- qr_login: on_status callback receives each status
- qr_login: expired raises QRExpiredError
- qr_login: timeout raises QRExpiredError
- Partial subclass still abstract

**Verify:** `pytest tests/test_auth_base.py -v`

**Commit:** `git commit -m "feat(auth): add BaseAuthenticator ABC with qr_login template method"`

---

### Task 3: `shared/auth/qr_display.py` — Terminal QR Rendering

**Files:**
- Rewrite: `shared/auth/qr_display.py` (replace placeholder)
- Create: `tests/test_qr_display.py`

**Dependencies:** Task 2, `pip install qrcode`

**What to implement:**

```python
# shared/auth/qr_display.py
import qrcode

_FULL = "▓"
_EMPTY = "░"

def _render_qr_matrix(url: str) -> str:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    return "\n".join("".join(_FULL if cell else _EMPTY for cell in row) for row in matrix)

def display_qr_in_terminal(url: str) -> None:
    print("\n" + _render_qr_matrix(url) + f"\n\n扫码链接（备用）: {url}\n")
```

Also add `qrcode>=7.4` to `pyproject.toml` dependencies.

**Test outline (`tests/test_qr_display.py`):**
- `_render_qr_matrix` returns string, contains block chars (▓/░), multiline output
- Different URLs produce different output
- `display_qr_in_terminal` prints to stdout (use capsys), contains QR chars and URL

**Verify:** `pytest tests/test_qr_display.py -v`

**Commit:** `git commit -m "feat(auth): implement terminal QR rendering with Unicode blocks"`

---

### Task 4: `shared/config.py` — Full TOML Rewrite

**Files:**
- Rewrite: `shared/config.py` (211 lines → ~300 lines)
- Create: `tests/test_config.py`

**Dependencies:** None (foundation module)

**What changes:**
- `import yaml` → `import tomllib`
- `load_config("config.yaml")` → `load_config("config.toml")`
- Remove: `Credential`, `Subscription`, `MonitorConfig`, `cookies_file` field
- Add: `RenewalConfig`, `AuthGlobalConfig`, `BilibiliAuth`, `XhsAuth`, `WeiboAuth`, `BilibiliMonitorConfig`, `XhsMonitorConfig`, `WeiboMonitorConfig`, `BiliSubscription`, `UserSubscription`, `BilibiliConfig`, `XhsConfig`, `WeiboConfig`, `GeneralConfig`

**New Config structure:**
```python
@dataclass class RenewalConfig:
    min_interval_hours: int = 24
    force_before_days: int = 7
    check_interval_hours: int = 6

@dataclass class AuthGlobalConfig:
    renewal: RenewalConfig = field(default_factory=RenewalConfig)

@dataclass class BilibiliAuth:  # replaces Credential
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    dedeuserid: str = ""
    ac_time_value: str = ""    # NEW: for cookie refresh
    expires_at: float = 0.0    # NEW: token expiry

@dataclass class XhsAuth:
    cookie: str = ""
    expires_at: float = 0.0

@dataclass class WeiboAuth:
    cookie: str = ""
    expires_at: float = 0.0

# DownloadConfig, TranscribeConfig, AnalysisConfig, NotificationConfig — unchanged

@dataclass class BilibiliMonitorConfig:  # replaces MonitorConfig (same fields)
    mode: str = "rss"
    interval_minutes: int = 3
    watch_dynamic: bool = True
    max_videos_per_check: int = 10
    rsshub_instances: list[str] = field(default_factory=lambda: [...3 defaults...])

@dataclass class XhsMonitorConfig:
    mode: str = "api"
    interval_minutes: int = 10

@dataclass class WeiboMonitorConfig:
    mode: str = "api"
    interval_minutes: int = 10

@dataclass class BiliSubscription:    # replaces Subscription
    uid: int = 0
    name: str = ""

@dataclass class UserSubscription:
    user_id: str = ""
    name: str = ""

@dataclass class BilibiliConfig:
    auth: BilibiliAuth = field(default_factory=BilibiliAuth)
    monitor: BilibiliMonitorConfig = field(default_factory=BilibiliMonitorConfig)
    subscriptions: list[BiliSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

@dataclass class XhsConfig:
    enabled: bool = False
    auth: XhsAuth = field(default_factory=XhsAuth)
    monitor: XhsMonitorConfig = field(default_factory=XhsMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

@dataclass class WeiboConfig:
    enabled: bool = False
    auth: WeiboAuth = field(default_factory=WeiboAuth)
    monitor: WeiboMonitorConfig = field(default_factory=WeiboMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

@dataclass class GeneralConfig:
    data_dir: str = "./data"

@dataclass class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    auth: AuthGlobalConfig = field(default_factory=AuthGlobalConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    bilibili: BilibiliConfig = field(default_factory=BilibiliConfig)
    xiaohongshu: XhsConfig = field(default_factory=XhsConfig)
    weibo: WeiboConfig = field(default_factory=WeiboConfig)
```

**Key functions:**
```python
def _dict_to_dataclass(cls, data: dict): ...  # recursive, ignores unknown fields
def load_config(path: str | Path = "config.toml") -> Config: ...  # tomllib + env overrides
def _parse_config(raw: dict) -> Config: ...  # maps TOML dict to Config
def _apply_env_overrides(cfg: Config) -> None: ...  # FEEDFLOW_* env vars
```

**Env var mapping:**
- `FEEDFLOW_GOTIFY_URL` → `cfg.bilibili.notification.gotify_url`
- `FEEDFLOW_GOTIFY_TOKEN_BILI` → `cfg.bilibili.notification.gotify_token`
- `FEEDFLOW_GOTIFY_TOKEN_XHS` → `cfg.xiaohongshu.notification.gotify_token`
- `FEEDFLOW_GOTIFY_TOKEN_WEIBO` → `cfg.weibo.notification.gotify_token`
- `FEEDFLOW_XHS_COOKIE` → `cfg.xiaohongshu.auth.cookie`
- `FEEDFLOW_WEIBO_COOKIE` → `cfg.weibo.auth.cookie`
- `FEEDFLOW_LLM_API_KEY` → `cfg.analysis.api_key`
- `FEEDFLOW_LLM_API_BASE` → `cfg.analysis.api_base`

**Test outline (`tests/test_config.py`):**
- Missing file → returns Config() defaults
- Empty TOML file → returns Config() defaults
- Full TOML → all sections parsed correctly (general, auth.renewal, download, transcribe, analysis, bilibili.auth/monitor/subscriptions/notification, xiaohongshu, weibo)
- Minimal TOML (only bilibili.auth.sessdata) → only that field set, rest defaults
- Each env var override works, env takes priority over file
- All dataclass defaults correct

**Verify:** `pytest tests/test_config.py -v`

**Commit:** `git commit -m "feat(config): rewrite config.py for TOML-based configuration"`

---

### Task 5: `shared/auth/scheduler.py` — Renewal Decision Logic

**Files:**
- Create: `shared/auth/scheduler.py`
- Create: `tests/test_scheduler.py`

**Dependencies:** Task 1 (PlatformTokens), Task 4 (RenewalConfig)

**What to implement:**
```python
# shared/auth/scheduler.py
import time
from dataclasses import dataclass
from shared.auth.base import PlatformTokens
from shared.config import RenewalConfig

@dataclass
class RenewalDecision:
    should_renew: bool
    reason: str  # "expired" | "force_soon" | "within_interval" | "not_needed"

def should_renew(tokens: PlatformTokens, config: RenewalConfig) -> RenewalDecision:
    now = time.time()
    time_to_expire = tokens.expires_at - now
    if time_to_expire <= 0:
        return RenewalDecision(False, "expired")
    if time_to_expire < config.force_before_days * 86400:
        return RenewalDecision(True, "force_soon")
    if time_to_expire < config.min_interval_hours * 3600:
        return RenewalDecision(True, "within_interval")
    return RenewalDecision(False, "not_needed")
```

**Test outline (`tests/test_scheduler.py`):**
- Already expired → (False, "expired")
- expires_at == now → (False, "expired")
- 6 days remaining (default force_before=7) → (True, "force_soon")
- 1 day remaining → (True, "force_soon")
- Exactly 7 days remaining → (False, "not_needed") (not strictly less than threshold)
- 30 days remaining → (False, "not_needed")
- Custom config: force_before=3 days, min_interval=12 days, token at 10 days → (True, "within_interval")
- RenewalDecision dataclass equality

**Verify:** `pytest tests/test_scheduler.py -v`

**Commit:** `git commit -m "feat(auth): add should_renew pure decision function for token renewal"`

---

### Task 6: `shared/auth/token_store.py` — Credential Persistence

**Files:**
- Create: `shared/auth/token_store.py`
- Create: `tests/test_token_store.py`

**Dependencies:** Task 4 (config structure), `pip install tomlkit`

**What to implement:**
```python
# shared/auth/token_store.py
from pathlib import Path
import tomlkit

def update_auth_section(config_path: str | Path, platform: str, auth_dict: dict) -> None:
    """Update only [platform.auth] section in config.toml, preserving comments/format."""
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")
    doc = tomlkit.parse(p.read_text(encoding="utf-8"))
    # Ensure platform and auth tables exist
    if platform not in doc:
        doc.add(platform, tomlkit.table(is_super_table=True))
    if "auth" not in doc[platform]:
        doc[platform].add("auth", tomlkit.table())
    # Update fields
    for key, value in auth_dict.items():
        doc[platform]["auth"][key] = value
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
```

Also add `tomlkit>=0.13` to pyproject.toml dependencies.

**Test outline (`tests/test_token_store.py`):**
- Update bilibili.auth: only auth fields change, monitor/notification/general untouched
- Comments preserved after update
- New field added to auth section works
- Update xiaohongshu.auth: bilibili/weibo untouched
- Update weibo.auth: bilibili/xiaohongshu untouched
- Missing platform section → creates it
- Missing config file → raises FileNotFoundError
- Empty auth_dict → file unchanged

**Verify:** `pytest tests/test_token_store.py -v`

**Commit:** `git commit -m "feat(auth): add token_store with tomlkit partial auth updates"`

---

### Task 7: `platforms/bilibili/auth.py` — BilibiliAuthenticator + Backward-compat

**Files:**
- Rewrite: `platforms/bilibili/auth.py`
- Create: `tests/test_bilibili_authenticator.py`

**Dependencies:** Task 1 (BaseAuthenticator), Task 4 (new Config), Task 6 (token_store)

**What to implement:**

```python
# platforms/bilibili/auth.py — full rewrite
from shared.auth.base import (BaseAuthenticator, QRCodeResult, AuthStatus, QRStatus,
                               PlatformTokens, QRExpiredError, RefreshFailedError)
from shared.config import BilibiliAuth, BilibiliConfig, Config
from shared.auth.token_store import update_auth_section
import bilibili_api
from bilibili_api import login_v2

# ── Backward-compat helper ──
def get_credential(config: Config) -> bilibili_api.Credential:
    """Read from config.bilibili.auth (NEW path). Returns empty Credential if not configured."""
    auth = config.bilibili.auth
    if auth.sessdata and auth.bili_jct:
        return bilibili_api.Credential(
            sessdata=auth.sessdata, bili_jct=auth.bili_jct,
            buvid3=auth.buvid3 or "", dedeuserid=auth.dedeuserid or "")
    # warn user to run trawler login --platform bili
    return bilibili_api.Credential()

# ── QR Login Authenticator ──
class BilibiliAuthenticator(BaseAuthenticator):
    def __init__(self, config_path: str = "config.toml"):
        self._config_path = config_path
        self._qr_login: login_v2.QrCodeLogin | None = None

    def _get_qr_login(self) -> login_v2.QrCodeLogin: ...  # lazy init

    async def generate_qr_code(self) -> QRCodeResult:
        # qr.get_qrcode() → {"url": ..., "qrcode_key": ...}
        # Return QRCodeResult(qr_url=url, qr_key=key, expires_in=180)

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        # qr.poll_status(qr_key) → {"code": 86101|86090|0|86038}
        # Map: 86101→WAITING, 86090→SCANNED, 0→SUCCESS, 86038→EXPIRED

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        # qr.get_cookie() → {SESSDATA, bili_jct, buvid3, DedeUserID, ac_time_value}
        # Return PlatformTokens(platform="bilibili", cookies={...without ac_time_value...}, expires_at=now+180days)
        # NOTE: ac_time_value is NOT put into PlatformTokens.cookies. It is stored separately via config.
        #   Return a tuple (tokens, ac_time_value) or store it on self for the CLI to retrieve.
        #   Design choice: BilibiliAuthenticator.get_tokens returns the tokens AND
        #   saves ac_time_value to self._last_ac_time_value. CLI reads it via authenticator._last_ac_time_value.

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        # Load ac_time_value from config file via self._config_path
        #   cfg = load_config(self._config_path); ac = cfg.bilibili.auth.ac_time_value
        # If ac_time_value is empty → raise RefreshFailedError("缺少 ac_time_value，请重新扫码登录")
        # Build bilibili_api.Credential → check_refresh() → refresh()
        # Parse new cookies from response → return new PlatformTokens
        # NOTE: ac_time_value is NOT in PlatformTokens. Always read from config.

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        # Check expires_at > now, then try API call with credential

    def supports_refresh(self) -> bool:
        return True
```

**Key decisions:**
- `get_credential()` signature unchanged → `comments.py` and other callers need NO code change
- Remove `_parse_netscape_cookies()` and `config.cookies_file` fallback entirely
- `ac_time_value` is critical for refresh — must be persisted alongside cookies

**Test outline (`tests/test_bilibili_authenticator.py`):**
- `get_credential()`: reads config.bilibili.auth, empty config returns empty Credential
- `generate_qr_code()`: mocks QrCodeLogin, returns QRCodeResult
- `poll_qr_status()`: mocks poll_status, maps 4 B站 codes to QRStatus
- `get_tokens()`: mocks get_cookie, returns PlatformTokens with correct cookies
- `refresh_tokens()`: mocks Credential.refresh, returns new tokens
- `refresh_tokens()` without ac_time_value → raises RefreshFailedError
- `validate_tokens()`: expired → False, valid + API success → True
- `supports_refresh()` returns True

**Verify:** `pytest tests/test_bilibili_authenticator.py -v`

**Commit:** `git commit -m "feat(bili): rewrite auth.py as BilibiliAuthenticator with QR login"`

---

### Task 8: Config Path Migration — 10 Files

**Dependencies:** Task 4 (new Config structure must be in place)

This is a mechanical migration. Execute in batches:

#### Batch A: Entry points
| File | Change |
|------|--------|
| `run_check.py` | default `"config.yaml"` → `"config.toml"` (1 line) |

#### Batch B: core/pipeline.py (8 occurrences)
| Old | New |
|-----|-----|
| `config.monitor.mode` | `config.bilibili.monitor.mode` |
| `config.subscriptions` (3 occurrences) | `config.bilibili.subscriptions` |
| `config.monitor.max_videos_per_check` | `config.bilibili.monitor.max_videos_per_check` |
| `config.monitor.watch_dynamic` | `config.bilibili.monitor.watch_dynamic` |
| `config.notification` (2 occurrences) | `config.bilibili.notification` |

Note: `config.xiaohongshu.subscriptions` and `config.xiaohongshu.notification` already match new structure. `config.transcribe.delete_after_transcribe` and `config.transcribe.output_dir` unchanged. `config.analysis.*` unchanged.

#### Batch C: Bilibili platform files
| File | Change |
|------|--------|
| `platforms/bilibili/dynamic.py:162` | `config.monitor.max_videos_per_check` → `config.bilibili.monitor.max_videos_per_check` |
| `platforms/bilibili/monitor.py:154` | `config.monitor.max_videos_per_check` → `config.bilibili.monitor.max_videos_per_check` |
| `platforms/bilibili/rss_monitor.py:149` | `config.monitor.rsshub_instances` → `config.bilibili.monitor.rsshub_instances` |
| `platforms/bilibili/rss_monitor.py:266` | `config.monitor.max_videos_per_check` → `config.bilibili.monitor.max_videos_per_check` |
| `platforms/bilibili/comments.py` | No change (indirect via `get_credential()`) |

#### Batch D: Shared + XHS
| File | Change |
|------|--------|
| `shared/downloader.py:102-107` | Remove `config.cookies_file` block entirely (~6 lines) |
| `platforms/xiaohongshu/auth.py:41` | `config.xiaohongshu.cookie` → `config.xiaohongshu.auth.cookie` |
| `platforms/xiaohongshu/auth.py:51` | `config.yaml` → `config.toml` in warning message |

#### No changes needed (verification only):
- `core/notifier.py` — takes NotificationConfig param directly
- `core/transcriber.py` — `config.transcribe.*` unchanged
- `core/summarizer.py` — `config.analysis.*` unchanged
- `platforms/xiaohongshu/comments.py` — indirect via `get_xhs_cookie()`
- `platforms/xiaohongshu/monitor.py` — indirect via `get_xhs_cookie()`
- `platforms/xiaohongshu/downloader.py` — indirect via `get_xhs_cookie()`

**Verify:** `python -c "from shared.config import load_config; c = load_config(); print(c.bilibili.auth, c.bilibili.monitor)"` + grep for any remaining old paths: `rg 'config\.credential\.|config\.cookies_file|config\.monitor\.' --type py`

**Commit:** `git commit -m "refactor: migrate all config paths to new TOML structure"`

---

### Task 9: `run_check.py` — Click Group with login/token/check Subcommands

**Files:**
- Rewrite: `run_check.py`
- Create: `tests/test_cli.py`

**Dependencies:** Task 1, 4, 6, 7

**What to implement:**

```python
# run_check.py — restructured from single command to group
import asyncio, time, sys, logging
import click
from rich.console import Console
from rich.table import Table
from shared.config import load_config
from shared.auth import get_authenticator, update_auth_section, QRExpiredError
from shared.auth.base import PlatformTokens

console = Console()

@click.group()
def cli() -> None:
    """Trawler - 多平台创作者内容追更自动化工作流"""
    pass

@cli.command()
@click.option("--platform", type=click.Choice(["bili", "xhs", "weibo"]), required=True)
def login(platform: str) -> None:
    """二维码扫码登录"""
    if platform in ("xhs", "weibo"):
        console.print(f"[yellow]{platform} 登录功能将在后续版本支持[/yellow]")
        return
    try:
        authenticator = get_authenticator(platform)
        tokens = asyncio.run(authenticator.qr_login())
        auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
        # ac_time_value is stored separately (not in PlatformTokens)
        if hasattr(authenticator, "_last_ac_time_value") and authenticator._last_ac_time_value:
            auth_dict["ac_time_value"] = authenticator._last_ac_time_value
        update_auth_section(platform, auth_dict)
        console.print(f"[green]✓ {platform} 登录成功，凭证已保存[/green]")
    except QRExpiredError:
        console.print("[red]✗ 二维码已过期，请重试[/red]")
        sys.exit(1)

@cli.group()
def token() -> None:
    """Token 管理命令"""
    pass

@token.command("status")
def token_status() -> None:
    """查看各平台 token 状态"""
    config = load_config("config.toml")
    # Build Rich table: platform | status (未配置/已过期/有效+剩余天数) | 过期时间

@token.command("refresh")
@click.option("--platform", type=click.Choice(["bili", "xhs", "weibo"]), required=True)
def token_refresh(platform: str) -> None:
    """手动续期 token"""
    # xhs/weibo → "coming soon"
    # bili: check expires_at, get authenticator, refresh_tokens, update_auth_section

@cli.command()
@click.option("--platform", type=click.Choice(["all", "bili", "xhs", "weibo"]), default="all")
@click.option("--config", "config_path", default="config.toml", show_default=True)
@click.option("--verbose", is_flag=True, default=False)
def check(platform: str, config_path: str, verbose: bool) -> None:
    """检查各平台新内容"""
    # (migrated from current cli body)
    config = load_config(config_path)
    from core.pipeline import run_check_once
    asyncio.run(run_check_once(config, platform))
```

Also update `shared/auth/__init__.py` to add:
```python
def get_authenticator(platform: str) -> BaseAuthenticator:
    """Factory: get platform authenticator instance."""
    if platform == "bili":
        from platforms.bilibili.auth import BilibiliAuthenticator
        return BilibiliAuthenticator()
    raise ValueError(f"Unsupported platform: {platform}")

def update_auth_section(platform: str, auth_dict: dict, config_path: str = "config.toml") -> None:
    from shared.auth.token_store import update_auth_section as _update
    _update(config_path=config_path, platform=platform, auth_dict=auth_dict)
```

**Test outline (`tests/test_cli.py`):**
- `trawler --help` → exits 0, shows login/token/check
- `trawler check --help` → shows --platform choices include weibo, --config default config.toml
- `trawler check --platform bili` → mock load_config + run_check_once, exits 0
- `trawler check` with bad config → exits 1
- `trawler login --platform bili` → mock get_authenticator (returns AsyncMock qr_login), mock update_auth_section, prints success
- `trawler login --platform xhs` → prints "coming soon"
- `trawler token status` → loads config, prints Rich table with 3 platforms
- `trawler token refresh --platform bili` → mock authenticator.refresh_tokens, prints success
- `trawler token refresh --platform bili` expired → prints re-login message, exits 1

**Mock targets:** `shared.auth.get_authenticator`, `shared.auth.update_auth_section`, `shared.config.load_config`, `core.pipeline.run_check_once`
Use `click.testing.CliRunner` — no pytest-asyncio needed (commands use `asyncio.run()` internally).

**Verify:** `pytest tests/test_cli.py -v`

**Commit:** `git commit -m "feat(cli): restructure to click group with login/token/check commands"`

---

### Task 10: `pyproject.toml` — Dependency Updates

**File:** `pyproject.toml`

**Changes:**
- Add: `"qrcode>=7.4"`, `"tomlkit>=0.13"` to `dependencies`
- Remove: `"pyyaml>=6.0"` from `dependencies`
- Entry point unchanged: `trawler = "run_check:cli"`

**Verify:** `pip install -e ".[dev]"` succeeds, `python -c "import qrcode; import tomlkit; print('ok')"`

**Commit:** `git commit -m "chore: add qrcode+tomlkit deps, remove pyyaml"`

---

### Task 11: `config.toml.example` — Example Config File

**File:** `config.toml.example` (create)

Full commented TOML with all sections from spec: general, auth.renewal, download, transcribe, analysis, bilibili (auth/monitor/subscriptions/notification), xiaohongshu (auth/monitor/subscriptions/notification), weibo (auth/monitor/subscriptions/notification).

Users copy to `config.toml` and fill in values. Include Chinese comments explaining each section. Include note about `trawler login --platform bili` for QR login.

**Verify:** `python -c "import tomllib; tomllib.load(open('config.toml.example', 'rb'))"` succeeds

**Commit:** `git commit -m "docs: add config.toml.example template"`

---

### Design Fixes & Decision Record

以下 3 个设计修复已在 plan 中各 task 内标注，此处汇总确保不被遗漏：

#### Fix 1: `update_auth_section` 签名一致

`token_store.py` 中的函数签名为 `update_auth_section(config_path, platform, auth_dict)`。`shared/auth/__init__.py` 的 wrapper 需要传递 `config_path`：

```python
def update_auth_section(platform: str, auth_dict: dict, config_path: str = "config.toml") -> None:
    from shared.auth.token_store import update_auth_section as _update
    _update(config_path=config_path, platform=platform, auth_dict=auth_dict)
```

#### Fix 2: `ac_time_value` 从 config 读取，不在 PlatformTokens 中

`PlatformTokens` 不包含 `ac_time_value` 字段。`BilibiliAuthenticator.refresh_tokens()` 需要时直接从 config 文件加载：

```python
async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
    cfg = load_config(self._config_path)
    ac = cfg.bilibili.auth.ac_time_value
    if not ac:
        raise RefreshFailedError("缺少 ac_time_value，请重新扫码登录")
    # 用 ac 构建 bilibili_api.Credential 并续期
```

`get_tokens()` 也不把 `ac_time_value` 放入 `PlatformTokens.cookies`，而是存到 `self._last_ac_time_value` 供 CLI 取用。

#### Fix 3: CLI login 命令持久化 `ac_time_value`

QR 登录成功后，从 `authenticator._last_ac_time_value` 获取并写入 `auth_dict`：

```python
auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
if hasattr(authenticator, "_last_ac_time_value") and authenticator._last_ac_time_value:
    auth_dict["ac_time_value"] = authenticator._last_ac_time_value
update_auth_section(platform, auth_dict)
```
