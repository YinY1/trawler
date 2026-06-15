"""Tests for shared.auth.token_store — format-preserving TOML auth updates in cookies.toml."""

from __future__ import annotations

import textwrap
import tomllib

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


def _cookies_path(tmp_path):
    """Derive cookies.toml path from a tmp_path / 'config.toml'."""
    return tmp_path / "cookies.toml"


def _given_cookies(tmp_path, content: str) -> None:
    """Write initial content to cookies.toml."""
    _cookies_path(tmp_path).write_text(content, encoding="utf-8")


class TestUpdateAuthSection:
    """Tests for update_auth_section — target is cookies.toml."""

    async def test_update_bilibili_auth_fields(self, tmp_path):
        """Only bilibili.auth fields change, other sections untouched."""
        config_path = tmp_path / "config.toml"
        _given_cookies(tmp_path, SAMPLE_TOML)

        await update_auth_section(
            config_path,
            "bilibili",
            {
                "sessdata": "new_sess",
                "bili_jct": "new_jct",
            },
        )

        data = tomllib.loads(_cookies_path(tmp_path).read_text(encoding="utf-8"))
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

    async def test_comments_preserved(self, tmp_path):
        """TOML comments are still present after update."""
        config_path = tmp_path / "config.toml"
        _given_cookies(tmp_path, SAMPLE_TOML)

        await update_auth_section(config_path, "bilibili", {"sessdata": "new_sess"})

        content = _cookies_path(tmp_path).read_text(encoding="utf-8")
        assert "# Trawler configuration" in content
        assert "# comment above dedeuserid" in content

    async def test_new_field_added(self, tmp_path):
        """If auth_dict has a key not in original file, it's added."""
        config_path = tmp_path / "config.toml"
        _given_cookies(tmp_path, SAMPLE_TOML)

        await update_auth_section(
            config_path,
            "bilibili",
            {
                "refresh_token": "new_rt",
            },
        )

        data = tomllib.loads(_cookies_path(tmp_path).read_text(encoding="utf-8"))
        assert data["bilibili"]["auth"]["refresh_token"] == "new_rt"
        # Existing fields still present
        assert data["bilibili"]["auth"]["sessdata"] == "old_sess"

    async def test_update_xiaohongshu_auth(self, tmp_path):
        """Only xiaohongshu.auth changes, bilibili and weibo untouched."""
        config_path = tmp_path / "config.toml"
        _given_cookies(tmp_path, SAMPLE_TOML)

        await update_auth_section(config_path, "xiaohongshu", {"cookie": "new_xhs_cookie"})

        data = tomllib.loads(_cookies_path(tmp_path).read_text(encoding="utf-8"))
        assert data["xiaohongshu"]["auth"]["cookie"] == "new_xhs_cookie"
        assert data["bilibili"]["auth"]["sessdata"] == "old_sess"
        assert data["weibo"]["auth"]["cookie"] == "old_weibo_cookie"

    async def test_update_weibo_auth(self, tmp_path):
        """Only weibo.auth changes, bilibili and xiaohongshu untouched."""
        config_path = tmp_path / "config.toml"
        _given_cookies(tmp_path, SAMPLE_TOML)

        await update_auth_section(config_path, "weibo", {"cookie": "new_weibo_cookie"})

        data = tomllib.loads(_cookies_path(tmp_path).read_text(encoding="utf-8"))
        assert data["weibo"]["auth"]["cookie"] == "new_weibo_cookie"
        assert data["bilibili"]["auth"]["sessdata"] == "old_sess"
        assert data["xiaohongshu"]["auth"]["cookie"] == "old_xhs_cookie"

    async def test_missing_platform_section_creates_it(self, tmp_path):
        """If platform section doesn't exist, creates [platform] and [platform.auth]."""
        config_path = tmp_path / "config.toml"
        minimal = textwrap.dedent("""\
            [general]
            data_dir = "./data"
        """)
        _given_cookies(tmp_path, minimal)

        await update_auth_section(config_path, "douyin", {"cookie": "dy_cookie"})

        data = tomllib.loads(_cookies_path(tmp_path).read_text(encoding="utf-8"))
        assert data["douyin"]["auth"]["cookie"] == "dy_cookie"
        assert data["general"]["data_dir"] == "./data"

    async def test_creates_file_if_missing(self, tmp_path):
        """File is created if cookies.toml doesn't exist yet."""
        config_path = tmp_path / "config.toml"

        await update_auth_section(config_path, "bilibili", {"sessdata": "new_sess"})

        data = tomllib.loads(_cookies_path(tmp_path).read_text(encoding="utf-8"))
        assert data["bilibili"]["auth"]["sessdata"] == "new_sess"

    async def test_empty_auth_dict_file_unchanged(self, tmp_path):
        """Empty auth_dict leaves file content identical."""
        config_path = tmp_path / "config.toml"
        _given_cookies(tmp_path, SAMPLE_TOML)
        original = _cookies_path(tmp_path).read_text(encoding="utf-8")

        await update_auth_section(config_path, "bilibili", {})

        assert _cookies_path(tmp_path).read_text(encoding="utf-8") == original
