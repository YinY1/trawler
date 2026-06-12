"""Tests for shared.auth.token_store — format-preserving TOML auth updates."""

from __future__ import annotations

import textwrap
import tomllib

import pytest

from shared.auth.token_store import update_auth_section

SAMPLE_TOML = textwrap.dedent("""\
    # Trawler configuration

    [general]
    data_dir = "./data"

    [bilibili.auth]
    sessdata = "old_sess"
    bili_jct = "old_jct"
    buvid3 = "old_buvid"
    # comment above dedeuserid
    dedeuserid = "12345"

    [bilibili.monitor]
    mode = "rss"
    interval_minutes = 3

    [bilibili.notification]
    enabled = true
    gotify_url = "https://gotify.example.com"

    [xiaohongshu.auth]
    cookie = "old_xhs_cookie"

    [weibo.auth]
    cookie = "old_weibo_cookie"
""")


class TestUpdateAuthSection:
    """Tests for update_auth_section."""

    def test_update_bilibili_auth_fields(self, tmp_path):
        """Only bilibili.auth fields change, other sections untouched."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(SAMPLE_TOML, encoding="utf-8")

        update_auth_section(
            cfg,
            "bilibili",
            {
                "sessdata": "new_sess",
                "bili_jct": "new_jct",
            },
        )

        data = tomllib.load(cfg.open("rb"))
        assert data["bilibili"]["auth"]["sessdata"] == "new_sess"
        assert data["bilibili"]["auth"]["bili_jct"] == "new_jct"
        assert data["bilibili"]["auth"]["buvid3"] == "old_buvid"
        assert data["bilibili"]["auth"]["dedeuserid"] == "12345"
        # Other platforms untouched
        assert data["xiaohongshu"]["auth"]["cookie"] == "old_xhs_cookie"
        assert data["weibo"]["auth"]["cookie"] == "old_weibo_cookie"
        # Monitor / notification untouched
        assert data["bilibili"]["monitor"]["mode"] == "rss"
        assert data["bilibili"]["notification"]["gotify_url"] == "https://gotify.example.com"

    def test_comments_preserved(self, tmp_path):
        """TOML comments are still present after update."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(SAMPLE_TOML, encoding="utf-8")

        update_auth_section(cfg, "bilibili", {"sessdata": "new_sess"})

        content = cfg.read_text(encoding="utf-8")
        assert "# Trawler configuration" in content
        assert "# comment above dedeuserid" in content

    def test_new_field_added(self, tmp_path):
        """If auth_dict has a key not in original file, it's added."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(SAMPLE_TOML, encoding="utf-8")

        update_auth_section(
            cfg,
            "bilibili",
            {
                "ac_time_value": "new_ac_time",
            },
        )

        data = tomllib.load(cfg.open("rb"))
        assert data["bilibili"]["auth"]["ac_time_value"] == "new_ac_time"
        # Existing fields still present
        assert data["bilibili"]["auth"]["sessdata"] == "old_sess"

    def test_update_xiaohongshu_auth(self, tmp_path):
        """Only xiaohongshu.auth changes, bilibili and weibo untouched."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(SAMPLE_TOML, encoding="utf-8")

        update_auth_section(cfg, "xiaohongshu", {"cookie": "new_xhs_cookie"})

        data = tomllib.load(cfg.open("rb"))
        assert data["xiaohongshu"]["auth"]["cookie"] == "new_xhs_cookie"
        assert data["bilibili"]["auth"]["sessdata"] == "old_sess"
        assert data["weibo"]["auth"]["cookie"] == "old_weibo_cookie"

    def test_update_weibo_auth(self, tmp_path):
        """Only weibo.auth changes, bilibili and xiaohongshu untouched."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(SAMPLE_TOML, encoding="utf-8")

        update_auth_section(cfg, "weibo", {"cookie": "new_weibo_cookie"})

        data = tomllib.load(cfg.open("rb"))
        assert data["weibo"]["auth"]["cookie"] == "new_weibo_cookie"
        assert data["bilibili"]["auth"]["sessdata"] == "old_sess"
        assert data["xiaohongshu"]["auth"]["cookie"] == "old_xhs_cookie"

    def test_missing_platform_section_creates_it(self, tmp_path):
        """If platform section doesn't exist, creates [platform] and [platform.auth]."""
        cfg = tmp_path / "config.toml"
        minimal = textwrap.dedent("""\
            [general]
            data_dir = "./data"
        """)
        cfg.write_text(minimal, encoding="utf-8")

        update_auth_section(cfg, "douyin", {"cookie": "dy_cookie"})

        data = tomllib.load(cfg.open("rb"))
        assert data["douyin"]["auth"]["cookie"] == "dy_cookie"
        assert data["general"]["data_dir"] == "./data"

    def test_missing_file_raises(self, tmp_path):
        """FileNotFoundError raised if config file doesn't exist."""
        cfg = tmp_path / "nonexistent.toml"
        with pytest.raises(FileNotFoundError):
            update_auth_section(cfg, "bilibili", {"sessdata": "x"})

    def test_empty_auth_dict_file_unchanged(self, tmp_path):
        """Empty auth_dict leaves file content identical."""
        cfg = tmp_path / "config.toml"
        cfg.write_text(SAMPLE_TOML, encoding="utf-8")
        original = cfg.read_text(encoding="utf-8")

        update_auth_section(cfg, "bilibili", {})

        assert cfg.read_text(encoding="utf-8") == original
