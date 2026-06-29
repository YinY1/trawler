"""配置管理模块 — TOML 驱动，dataclass 结构，支持环境变量覆盖"""

from __future__ import annotations

# pyright: basic
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 认证续期配置 ──────────────────────────────────────────────


@dataclass
class RenewalConfig:
    min_interval_hours: int = 24
    force_before_days: int = 7
    check_interval_hours: int = 6
    max_interval_hours: int = 24  # 距上次刷新尝试超过此小时数也触发刷新


@dataclass
class AuthGlobalConfig:
    renewal: RenewalConfig = field(default_factory=RenewalConfig)


# ── 平台认证凭证 ──────────────────────────────────────────────


@dataclass
class BilibiliAuth:
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    dedeuserid: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    last_refresh_at: float = 0.0  # 上次刷新尝试的时间戳


@dataclass
class XhsAuth:
    cookie: str = ""
    expires_at: float = 0.0
    nickname: str = ""


@dataclass
class WeiboAuth:
    cookie: str = ""
    expires_at: float = 0.0
    nickname: str = ""


# ── 下载配置 ──────────────────────────────────────────────────


@dataclass
class DownloadConfig:
    """媒体下载配置"""

    dir: str = "./downloads"
    quality: str = "worst"
    format: str = "bestaudio/worst"
    max_concurrent: int = 3


# ── 转写配置 ──────────────────────────────────────────────────


@dataclass
class TranscribeConfig:
    """转写引擎配置"""

    model: str = "base"
    language: str = "zh"
    output_dir: str = "./transcripts"
    delete_after_transcribe: bool = True


# ── 监控配置（各平台独立）──────────────────────────────────────


@dataclass
class BilibiliMonitorConfig:
    mode: str = "rss"
    interval_minutes: int = 3
    watch_dynamic: bool = True
    max_videos_per_check: int = 10
    rsshub_instances: list[str] = field(
        default_factory=lambda: [
            "https://rsshub.yfi.moe",
            "https://rsshub.liumingye.cn",
            "https://rss.shab.fun",
        ]
    )


@dataclass
class XhsMonitorConfig:
    mode: str = "api"
    interval_minutes: int = 10


@dataclass
class WeiboMonitorConfig:
    mode: str = "api"
    interval_minutes: int = 10


# ── AI 分析配置 ───────────────────────────────────────────────


@dataclass
class LLMProviderConfig:
    """单个 LLM provider 配置（fallback 链的一节）。

    与 ``AnalysisConfig`` 的关系：
    - ``AnalysisConfig`` 顶层有 ``enabled`` 全局开关 + 旧的 4 个字段（作为「主 provider」）
    - ``AnalysisConfig.extra_providers`` 是 ``list[LLMProviderConfig]``（备用链，按序 fallback）
    """

    name: str = ""  # 可选标识符（仅日志用，无 name 时用 provider+api_base）
    provider: str = "openai"
    api_base: str = ""
    api_key: str = ""
    model_name: str = ""


@dataclass
class AnalysisConfig:
    """AI 分析配置。

    支持两种配置方式（向上兼容）：
    1. **单 provider（旧）**：直接填 ``provider`` / ``api_base`` / ``api_key`` / ``model_name``
    2. **多 provider fallback（新）**：填上面的主 provider + ``extra_providers`` 列表。
       ``providers_chain`` property 返回 [主 provider, *extra_providers] 作为 fallback 链。

    ``enabled=False`` 时 ``providers_chain`` 返回空列表。
    """

    enabled: bool = True
    provider: str = "openai"
    api_base: str = ""
    api_key: str = ""
    model_name: str = ""
    # fallback 链（按序尝试，前一个失败才用下一个）
    extra_providers: list[LLMProviderConfig] = field(default_factory=list)

    @property
    def providers_chain(self) -> list[LLMProviderConfig]:
        """统一访问入口：返回 fallback 链。

        ``enabled=False`` 时返回空链（disabled 语义）。

        退化修复：``enabled=True`` 但主 provider ``api_base`` 为空时，主 provider
        被跳过（视为「未配置」，与 disabled 语义对齐）。避免旧配置残留默认值场景
        （enabled=true 且 api_base/api_key 都为空）退化成卡 SUMMARIZED：旧版本直接
        source="none" 不卡，新版本若主 provider 进入链会让 create_provider
        抛 ValueError → analyze_content except 后 failed=True 卡住。
        """
        if not self.enabled:
            return []
        chain: list[LLMProviderConfig] = []
        # 主 provider：api_base 非空才纳入链（避免残留默认值退化）
        if self.api_base:
            chain.append(
                LLMProviderConfig(
                    provider=self.provider,
                    api_base=self.api_base,
                    api_key=self.api_key,
                    model_name=self.model_name,
                )
            )
        chain.extend(self.extra_providers)
        return chain


# ── 推送端点配置 ───────────────────────────────────────────────


@dataclass
class EndpointConfig:
    """Gotify 推送端点（全局列表，订阅通过 name 引用）。"""

    name: str
    url: str
    token: str
    priority: int = 5
    enabled: bool = True
    kind: str = "gotify"  # 预留："gotify" | "telegram" | "email"


# ── 订阅条目 ──────────────────────────────────────────────────


@dataclass
class BiliSubscription:
    uid: int = 0
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)


@dataclass
class UserSubscription:
    user_id: str = ""
    name: str = ""
    notify_endpoints: list[str] = field(default_factory=list)


# ── 平台配置 ──────────────────────────────────────────────────


@dataclass
class BilibiliConfig:
    auth: BilibiliAuth = field(default_factory=BilibiliAuth)
    monitor: BilibiliMonitorConfig = field(default_factory=BilibiliMonitorConfig)
    subscriptions: list[BiliSubscription] = field(default_factory=list)


@dataclass
class XhsConfig:
    enabled: bool = False
    auth: XhsAuth = field(default_factory=XhsAuth)
    monitor: XhsMonitorConfig = field(default_factory=XhsMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)


@dataclass
class WeiboConfig:
    enabled: bool = False
    auth: WeiboAuth = field(default_factory=WeiboAuth)
    monitor: WeiboMonitorConfig = field(default_factory=WeiboMonitorConfig)
    subscriptions: list[UserSubscription] = field(default_factory=list)


# ── Web 站点访问鉴权 ──────────────────────────────────────────


@dataclass
class WebAuthConfig:
    """Web UI 访问鉴权配置。

    存储在 ``data/auth.toml``（独立于主 ``config/config.toml``）。
    username 固定为 ``admin``，本 dataclass 不含 username 字段。
    """

    admin_password_hash: str = ""
    session_secret: str = ""
    session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 天


# ── 顶层配置 ──────────────────────────────────────────────────


@dataclass
class GeneralConfig:
    data_dir: str = "./data"
    disable_ssl_verify: bool = False


@dataclass
class Config:
    """Trawler 全局配置"""

    general: GeneralConfig = field(default_factory=GeneralConfig)
    auth: AuthGlobalConfig = field(default_factory=AuthGlobalConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    bilibili: BilibiliConfig = field(default_factory=BilibiliConfig)
    xiaohongshu: XhsConfig = field(default_factory=XhsConfig)
    weibo: WeiboConfig = field(default_factory=WeiboConfig)
    endpoints: list[EndpointConfig] = field(default_factory=list)


# ── 辅助函数 ──────────────────────────────────────────────────


def _dict_to_dataclass(cls, data: dict):
    """Recursively convert dict to dataclass, ignoring unknown fields."""
    if not isinstance(data, dict):
        return data
    valid_keys = {f.name for f in fields(cls)}
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        field_type = f.type
        # Handle string annotations
        if isinstance(field_type, str):
            field_type = globals().get(field_type, type(None))
        if hasattr(field_type, "__dataclass_fields__") and isinstance(value, dict):
            kwargs[f.name] = _dict_to_dataclass(field_type, value)
        else:
            kwargs[f.name] = value
    return cls(**{k: v for k, v in kwargs.items() if k in valid_keys})


def _parse_config(raw: dict) -> Config:
    """将原始 TOML 字典解析为 Config dataclass"""
    cfg = Config()

    # general
    if general := raw.get("general"):
        cfg.general = _dict_to_dataclass(GeneralConfig, general)

    # auth.renewal
    if auth_raw := raw.get("auth"):
        if renewal_raw := auth_raw.get("renewal"):
            cfg.auth.renewal = _dict_to_dataclass(RenewalConfig, renewal_raw)

    # download, transcribe, analysis
    if dl := raw.get("download"):
        cfg.download = _dict_to_dataclass(DownloadConfig, dl)
    if tr := raw.get("transcribe"):
        cfg.transcribe = _dict_to_dataclass(TranscribeConfig, tr)
    if ana := raw.get("analysis"):
        # 单独处理 extra_providers（list of dict → list[LLMProviderConfig]）
        extras_raw = ana.get("extra_providers", [])
        extras = [
            _dict_to_dataclass(LLMProviderConfig, ep) if isinstance(ep, dict) else ep
            for ep in extras_raw
        ]
        ana_no_extras = {k: v for k, v in ana.items() if k != "extra_providers"}
        cfg.analysis = _dict_to_dataclass(AnalysisConfig, ana_no_extras)
        cfg.analysis.extra_providers = extras

    # bilibili
    if bili := raw.get("bilibili"):
        auth = _dict_to_dataclass(BilibiliAuth, bili.get("auth", {}))
        monitor = _dict_to_dataclass(BilibiliMonitorConfig, bili.get("monitor", {}))
        subs = [BiliSubscription(**s) for s in bili.get("subscriptions", [])]
        cfg.bilibili = BilibiliConfig(auth=auth, monitor=monitor, subscriptions=subs)

    # xiaohongshu
    if xhs := raw.get("xiaohongshu"):
        auth = _dict_to_dataclass(XhsAuth, xhs.get("auth", {}))
        monitor = _dict_to_dataclass(XhsMonitorConfig, xhs.get("monitor", {}))
        subs = [UserSubscription(**s) for s in xhs.get("subscriptions", [])]
        cfg.xiaohongshu = XhsConfig(
            enabled=xhs.get("enabled", False),
            auth=auth,
            monitor=monitor,
            subscriptions=subs,
        )

    # weibo
    if wb := raw.get("weibo"):
        auth = _dict_to_dataclass(WeiboAuth, wb.get("auth", {}))
        monitor = _dict_to_dataclass(WeiboMonitorConfig, wb.get("monitor", {}))
        subs = [UserSubscription(**s) for s in wb.get("subscriptions", [])]
        cfg.weibo = WeiboConfig(
            enabled=wb.get("enabled", False),
            auth=auth,
            monitor=monitor,
            subscriptions=subs,
        )

    # endpoints（全局）
    if eps := raw.get("endpoints"):
        cfg.endpoints = [EndpointConfig(**ep) for ep in eps]

    return cfg


def _apply_env_overrides(cfg: Config) -> None:
    """环境变量覆盖配置值，优先级高于配置文件"""
    # 平台 cookies
    if v := os.environ.get("TRAWLER_XHS_COOKIE"):
        cfg.xiaohongshu.auth.cookie = v
    if v := os.environ.get("TRAWLER_WEIBO_COOKIE"):
        cfg.weibo.auth.cookie = v
    if v := os.environ.get("TRAWLER_BILI_SESSDATA"):
        cfg.bilibili.auth.sessdata = v
    if v := os.environ.get("TRAWLER_BILI_REFRESH_TOKEN"):
        cfg.bilibili.auth.refresh_token = v
    # AI
    if v := os.environ.get("TRAWLER_LLM_API_KEY"):
        cfg.analysis.api_key = v
    if v := os.environ.get("TRAWLER_LLM_API_BASE"):
        cfg.analysis.api_base = v


async def load_config(path: str | Path = "config/config.toml") -> Config:
    """从 TOML 文件加载配置并应用环境变量覆盖。

    配置从三个文件合并加载（均位于 ``config/`` 目录下）：
    - ``config/config.toml``: 基础配置
    - ``config/cookies.toml``: 平台登录凭证
    - ``config/subscriptions.toml``: 订阅列表
    """
    p = Path(path)
    logger.debug("⚙️ 加载配置: %s", p)

    raw: dict = {}

    # ── 1. 加载基础配置 ────────────────────────────────────────
    if p.exists():
        with open(p, "rb") as f:
            raw = tomllib.load(f)

    # ── 2. 合并凭证配置（config/cookies.toml）────────────────
    cookies_path = p.with_name("cookies.toml")
    if cookies_path.exists():
        logger.debug("⚙️ 合并凭证: %s", cookies_path)
        with open(cookies_path, "rb") as f:
            cookies_raw: dict = tomllib.load(f)
        for _platform in ("bilibili", "xiaohongshu", "weibo"):
            _entry = cookies_raw.get(_platform)
            if isinstance(_entry, dict) and "auth" in _entry:
                if _platform not in raw:
                    raw[_platform] = {}
                raw[_platform]["auth"] = _entry["auth"]

    # ── 3. 合并订阅列表（config/subscriptions.toml）──────────
    subs_path = p.with_name("subscriptions.toml")
    if subs_path.exists():
        logger.debug("⚙️ 合并订阅: %s", subs_path)
        with open(subs_path, "rb") as f:
            subs_raw: dict = tomllib.load(f)
        for _platform in ("bilibili", "xiaohongshu", "weibo"):
            _entry = subs_raw.get(_platform)
            if isinstance(_entry, dict) and "subscriptions" in _entry:
                if _platform not in raw:
                    raw[_platform] = {}
                raw[_platform]["subscriptions"] = _entry["subscriptions"]

    cfg = _parse_config(raw)
    _apply_env_overrides(cfg)
    logger.debug("⚙️ 配置加载完成")
    return cfg
