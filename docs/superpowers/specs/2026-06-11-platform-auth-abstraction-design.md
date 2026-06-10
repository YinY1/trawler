# 平台认证抽象层设计

> 日期: 2026-06-11
> 状态: 已批准

## 概述

为 Trawler 建立统一的平台认证抽象层，支持二维码扫码登录和 Token 自动续期。同时新增微博平台支持。配置格式从 YAML 切换到 TOML，全面重新设计配置结构。

## 目标

1. **统一认证抽象** — 所有平台继承 `BaseAuthenticator` ABC，遵循相同的接口契约
2. **二维码登录** — B站、微博、小红书支持终端 QR 扫码登录
3. **Token 主动续期** — 在 Token 过期**之前**主动续期，而非过期后补救。可配置最短续期间隔，写死过期前 7 天必须续期
4. **微博平台** — 完整功能：监控用户动态、内容抓取、摘要、通知
5. **凭证持久化** — 扫码成功后自动写入 `config.toml`
6. **TOML 配置** — 全面重新设计配置结构，使用 TOML 格式

## 架构

### 核心抽象（`shared/auth/base.py`）

**使用 ABC 作为唯一抽象契约。不需要 Protocol — 所有平台都是内部实现，无需结构化子类型。**

```python
import enum

class QRStatus(enum.StrEnum):
    """二维码扫码状态"""
    WAITING = "waiting"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    SUCCESS = "success"

class AuthError(Exception):
    """认证错误基类"""
class QRExpiredError(AuthError): pass
class NetworkError(AuthError): pass
class TokenInvalidError(AuthError): pass
class RefreshFailedError(AuthError): pass

@dataclass
class QRCodeResult:
    """二维码生成结果"""
    qr_url: str          # 编码为二维码的 URL
    qr_key: str          # 轮询用的 key (B站: qrcode_key, 微博: qrid, XHS: qr_id+code)
    expires_in: int      # 有效期（秒），默认 180

@dataclass
class AuthStatus:
    """认证状态"""
    success: bool
    status: QRStatus
    message: str

@dataclass
class PlatformTokens:
    """通用凭证容器 — 各平台返回，scheduler 只依赖 expires_at 做续期决策"""
    platform: str                    # "bilibili" | "xiaohongshu" | "weibo"
    cookies: dict[str, str]          # 标准 cookie 键值对
    obtained_at: float               # 获取时间戳
    expires_at: float                # 过期时间戳

class BaseAuthenticator(ABC):
    """平台认证基类 — 定义 QR 登录骨架和续期接口"""

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
        return False   # 子类按需覆盖

    async def qr_login(self, on_status: Callable[[AuthStatus], None] | None = None) -> PlatformTokens:
        """完整的 QR 登录流程: 生成 → 显示 → 轮询 → 获取凭证"""
        qr = await self.generate_qr_code()
        display_qr_in_terminal(qr.qr_url)
        deadline = time.monotonic() + qr.expires_in
        while time.monotonic() < deadline:
            status = await self.poll_qr_status(qr.qr_key)
            if on_status:
                on_status(status)
            if status.status == QRStatus.SUCCESS:
                return await self.get_tokens(qr.qr_key)
            if status.status == QRStatus.EXPIRED:
                raise QRExpiredError("二维码已过期")
            await asyncio.sleep(2)
        raise QRExpiredError("二维码轮询超时")
```

**关键设计决策：**
- 去掉了 `PlatformAuthenticator(Protocol)` — 用 ABC 统一抽象
- `AuthStatus.status` 使用 `StrEnum` 替代裸字符串
- 新增 `AuthError` 错误层级：`QRExpiredError`, `NetworkError`, `TokenInvalidError`, `RefreshFailedError`
- `PlatformTokens` 去掉 `extra: dict` — 各平台用平台特有的 Tokens 子类（见下文）
- 续期决策函数 `should_renew()` 是纯函数，可独立测试

### 公共模块（`shared/auth/`）

```
shared/auth/
  __init__.py
  base.py        # BaseAuthenticator(ABC) + 数据模型 (QRCodeResult, AuthStatus, PlatformTokens 等)
  qr_display.py  # 终端二维码渲染 (依赖 qrcode 库)
  token_store.py # 读写 config.toml 中的凭证（只更新 auth section）
  scheduler.py   # Token 主动续期调度器 (过期前续期)
```

#### `base.py` — 认证骨架

- `BaseAuthenticator(ABC)` — 提供 `qr_login()` 模板方法，子类实现 `generate_qr_code()`、`poll_qr_status()`、`get_tokens()` 等
- 数据模型: `QRCodeResult`, `AuthStatus`, `QRStatus(StrEnum)`, `PlatformTokens`
- 错误层级: `AuthError` → `QRExpiredError` / `NetworkError` / `TokenInvalidError` / `RefreshFailedError`

#### `qr_display.py` — 终端二维码

使用 `qrcode` 库生成 QR 码，以 Unicode 块字符在终端渲染。

#### `token_store.py` — 凭证持久化（局部更新）

**只更新 `[platform.auth]` section，不做全量序列化。**

- `update_auth_section(platform: str, auth_dict: dict) -> None` — 用 `tomlkit` 读取 → 更新指定 section → 写回
- 避免全量覆写导致用户手动修改丢失
- QR 登录成功后调用此函数写入凭证
- 续期成功后调用此函数更新凭证和 `expires_at`

#### `scheduler.py` — 主动续期调度

**核心原则：在 Token 过期前主动续期，过期后续期没有意义。**

**调度器生命周期：**
- 第1-3期：独立的 CLI 命令 `trawler token refresh --all`（适合 cron 调用）
- 第4期：可选的 daemon 模式 `trawler token renew --daemon`（后台常驻）
- `run_check` 启动前会调用 `validate_tokens()` 检查有效性，但不运行续期调度器

**续期决策 — 纯函数，可独立测试：**

```python
@dataclass
class RenewalDecision:
    should_renew: bool
    reason: str          # "expired" | "force_soon" | "within_interval" | "not_needed"

def should_renew(tokens: PlatformTokens, config: RenewalConfig) -> RenewalDecision:
    """续期决策 — 纯函数，无副作用"""
    now = time.time()
    time_to_expire = tokens.expires_at - now

    if time_to_expire <= 0:
        return RenewalDecision(False, "expired")      # 已过期，通知重新扫码

    force_threshold = config.force_before_days * 86400
    if time_to_expire < force_threshold:
        return RenewalDecision(True, "force_soon")     # 即将过期，强制续期

    min_interval = config.min_interval_hours * 3600
    if time_to_expire < min_interval:
        return RenewalDecision(True, "within_interval") # 进入续期窗口

    return RenewalDecision(False, "not_needed")         # 距过期充裕，跳过
```

### 目录结构（变更后）

```
shared/
  protocols.py           # 保持现有数据模型 (VideoInfo, NoteInfo 等) 和 JsonSetStore
  auth/                  # 新增
    __init__.py
    base.py              # BaseAuthenticator(ABC) + 数据模型 + 错误层级
    qr_display.py
    token_store.py
    scheduler.py
  config.py              # 完全重写: TOML + 新配置结构

platforms/
  bilibili/
    auth.py              # 重构为 BilibiliAuthenticator(BaseAuthenticator)
  xiaohongshu/
    auth.py              # 重构为 XhsAuthenticator(BaseAuthenticator)
  weibo/                 # 新增
    __init__.py
    auth.py              # WeiboAuthenticator(BaseAuthenticator)
    api.py               # 微博 API 封装
    monitor.py           # 微博动态监控

core/
  pipeline.py            # 扩展: 增加 weibo 平台分支 + token 检查
  notifier.py            # 扩展: 增加微博通知格式

run_check.py             # 扩展: 新增 login/token 子命令组
```

### CLI 命令设计

```bash
# 二维码登录
trawler login --platform bili       # B站 QR 扫码登录
trawler login --platform xhs        # 小红书 QR 扫码登录 (第3期)
trawler login --platform weibo      # 微博 QR 扫码登录 (第2期)

# Token 管理
trawler token status                # 查看各平台 token 状态
trawler token refresh --platform bili  # 手动续期

# 现有命令扩展
trawler check --platform all        # 检查新内容 (现有，增加 weibo)
```

使用 Click 子命令组（`click.Group`），在 `run_check.py` 中组织。如果后续命令增多，考虑拆分为 `cli/` 包。

## 各平台实现细节

### B站（第1期）

**QR 登录：**
- 直接使用 `bilibili_api.login_v2.QrCodeLogin`
- 轮询间隔 2 秒，超时 180 秒
- 成功后获取 `SESSDATA`, `bili_jct`, `DedeUserID`, `buvid3`, `ac_time_value`

**Token 主动续期：**
- 续期策略：在 cookie 过期前主动续期，不过期后续期
- 使用 `Credential.check_refresh()` 检查是否需要续期
- 使用 `Credential.refresh()` 执行 4 步续期（需要 `ac_time_value`）
- 续期后自动更新 `config.toml`
- 如果 `ac_time_value` 丢失或已过期，通知用户重新扫码登录

**API 参考：**
- QR 生成: `GET https://passport.bilibili.com/x/passport-login/web/qrcode/generate`
- QR 轮询: `GET https://passport.bilibili.com/x/passport-login/web/qrcode/poll`
- 续期检查: `GET https://passport.bilibili.com/x/passport-login/web/cookie/info`
- Cookie 刷新: `POST https://passport.bilibili.com/x/passport-login/web/cookie/refresh`

### 微博（第2期）

**QR 登录：**
- 纯 HTTP，无签名依赖
- QR 生成: `GET https://passport.weibo.com/sso/v2/qrcode/image?entry=miniblog&size=180`
- QR 轮询: `GET https://passport.weibo.com/sso/v2/qrcode/check?entry=miniblog&qrid={qrid}`
- 轮询间隔 2 秒，超时 ~240 秒
- 成功后获取 `SUB`, `SUBP`, `WBPSESS`, `SSOLoginState` cookies

**Token 主动续期：**
- 定期访问微博主页（`GET https://weibo.com/`）保持 cookie 活跃
- Cookie 有效期约 7 天，保活请求频率可配置（默认每 6 小时一次）
- 续期后更新 `expires_at` 时间戳
- 已过期则通知用户重新扫码

**内容监控：**
- 移动端 API: `GET https://m.weibo.cn/api/container/getIndex`
- PC 端 API: `GET https://weibo.com/ajax/statuses/mymblog`
- 返回微博博文的图文内容

**动态/内容模型：**
```python
@dataclass
class WeiboPost:
    """微博帖子"""
    post_id: str
    text: str                 # 原始文本 (含 HTML)
    clean_text: str           # 纯文本
    author: str
    user_id: str
    pubdate: int
    image_urls: list[str]
    reposts_count: int
    comments_count: int
    likes_count: int
    is_original: bool
    reposted_post: WeiboPost | None  # 转发的原帖
```

**数据存储：**
- 复用 `shared/protocols.py` 中的 `JsonSetStore` 做去重

### 小红书（第3期）

**QR 登录：**
- 依赖 `vendor/spider_xhs` 的签名 JS 文件（通过 `execjs` + Node.js）
- QR 生成: `POST https://edith.xiaohongshu.com/api/sns/web/v1/login/qrcode/create`
- QR 轮询: `POST https://edith.xiaohongshu.com/api/qrcode/userinfo`
- 成功后获取 `web_session` cookie

**前提条件：**
- `vendor/spider_xhs` 签名模块可用于登录 API（需验证）
- 系统安装 Node.js

**Token 续期：**
- 待探索。小红书无官方续期机制
- 可能方案：定期访问 XHS 主页保活，或 cookie 即将过期时提示重新扫码

## 配置全面重新设计（`config.toml`）

**从 YAML 切换到 TOML，全面整理配置结构。不兼容旧配置格式。**

### TOML 配置文件结构

```toml
# ═══════════════════════════════════════════════════════════
# Trawler 配置
# ═══════════════════════════════════════════════════════════

# ── 通用设置 ──────────────────────────────────────────────
[general]
data_dir = "./data"

# ── 认证与续期 ──────────────────────────────────────────────
[auth.renewal]
min_interval_hours = 24     # 可配置: 最短续期间隔（小时）
force_before_days = 7       # 过期前 N 天强制续期（不建议修改）
check_interval_hours = 6    # 调度器检查频率（小时）

# ── 下载设置 ──────────────────────────────────────────────
[download]
dir = "./downloads"
quality = "worst"
format = "bestaudio/worst"
max_concurrent = 3

# ── 转写设置 ──────────────────────────────────────────────
[transcribe]
model = "base"
language = "zh"
output_dir = "./transcripts"
delete_after_transcribe = true

# ── AI 分析 ────────────────────────────────────────────────
[analysis]
enabled = true
provider = "codebuddy"     # codebuddy | openai | ollama
api_base = ""
api_key = ""
model_name = ""

# ── B站 ────────────────────────────────────────────────────
[bilibili]
# QR 扫码登录后自动写入以下字段，无需手动填写
[bilibili.auth]
sessdata = ""
bili_jct = ""
buvid3 = ""
dedeuserid = ""
ac_time_value = ""         # 用于 cookie 续期，登录后自动填充
expires_at = 0             # Token 过期时间戳，登录后自动填充

[bilibili.monitor]
mode = "rss"               # rss | api
interval_minutes = 3
watch_dynamic = true
max_videos_per_check = 10
rsshub_instances = [
    "https://rsshub.yfi.moe",
    "https://rsshub.liumingye.cn",
    "https://rss.shab.fun",
]

[[bilibili.subscriptions]]
uid = 0
name = ""

[bilibili.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5

# ── 小红书 ─────────────────────────────────────────────────
[xiaohongshu]
enabled = false

[xiaohongshu.auth]
cookie = ""                 # 或 QR 登录后自动填充的 cookie 字段
expires_at = 0

[xiaohongshu.monitor]
mode = "api"
interval_minutes = 10

[[xiaohongshu.subscriptions]]
user_id = ""
name = ""

[xiaohongshu.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5

# ── 微博 ───────────────────────────────────────────────────
[weibo]
enabled = false

[weibo.auth]
cookie = ""                 # 或 QR 登录后自动填充
expires_at = 0

[weibo.monitor]
mode = "api"                # api | mobile
interval_minutes = 10

[[weibo.subscriptions]]
user_id = ""
name = ""

[weibo.notification]
enabled = true
gotify_url = ""
gotify_token = ""
priority = 5
```

### `shared/config.py` 完整重写

**设计原则：每个平台独立的扁平 dataclass，不用继承。通用结构靠约定而非类型层次。**

```python
import os
import tomllib        # Python 3.11+
from dataclasses import dataclass, field
from pathlib import Path

# ── 认证续期配置 ────────────────────────────────────────
@dataclass
class RenewalConfig:
    min_interval_hours: int = 24
    force_before_days: int = 7
    check_interval_hours: int = 6

@dataclass
class AuthGlobalConfig:
    renewal: RenewalConfig = field(default_factory=RenewalConfig)

# ── 平台认证凭证（各自独立，不继承）──────────────────────
@dataclass
class BilibiliAuth:
    """B站凭证 — QR 登录后自动填充"""
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    dedeuserid: str = ""
    ac_time_value: str = ""      # 用于 cookie 续期
    expires_at: float = 0.0

@dataclass
class XhsAuth:
    """小红书凭证"""
    cookie: str = ""
    expires_at: float = 0.0

@dataclass
class WeiboAuth:
    """微博凭证"""
    cookie: str = ""
    expires_at: float = 0.0

# ── 下载配置 ──────────────────────────────────────────────
@dataclass
class DownloadConfig:
    dir: str = "./downloads"
    quality: str = "worst"
    format: str = "bestaudio/worst"
    max_concurrent: int = 3

# ── 转写配置 ──────────────────────────────────────────────
@dataclass
class TranscribeConfig:
    model: str = "base"
    language: str = "zh"
    output_dir: str = "./transcripts"
    delete_after_transcribe: bool = True

# ── 监控配置（各平台独立）──────────────────────────────────
@dataclass
class BilibiliMonitorConfig:
    mode: str = "rss"            # rss | api
    interval_minutes: int = 3
    watch_dynamic: bool = True
    max_videos_per_check: int = 10
    rsshub_instances: list[str] = field(default_factory=lambda: [
        "https://rsshub.yfi.moe",
        "https://rsshub.liumingye.cn",
        "https://rss.shab.fun",
    ])

@dataclass
class XhsMonitorConfig:
    mode: str = "api"
    interval_minutes: int = 10

@dataclass
class WeiboMonitorConfig:
    mode: str = "api"            # api | mobile
    interval_minutes: int = 10

# ── 分析配置 ──────────────────────────────────────────────
@dataclass
class AnalysisConfig:
    enabled: bool = True
    provider: str = "codebuddy"  # codebuddy | openai | ollama
    api_base: str = ""
    api_key: str = ""
    model_name: str = ""

# ── 通知配置 ──────────────────────────────────────────────
@dataclass
class NotificationConfig:
    enabled: bool = True
    gotify_url: str = ""
    gotify_token: str = ""
    priority: int = 5

# ── 订阅条目 ──────────────────────────────────────────────
@dataclass
class BiliSubscription:
    uid: int = 0
    name: str = ""

@dataclass
class UserSubscription:
    user_id: str = ""
    name: str = ""

# ── 平台配置（各自独立，不继承）──────────────────────────
@dataclass
class BilibiliConfig:
    auth: BilibiliAuth = field(default_factory=BilibiliAuth)
    monitor: BilibiliMonitorConfig = field(default_factory=BilibiliMonitorConfig)
    subscriptions: list[BiliSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

@dataclass
class XhsConfig:
    enabled: bool = False
    auth: XhsAuth = field(default_factory=XhsAuth)
    monitor: XhsMonitorConfig = field(default_factory=XhsMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

@dataclass
class WeiboConfig:
    enabled: bool = False
    auth: WeiboAuth = field(default_factory=WeiboAuth)
    monitor: WeiboMonitorConfig = field(default_factory=WeiboMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)
    notification: NotificationConfig = field(default_factory=NotificationConfig)

# ── 顶层配置 ──────────────────────────────────────────────
@dataclass
class GeneralConfig:
    data_dir: str = "./data"

@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    auth: AuthGlobalConfig = field(default_factory=AuthGlobalConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    bilibili: BilibiliConfig = field(default_factory=BilibiliConfig)
    xiaohongshu: XhsConfig = field(default_factory=XhsConfig)
    weibo: WeiboConfig = field(default_factory=WeiboConfig)

def load_config(path: str | Path = "config.toml") -> Config:
    """从 TOML 文件加载配置，并应用环境变量覆盖"""
    p = Path(path)
    if not p.exists():
        return Config()
    with open(p, "rb") as f:
        raw: dict = tomllib.load(f)
    cfg = _parse_config(raw)
    _apply_env_overrides(cfg)
    return cfg

def _parse_config(raw: dict) -> Config:
    """将 TOML dict 解析为 Config dataclass"""
    ...

def _apply_env_overrides(cfg: Config) -> None:
    """环境变量覆盖敏感字段（Docker/K8s 部署用）"""
    if v := os.environ.get("FEEDFLOW_GOTIFY_URL"):
        cfg.bilibili.notification.gotify_url = v
    if v := os.environ.get("FEEDFLOW_GOTIFY_TOKEN_BILI"):
        cfg.bilibili.notification.gotify_token = v
    if v := os.environ.get("FEEDFLOW_GOTIFY_TOKEN_XHS"):
        cfg.xiaohongshu.notification.gotify_token = v
    if v := os.environ.get("FEEDFLOW_GOTIFY_TOKEN_WEIBO"):
        cfg.weibo.notification.gotify_token = v
    if v := os.environ.get("FEEDFLOW_XHS_COOKIE"):
        cfg.xiaohongshu.auth.cookie = v
    if v := os.environ.get("FEEDFLOW_WEIBO_COOKIE"):
        cfg.weibo.auth.cookie = v
    if v := os.environ.get("FEEDFLOW_LLM_API_KEY"):
        cfg.analysis.api_key = v
    if v := os.environ.get("FEEDFLOW_LLM_API_BASE"):
        cfg.analysis.api_base = v
```

## 分期实施计划

| 期数 | 内容 | 依赖 |
|------|------|------|
| **第1期 (P0)** | TOML 配置重写 + 抽象层定义 + `shared/auth/` 公共模块 + B站 QR 登录 + B站主动续期 + `login`/`token` CLI 命令 | 无 |
| **第2期 (P1)** | 微博平台完整功能 (QR 登录 + 监控 + 摘要 + 通知 + 主页保活续期) | 第1期 |
| **第3期 (P2)** | 小红书 QR 登录 + 签名模块验证 | 第1期 + vendor 签名模块 |
| **第4期 (P3)** | 通用续期调度统一集成 + 各平台 token 生命周期管理 | 第1-2期 |

## 依赖

### 新增 Python 包

| 包 | 用途 | 期数 |
|---|------|------|
| `qrcode` | 终端二维码渲染 | 第1期 |
| `tomlkit` | TOML 读写（保留注释和格式） | 第1期 |

### 已有依赖（无变化）

- `bilibili-api-python` — B站 QR 登录和 Cookie 续期
- `aiohttp` — HTTP 请求
- `click` — CLI 框架
- `rich` — 终端输出

### 标准库（Python 3.11+）

- `tomllib` — TOML 读取（标准库，无需安装）

### 可选依赖

| 包 | 用途 | 期数 |
|---|------|------|
| `PyExecJS` | 小红书签名执行 | 第3期 |
| `Node.js` (系统) | 小红书签名 JS 运行时 | 第3期 |

## 迁移策略

**不兼容旧 `config.yaml` 格式。全新 TOML 配置结构。**

### 现有接口迁移

**B站 `get_credential(config)`:**
- 重构为从 `Config.bilibili.auth` 读取凭证构建 `bilibili_api.Credential`
- 调用方（`monitor.py`, `comments.py`, `dynamic.py`）需适配新 Config 结构

**小红书 `get_xhs_cookie(config) -> str`:**
- 重构为从 `Config.xiaohongshu.auth.cookie` 读取
- 调用方需适配新 Config 结构

### 数据格式

各平台 `BaseAuthenticator.get_tokens()` 返回 `PlatformTokens`，其 `cookies` 统一为 `dict[str, str]`：
- B站: `{SESSDATA, bili_jct, buvid3, DedeUserID}` — `ac_time_value` 通过 config 的 `BilibiliAuth` 管理
- 微博: `{SUB, SUBP, WBPSESS, SSOLoginState}`
- 小红书: `{a1, web_session, webId, ...}`

各平台认证器内部负责 `PlatformTokens` ↔ 平台特有配置字段（如 `BilibiliAuth`）的转换。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| B站 `Credential.refresh()` 可能有 bug | 自动续期失败 | 续期前检查 `ac_time_value` 是否存在；续期失败后通知用户重新扫码 |
| 小红书签名 JS 更新频繁 | 第3期 XHS QR 可能失效 | vendor 模块更新机制；降级为 Cookie 手动填写 |
| 微博 cookie 7天过期 | 保活失败时需重新扫码 | 主动续期策略（过期前 7 天强制续期）+ 过期提醒通知 |
| B站 QR 风控 (412) | QR 生成/轮询失败 | 轮询间隔 >= 2s；合理 User-Agent |
| `ac_time_value` 丢失 | B站 Cookie 续期不可用 | 持久化到 config.toml + 续期失败后引导重新扫码 |
| TOML 配置不兼容旧 YAML | 用户需手动迁移 | 提供 `config.toml.example` 模板 + 首次运行提示配置 |
