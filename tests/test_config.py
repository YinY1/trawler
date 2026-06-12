"""Tests for shared/config.py — TOML-based configuration"""

import os
import pytest
from pathlib import Path

from shared.config import (
    Config,
    GeneralConfig,
    AuthGlobalConfig,
    RenewalConfig,
    BilibiliAuth,
    XhsAuth,
    WeiboAuth,
    DownloadConfig,
    TranscribeConfig,
    BilibiliMonitorConfig,
    XhsMonitorConfig,
    WeiboMonitorConfig,
    AnalysisConfig,
    NotificationConfig,
    BiliSubscription,
    UserSubscription,
    BilibiliConfig,
    XhsConfig,
    WeiboConfig,
    load_config,
    _dict_to_dataclass,
)


# ── Fixtures ──────────────────────────────────────────────────

MINIMAL_TOML = """
[bilibili.auth]
sessdata = "abc123"
"""

FULL_TOML = """
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

[bilibili.auth]
sessdata = "bili_sess"
bili_jct = "bili_jct_val"
buvid3 = "buvid3_val"
dedeuserid = "12345"
ac_time_value = "ac123"
expires_at = 1735689600.0

[bilibili.monitor]
mode = "api"
interval_minutes = 5
watch_dynamic = false
max_videos_per_check = 20
rsshub_instances = ["https://custom.rsshub.local"]

[bilibili.notification]
enabled = true
gotify_url = "https://gotify.example.com"
gotify_token = "bili-token"
priority = 8

[[bilibili.subscriptions]]
uid = 1001
name = "UP主A"

[[bilibili.subscriptions]]
uid = 2002
name = "UP主B"

[xiaohongshu]
enabled = true

[xiaohongshu.auth]
cookie = "xhs_cookie_val"
expires_at = 1735689600.0

[xiaohongshu.monitor]
mode = "rss"
interval_minutes = 15

[xiaohongshu.notification]
enabled = true
gotify_url = "https://gotify.example.com"
gotify_token = "xhs-token"
priority = 6

[[xiaohongshu.subscriptions]]
user_id = "xhs_user1"
name = "博主A"

[weibo]
enabled = true

[weibo.auth]
cookie = "weibo_cookie_val"
expires_at = 1735689600.0

[weibo.monitor]
mode = "api"
interval_minutes = 8

[weibo.notification]
enabled = true
gotify_url = "https://gotify.example.com"
gotify_token = "weibo-token"
priority = 7

[[weibo.subscriptions]]
user_id = "weibo_user1"
name = "博主B"
"""


# ── 1. Missing file → returns Config() defaults ───────────────


class TestMissingFile:
    def test_missing_file_returns_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert isinstance(cfg, Config)
        assert cfg == Config()

    def test_missing_file_all_defaults(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        # Spot-check all major sections
        assert cfg.general.data_dir == "./data"
        assert cfg.auth.renewal.min_interval_hours == 24
        assert cfg.download.dir == "./downloads"
        assert cfg.transcribe.model == "base"
        assert cfg.analysis.enabled is True
        assert cfg.bilibili.auth.sessdata == ""
        assert cfg.bilibili.monitor.mode == "rss"
        assert cfg.bilibili.subscriptions == []
        assert cfg.bilibili.notification.gotify_url == ""
        assert cfg.xiaohongshu.enabled is False
        assert cfg.xiaohongshu.auth.cookie == ""
        assert cfg.weibo.enabled is False
        assert cfg.weibo.auth.cookie == ""


# ── 2. Empty TOML file → returns Config() defaults ────────────


class TestEmptyToml:
    def test_empty_file_returns_defaults(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text("", encoding="utf-8")
        cfg = load_config(p)
        assert cfg == Config()


# ── 3. Full TOML config ───────────────────────────────────────


class TestFullToml:
    def test_general(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.general.data_dir == "/data/trawler"

    def test_auth_renewal(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.auth.renewal.min_interval_hours == 12
        assert cfg.auth.renewal.force_before_days == 3
        assert cfg.auth.renewal.check_interval_hours == 4

    def test_download(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.download.dir == "/media/downloads"
        assert cfg.download.quality == "best"
        assert cfg.download.format == "bestvideo+bestaudio"
        assert cfg.download.max_concurrent == 5

    def test_transcribe(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.transcribe.model == "large"
        assert cfg.transcribe.language == "en"
        assert cfg.transcribe.output_dir == "/transcripts"
        assert cfg.transcribe.delete_after_transcribe is False

    def test_analysis(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.analysis.enabled is False
        assert cfg.analysis.provider == "openai"
        assert cfg.analysis.api_base == "https://api.openai.com/v1"
        assert cfg.analysis.api_key == "sk-test123"
        assert cfg.analysis.model_name == "gpt-4"

    def test_bilibili_auth(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.bilibili.auth.sessdata == "bili_sess"
        assert cfg.bilibili.auth.bili_jct == "bili_jct_val"
        assert cfg.bilibili.auth.buvid3 == "buvid3_val"
        assert cfg.bilibili.auth.dedeuserid == "12345"
        assert cfg.bilibili.auth.ac_time_value == "ac123"
        assert cfg.bilibili.auth.expires_at == 1735689600.0

    def test_bilibili_monitor(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.bilibili.monitor.mode == "api"
        assert cfg.bilibili.monitor.interval_minutes == 5
        assert cfg.bilibili.monitor.watch_dynamic is False
        assert cfg.bilibili.monitor.max_videos_per_check == 20
        assert cfg.bilibili.monitor.rsshub_instances == ["https://custom.rsshub.local"]

    def test_bilibili_notification(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.bilibili.notification.gotify_url == "https://gotify.example.com"
        assert cfg.bilibili.notification.gotify_token == "bili-token"
        assert cfg.bilibili.notification.priority == 8

    def test_bilibili_subscriptions(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert len(cfg.bilibili.subscriptions) == 2
        assert cfg.bilibili.subscriptions[0] == BiliSubscription(uid=1001, name="UP主A")
        assert cfg.bilibili.subscriptions[1] == BiliSubscription(uid=2002, name="UP主B")

    def test_xiaohongshu(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.xiaohongshu.enabled is True
        assert cfg.xiaohongshu.auth.cookie == "xhs_cookie_val"
        assert cfg.xiaohongshu.auth.expires_at == 1735689600.0
        assert cfg.xiaohongshu.monitor.mode == "rss"
        assert cfg.xiaohongshu.monitor.interval_minutes == 15
        assert cfg.xiaohongshu.notification.gotify_token == "xhs-token"
        assert cfg.xiaohongshu.notification.priority == 6
        assert len(cfg.xiaohongshu.subscriptions) == 1
        assert cfg.xiaohongshu.subscriptions[0] == UserSubscription(user_id="xhs_user1", name="博主A")

    def test_weibo(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        cfg = load_config(p)
        assert cfg.weibo.enabled is True
        assert cfg.weibo.auth.cookie == "weibo_cookie_val"
        assert cfg.weibo.auth.expires_at == 1735689600.0
        assert cfg.weibo.monitor.mode == "api"
        assert cfg.weibo.monitor.interval_minutes == 8
        assert cfg.weibo.notification.gotify_token == "weibo-token"
        assert cfg.weibo.notification.priority == 7
        assert len(cfg.weibo.subscriptions) == 1
        assert cfg.weibo.subscriptions[0] == UserSubscription(user_id="weibo_user1", name="博主B")


# ── 4. Minimal TOML ──────────────────────────────────────────


class TestMinimalToml:
    def test_only_sessdata_set(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text(MINIMAL_TOML, encoding="utf-8")
        cfg = load_config(p)
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
        assert cfg.bilibili.notification.gotify_url == ""
        assert cfg.xiaohongshu.enabled is False
        assert cfg.weibo.enabled is False


# ── 5. Env var overrides ──────────────────────────────────────


class TestEnvOverrides:
    def test_trawler_gotify_url(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_GOTIFY_URL", "https://override.example.com")
        cfg = load_config(p)
        assert cfg.bilibili.notification.gotify_url == "https://override.example.com"

    def test_trawler_gotify_token_bili(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_GOTIFY_TOKEN_BILI", "override-bili-token")
        cfg = load_config(p)
        assert cfg.bilibili.notification.gotify_token == "override-bili-token"

    def test_trawler_gotify_token_xhs(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_GOTIFY_TOKEN_XHS", "override-xhs-token")
        cfg = load_config(p)
        assert cfg.xiaohongshu.notification.gotify_token == "override-xhs-token"

    def test_trawler_gotify_token_weibo(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_GOTIFY_TOKEN_WEIBO", "override-weibo-token")
        cfg = load_config(p)
        assert cfg.weibo.notification.gotify_token == "override-weibo-token"

    def test_trawler_xhs_cookie(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_XHS_COOKIE", "override-xhs-cookie")
        cfg = load_config(p)
        assert cfg.xiaohongshu.auth.cookie == "override-xhs-cookie"

    def test_trawler_weibo_cookie(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_WEIBO_COOKIE", "override-weibo-cookie")
        cfg = load_config(p)
        assert cfg.weibo.auth.cookie == "override-weibo-cookie"

    def test_trawler_llm_api_key(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_LLM_API_KEY", "override-api-key")
        cfg = load_config(p)
        assert cfg.analysis.api_key == "override-api-key"

    def test_trawler_llm_api_base(self, tmp_path, monkeypatch):
        p = tmp_path / "config.toml"
        p.write_text(FULL_TOML, encoding="utf-8")
        monkeypatch.setenv("FEEDFLOW_LLM_API_BASE", "https://override.api.com")
        cfg = load_config(p)
        assert cfg.analysis.api_base == "https://override.api.com"

    def test_env_override_with_empty_config(self, tmp_path, monkeypatch):
        """Env vars should work even when no config file exists."""
        monkeypatch.setenv("FEEDFLOW_GOTIFY_URL", "https://from-env.com")
        monkeypatch.setenv("FEEDFLOW_XHS_COOKIE", "env-cookie")
        monkeypatch.setenv("FEEDFLOW_LLM_API_KEY", "env-key")
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert cfg.bilibili.notification.gotify_url == "https://from-env.com"
        assert cfg.xiaohongshu.auth.cookie == "env-cookie"
        assert cfg.analysis.api_key == "env-key"


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
        assert a.ac_time_value == ""
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
        assert m.interval_minutes == 3
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
        assert m.interval_minutes == 10

    def test_weibo_monitor_config_defaults(self):
        m = WeiboMonitorConfig()
        assert m.mode == "api"
        assert m.interval_minutes == 10

    def test_analysis_config_defaults(self):
        a = AnalysisConfig()
        assert a.enabled is True
        assert a.provider == "codebuddy"
        assert a.api_base == ""
        assert a.api_key == ""
        assert a.model_name == ""

    def test_notification_config_defaults(self):
        n = NotificationConfig()
        assert n.enabled is True
        assert n.gotify_url == ""
        assert n.gotify_token == ""
        assert n.priority == 5

    def test_bili_subscription_defaults(self):
        s = BiliSubscription()
        assert s.uid == 0
        assert s.name == ""

    def test_user_subscription_defaults(self):
        s = UserSubscription()
        assert s.user_id == ""
        assert s.name == ""

    def test_bilibili_config_defaults(self):
        b = BilibiliConfig()
        assert isinstance(b.auth, BilibiliAuth)
        assert isinstance(b.monitor, BilibiliMonitorConfig)
        assert isinstance(b.notification, NotificationConfig)
        assert b.subscriptions == []

    def test_xhs_config_defaults(self):
        x = XhsConfig()
        assert x.enabled is False
        assert isinstance(x.auth, XhsAuth)
        assert isinstance(x.monitor, XhsMonitorConfig)
        assert isinstance(x.notification, NotificationConfig)
        assert x.subscriptions == []

    def test_weibo_config_defaults(self):
        w = WeiboConfig()
        assert w.enabled is False
        assert isinstance(w.auth, WeiboAuth)
        assert isinstance(w.monitor, WeiboMonitorConfig)
        assert isinstance(w.notification, NotificationConfig)
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
    def test_load_config_default_path_is_toml(self, tmp_path, monkeypatch):
        """Verify that load_config() defaults to 'config.toml'."""
        monkeypatch.chdir(tmp_path)
        p = tmp_path / "config.toml"
        p.write_text(MINIMAL_TOML, encoding="utf-8")
        cfg = load_config()
        assert cfg.bilibili.auth.sessdata == "abc123"
