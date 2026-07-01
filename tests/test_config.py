"""Tests for shared/config.py — TOML-based configuration"""

from __future__ import annotations

from pathlib import Path

from shared.config import (
    AnalysisConfig,
    AuthGlobalConfig,
    BilibiliAuth,
    BilibiliConfig,
    BilibiliMonitorConfig,
    BiliSubscription,
    Config,
    DownloadConfig,
    EndpointConfig,
    GeneralConfig,
    RenewalConfig,
    TranscribeConfig,
    UserSubscription,
    WeiboAuth,
    WeiboConfig,
    WeiboMonitorConfig,
    XhsAuth,
    XhsConfig,
    XhsMonitorConfig,
    _dict_to_dataclass,
    load_config,
)

# ── Test TOML fixtures ────────────────────────────────────────

BASE_TOML = """
[general]
    data_dir = "/data/trawler"

[auth.renewal]
min_interval_hours = 12
force_before_days = 3
check_interval_hours = 4

[download]
dir = "/media/downloads"
quality = "best"
format = "bestvideo+bestaudio"
max_concurrent = 5

[transcribe]
model = "large"
language = "en"
output_dir = "/transcripts"
delete_after_transcribe = false

[analysis]
enabled = false
provider = "openai"
api_base = "https://api.openai.com/v1"
api_key = "sk-test123"
model_name = "gpt-4"

[bilibili.monitor]
mode = "api"
watch_dynamic = false
max_videos_per_check = 20
rsshub_instances = ["https://custom.rsshub.local"]

[xiaohongshu]
enabled = true

[xiaohongshu.monitor]
mode = "rss"

[weibo]
enabled = true

[weibo.monitor]
mode = "api"
"""

COOKIES_TOML = """
[bilibili.auth]
sessdata = "bili_sess"
bili_jct = "bili_jct_val"
buvid3 = "buvid3_val"
dedeuserid = "12345"
refresh_token = "ac123"
expires_at = 1735689600.0

[xiaohongshu.auth]
cookie = "xhs_cookie_val"
expires_at = 1735689600.0

[weibo.auth]
cookie = "weibo_cookie_val"
expires_at = 1735689600.0
"""

SUBS_TOML = """
[[bilibili.subscriptions]]
uid = 1001
name = "UP主A"

[[bilibili.subscriptions]]
uid = 2002
name = "UP主B"

[[xiaohongshu.subscriptions]]
user_id = "xhs_user1"
name = "博主A"

[[weibo.subscriptions]]
user_id = "weibo_user1"
name = "博主B"
"""

MINIMAL_COOKIES_TOML = """
[bilibili.auth]
sessdata = "abc123"
"""


def _write_full_config(tmp_path) -> Path:
    """Write all three config files (base + cookies + subscriptions) to tmp_path.

    Returns the path to config.toml.
    """
    p = tmp_path / "config.toml"
    p.write_text(BASE_TOML, encoding="utf-8")
    (tmp_path / "cookies.toml").write_text(COOKIES_TOML, encoding="utf-8")
    (tmp_path / "subscriptions.toml").write_text(SUBS_TOML, encoding="utf-8")
    return p


# ── 1. Missing file → returns Config() defaults ───────────────


class TestMissingFile:
    async def test_missing_file_returns_defaults(self, tmp_path):
        cfg = await load_config(tmp_path / "nonexistent.toml")
        assert isinstance(cfg, Config)
        assert cfg == Config()

    async def test_missing_file_all_defaults(self, tmp_path):
        cfg = await load_config(tmp_path / "nonexistent.toml")
        # Spot-check all major sections
        assert cfg.general.data_dir == "./data"
        assert cfg.auth.renewal.min_interval_hours == 24
        assert cfg.download.dir == "./downloads"
        assert cfg.transcribe.model == "base"
        assert cfg.analysis.enabled is True
        assert cfg.bilibili.auth.sessdata == ""
        assert cfg.bilibili.monitor.mode == "rss"
        assert cfg.bilibili.subscriptions == []
        assert cfg.endpoints == []
        assert cfg.xiaohongshu.enabled is False
        assert cfg.xiaohongshu.auth.cookie == ""
        assert cfg.weibo.enabled is False
        assert cfg.weibo.auth.cookie == ""


# ── 2. Empty TOML file → returns Config() defaults ────────────


class TestEmptyToml:
    async def test_empty_file_returns_defaults(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("", encoding="utf-8")
        cfg = await load_config(p)
        assert cfg == Config()


# ── 3. Full TOML config ───────────────────────────────────────


class TestFullToml:
    async def test_general(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.general.data_dir == "/data/trawler"

    async def test_auth_renewal(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.auth.renewal.min_interval_hours == 12
        assert cfg.auth.renewal.force_before_days == 3
        assert cfg.auth.renewal.check_interval_hours == 4

    async def test_download(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.download.dir == "/media/downloads"
        assert cfg.download.quality == "best"
        assert cfg.download.format == "bestvideo+bestaudio"
        assert cfg.download.max_concurrent == 5

    async def test_transcribe(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.transcribe.model == "large"
        assert cfg.transcribe.language == "en"
        assert cfg.transcribe.output_dir == "/transcripts"
        assert cfg.transcribe.delete_after_transcribe is False

    async def test_analysis(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.analysis.enabled is False
        assert cfg.analysis.provider == "openai"
        assert cfg.analysis.api_base == "https://api.openai.com/v1"
        assert cfg.analysis.api_key == "sk-test123"
        assert cfg.analysis.model_name == "gpt-4"

    async def test_bilibili_auth(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.bilibili.auth.sessdata == "bili_sess"
        assert cfg.bilibili.auth.bili_jct == "bili_jct_val"
        assert cfg.bilibili.auth.buvid3 == "buvid3_val"
        assert cfg.bilibili.auth.dedeuserid == "12345"
        assert cfg.bilibili.auth.refresh_token == "ac123"
        assert cfg.bilibili.auth.expires_at == 1735689600.0

    async def test_bilibili_monitor(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.bilibili.monitor.mode == "api"
        assert cfg.bilibili.monitor.watch_dynamic is False
        assert cfg.bilibili.monitor.max_videos_per_check == 20
        assert cfg.bilibili.monitor.rsshub_instances == ["https://custom.rsshub.local"]

    async def test_bilibili_subscriptions(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert len(cfg.bilibili.subscriptions) == 2
        assert cfg.bilibili.subscriptions[0] == BiliSubscription(uid=1001, name="UP主A")
        assert cfg.bilibili.subscriptions[1] == BiliSubscription(uid=2002, name="UP主B")

    async def test_xiaohongshu(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.xiaohongshu.enabled is True
        assert cfg.xiaohongshu.auth.cookie == "xhs_cookie_val"
        assert cfg.xiaohongshu.auth.expires_at == 1735689600.0
        assert cfg.xiaohongshu.monitor.mode == "rss"
        assert len(cfg.xiaohongshu.subscriptions) == 1
        assert cfg.xiaohongshu.subscriptions[0] == UserSubscription(user_id="xhs_user1", name="博主A")

    async def test_weibo(self, tmp_path):
        p = _write_full_config(tmp_path)
        cfg = await load_config(p)
        assert cfg.weibo.enabled is True
        assert cfg.weibo.auth.cookie == "weibo_cookie_val"
        assert cfg.weibo.auth.expires_at == 1735689600.0
        assert cfg.weibo.monitor.mode == "api"
        assert len(cfg.weibo.subscriptions) == 1
        assert cfg.weibo.subscriptions[0] == UserSubscription(user_id="weibo_user1", name="博主B")


# ── 4. Minimal TOML ──────────────────────────────────────────


class TestMinimalToml:
    async def test_only_sessdata_set(self, tmp_path):
        # Write an empty config.toml + minimal cookies.toml
        p = tmp_path / "config.toml"
        p.write_text("", encoding="utf-8")
        (tmp_path / "cookies.toml").write_text(MINIMAL_COOKIES_TOML, encoding="utf-8")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.sessdata == "abc123"
        # Everything else should be defaults
        assert cfg.bilibili.auth.bili_jct == ""
        assert cfg.bilibili.auth.buvid3 == ""
        assert cfg.general.data_dir == "./data"
        assert cfg.download.dir == "./downloads"
        assert cfg.transcribe.model == "base"
        assert cfg.analysis.enabled is True
        assert cfg.bilibili.monitor.mode == "rss"
        assert cfg.bilibili.subscriptions == []
        assert cfg.endpoints == []
        assert cfg.xiaohongshu.enabled is False
        assert cfg.weibo.enabled is False


# ── 5. Env var overrides ──────────────────────────────────────


class TestEnvOverrides:
    async def test_trawler_xhs_cookie(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_XHS_COOKIE", "override-xhs-cookie")
        cfg = await load_config(p)
        assert cfg.xiaohongshu.auth.cookie == "override-xhs-cookie"

    async def test_trawler_weibo_cookie(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_WEIBO_COOKIE", "override-weibo-cookie")
        cfg = await load_config(p)
        assert cfg.weibo.auth.cookie == "override-weibo-cookie"

    async def test_trawler_llm_api_key(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_LLM_API_KEY", "override-api-key")
        cfg = await load_config(p)
        assert cfg.analysis.api_key == "override-api-key"

    async def test_trawler_llm_api_base(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_LLM_API_BASE", "https://override.api.com")
        cfg = await load_config(p)
        assert cfg.analysis.api_base == "https://override.api.com"

    async def test_trawler_bili_sessdata(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_BILI_SESSDATA", "override-sessdata")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.sessdata == "override-sessdata"

    async def test_trawler_bili_refresh_token(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_BILI_REFRESH_TOKEN", "override-refresh")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.refresh_token == "override-refresh"

    async def test_env_override_with_empty_config(self, tmp_path, monkeypatch):
        """Env vars should work even when no config file exists."""
        monkeypatch.setenv("TRAWLER_XHS_COOKIE", "env-cookie")
        monkeypatch.setenv("TRAWLER_LLM_API_KEY", "env-key")
        cfg = await load_config(tmp_path / "nonexistent.toml")
        assert cfg.xiaohongshu.auth.cookie == "env-cookie"
        assert cfg.analysis.api_key == "env-key"

    async def test_trawler_bili_jct(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_BILI_JCT", "override-jct")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.bili_jct == "override-jct"

    async def test_trawler_bili_buvid3(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_BILI_BUVID3", "override-buvid")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.buvid3 == "override-buvid"

    async def test_trawler_bili_dedeuserid(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_BILI_DEDEUSERID", "override-dede")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.dedeuserid == "override-dede"

    async def test_trawler_llm_model(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_LLM_MODEL", "gpt-4o")
        cfg = await load_config(p)
        assert cfg.analysis.model_name == "gpt-4o"

    async def test_trawler_llm_provider(self, tmp_path, monkeypatch):
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_LLM_PROVIDER", "ollama")
        cfg = await load_config(p)
        assert cfg.analysis.provider == "ollama"

    async def test_env_overrides_cover_bili_credentials_and_llm_provider(self, tmp_path, monkeypatch):
        """B 站三件套 + LLM model/provider 可用 env 覆盖 (#66)。"""
        p = _write_full_config(tmp_path)
        monkeypatch.setenv("TRAWLER_BILI_JCT", "test_jct")
        monkeypatch.setenv("TRAWLER_BILI_BUVID3", "test_buvid")
        monkeypatch.setenv("TRAWLER_BILI_DEDEUSERID", "test_dede")
        monkeypatch.setenv("TRAWLER_LLM_MODEL", "gpt-4o")
        monkeypatch.setenv("TRAWLER_LLM_PROVIDER", "openai")
        cfg = await load_config(p)
        assert cfg.bilibili.auth.bili_jct == "test_jct"
        assert cfg.bilibili.auth.buvid3 == "test_buvid"
        assert cfg.bilibili.auth.dedeuserid == "test_dede"
        assert cfg.analysis.model_name == "gpt-4o"
        assert cfg.analysis.provider == "openai"


# ── 6. Dataclass defaults verification ────────────────────────


class TestDataclassDefaults:
    def test_renewal_config_defaults(self):
        r = RenewalConfig()
        assert r.min_interval_hours == 24
        assert r.force_before_days == 7
        assert r.check_interval_hours == 6

    def test_auth_global_config_defaults(self):
        a = AuthGlobalConfig()
        assert isinstance(a.renewal, RenewalConfig)

    def test_bilibili_auth_defaults(self):
        a = BilibiliAuth()
        assert a.sessdata == ""
        assert a.bili_jct == ""
        assert a.buvid3 == ""
        assert a.dedeuserid == ""
        assert a.refresh_token == ""
        assert a.expires_at == 0.0

    def test_xhs_auth_defaults(self):
        a = XhsAuth()
        assert a.cookie == ""
        assert a.expires_at == 0.0

    def test_weibo_auth_defaults(self):
        a = WeiboAuth()
        assert a.cookie == ""
        assert a.expires_at == 0.0

    def test_download_config_defaults(self):
        d = DownloadConfig()
        assert d.dir == "./downloads"
        assert d.quality == "worst"
        assert d.format == "bestaudio/worst"
        assert d.max_concurrent == 3

    def test_transcribe_config_defaults(self):
        t = TranscribeConfig()
        assert t.model == "base"
        assert t.language == "zh"
        assert t.output_dir == "./transcripts"
        assert t.delete_after_transcribe is True

    def test_bilibili_monitor_config_defaults(self):
        m = BilibiliMonitorConfig()
        assert m.mode == "rss"
        assert m.watch_dynamic is True
        assert m.max_videos_per_check == 10
        assert m.rsshub_instances == [
            "https://rsshub.yfi.moe",
            "https://rsshub.liumingye.cn",
            "https://rss.shab.fun",
        ]

    def test_xhs_monitor_config_defaults(self):
        m = XhsMonitorConfig()
        assert m.mode == "api"

    def test_weibo_monitor_config_defaults(self):
        m = WeiboMonitorConfig()
        assert m.mode == "api"

    def test_analysis_config_defaults(self):
        a = AnalysisConfig()
        assert a.enabled is True
        assert a.provider == "openai"
        assert a.api_base == ""
        assert a.api_key == ""
        assert a.model_name == ""

    def test_bili_subscription_defaults(self):
        s = BiliSubscription()
        assert s.uid == 0
        assert s.name == ""
        assert s.notify_endpoints == []

    def test_user_subscription_defaults(self):
        s = UserSubscription()
        assert s.user_id == ""
        assert s.name == ""
        assert s.notify_endpoints == []

    def test_bilibili_config_defaults(self):
        b = BilibiliConfig()
        assert isinstance(b.auth, BilibiliAuth)
        assert isinstance(b.monitor, BilibiliMonitorConfig)
        assert b.subscriptions == []

    def test_xhs_config_defaults(self):
        x = XhsConfig()
        assert x.enabled is False
        assert isinstance(x.auth, XhsAuth)
        assert isinstance(x.monitor, XhsMonitorConfig)
        assert x.subscriptions == []

    def test_weibo_config_defaults(self):
        w = WeiboConfig()
        assert w.enabled is False
        assert isinstance(w.auth, WeiboAuth)
        assert isinstance(w.monitor, WeiboMonitorConfig)
        assert w.subscriptions == []

    def test_config_defaults(self):
        c = Config()
        assert isinstance(c.general, GeneralConfig)
        assert isinstance(c.auth, AuthGlobalConfig)
        assert isinstance(c.download, DownloadConfig)
        assert isinstance(c.transcribe, TranscribeConfig)
        assert isinstance(c.analysis, AnalysisConfig)
        assert isinstance(c.bilibili, BilibiliConfig)
        assert isinstance(c.xiaohongshu, XhsConfig)
        assert isinstance(c.weibo, WeiboConfig)


# ── 7. _dict_to_dataclass tests ───────────────────────────────


class TestDictToDataclass:
    def test_simple_conversion(self):
        data = {"dir": "/tmp", "quality": "best"}
        result = _dict_to_dataclass(DownloadConfig, data)
        assert result.dir == "/tmp"
        assert result.quality == "best"
        # Unset fields should still use defaults
        assert result.format == "bestaudio/worst"
        assert result.max_concurrent == 3

    def test_ignores_unknown_fields(self):
        data = {"dir": "/tmp", "unknown_field": "ignored"}
        result = _dict_to_dataclass(DownloadConfig, data)
        assert result.dir == "/tmp"
        # Should not raise

    def test_nested_conversion(self):
        data = {"renewal": {"min_interval_hours": 48}}
        result = _dict_to_dataclass(AuthGlobalConfig, data)
        assert result.renewal.min_interval_hours == 48
        assert result.renewal.force_before_days == 7  # default preserved

    def test_non_dict_passthrough(self):
        result = _dict_to_dataclass(DownloadConfig, "not a dict")
        assert result == "not a dict"

    def test_empty_dict(self):
        result = _dict_to_dataclass(DownloadConfig, {})
        assert result == DownloadConfig()


# ── 8. Default path is config.toml ────────────────────────────


class TestDefaultPath:
    async def test_load_config_default_path_is_toml(self, tmp_path, monkeypatch):
        """Verify that load_config() defaults to 'config/config.toml'."""
        monkeypatch.chdir(tmp_path)
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        p = cfg_dir / "config.toml"
        p.write_text("", encoding="utf-8")
        (cfg_dir / "cookies.toml").write_text(MINIMAL_COOKIES_TOML, encoding="utf-8")
        cfg = await load_config()
        assert cfg.bilibili.auth.sessdata == "abc123"


# ── 9. Endpoint / notify_endpoints (new notifier abstraction) ─


class TestEndpointConfig:
    def test_endpoint_config_defaults(self):
        ep = EndpointConfig(name="default", url="https://g.example.com", token="tk")
        assert ep.priority == 5
        assert ep.enabled is True
        assert ep.kind == "gotify"

    def test_bili_subscription_notify_endpoints(self):
        s = BiliSubscription(uid=1, name="x", notify_endpoints=["a", "b"])
        assert s.notify_endpoints == ["a", "b"]

    def test_config_endpoints_parsed(self):
        from shared.config import _parse_config

        raw = {"endpoints": [{"name": "ep1", "url": "u", "token": "t"}]}
        cfg = _parse_config(raw)
        assert len(cfg.endpoints) == 1
        assert cfg.endpoints[0].name == "ep1"


# ── 10. Multi-provider fallback chain (plan 2026-06-28) ────────


class TestMultiProviderConfig:
    """LLMProviderConfig + AnalysisConfig.providers_chain + 向上兼容解析。"""

    def test_parse_config_legacy_single_provider_still_works(self) -> None:
        """旧 config.toml（无 extra_providers）必须 100% 向上兼容。"""
        from shared.config import _parse_config

        raw = {
            "analysis": {
                "enabled": True,
                "provider": "openai",
                "api_base": "https://x",
                "api_key": "k",
            }
        }
        cfg = _parse_config(raw)
        assert cfg.analysis.enabled is True
        assert cfg.analysis.provider == "openai"
        assert cfg.analysis.api_base == "https://x"
        assert cfg.analysis.api_key == "k"
        assert cfg.analysis.extra_providers == []

    def test_parse_config_extra_providers_list_parsed(self) -> None:
        from shared.config import LLMProviderConfig, _parse_config

        raw = {
            "analysis": {
                "provider": "openai",
                "api_base": "https://primary",
                "api_key": "k1",
                "model_name": "gpt-4o-mini",
                "extra_providers": [
                    {
                        "provider": "openai",
                        "api_base": "https://secondary",
                        "api_key": "k2",
                        "model_name": "gpt-4o",
                    },
                    {
                        "provider": "ollama",
                        "api_base": "http://local:11434/v1",
                        "model_name": "qwen2.5:7b",
                    },
                ],
            }
        }
        cfg = _parse_config(raw)
        assert len(cfg.analysis.extra_providers) == 2
        assert isinstance(cfg.analysis.extra_providers[0], LLMProviderConfig)
        assert cfg.analysis.extra_providers[0].api_base == "https://secondary"
        assert cfg.analysis.extra_providers[1].provider == "ollama"

    def test_analysis_providers_chain_property_returns_main_plus_extras(self) -> None:
        """AnalysisConfig.providers_chain 是统一访问入口（主 + 备用 list）。"""
        from shared.config import AnalysisConfig, LLMProviderConfig

        cfg = AnalysisConfig(provider="openai", api_base="https://x", api_key="k")
        cfg.extra_providers = [LLMProviderConfig(provider="ollama", api_base="http://l:11434/v1")]
        chain = cfg.providers_chain
        assert len(chain) == 2
        assert chain[0].api_base == "https://x"  # 主 provider 在前
        assert chain[1].provider == "ollama"

    def test_analysis_providers_chain_empty_when_main_unconfigured(self) -> None:
        """主 provider 未配置 api_base 时返回空链（disabled 语义）。

        覆盖两类「未配置」：
        - enabled=False → 空（disabled）
        - enabled=True 但主 provider api_base='' → 空（残留默认值场景，避免退化）
        """
        from shared.config import AnalysisConfig

        cfg = AnalysisConfig(enabled=False)
        assert cfg.providers_chain == []
        cfg2 = AnalysisConfig(enabled=True)  # api_base 默认 ""
        assert cfg2.providers_chain == []

    def test_analysis_providers_chain_skips_unconfigured_main_but_keeps_extras(self) -> None:
        """主 provider api_base 为空但 extra_providers 配了 → 链里跳过主，只用 extras。"""
        from shared.config import AnalysisConfig, LLMProviderConfig

        cfg = AnalysisConfig(enabled=True)  # 主 provider api_base 默认空
        cfg.extra_providers = [
            LLMProviderConfig(provider="ollama", api_base="http://l:11434/v1"),
            LLMProviderConfig(provider="openai", api_base="https://x", api_key="k"),
        ]
        chain = cfg.providers_chain
        assert len(chain) == 2
        assert chain[0].provider == "ollama"
        assert chain[1].api_base == "https://x"
