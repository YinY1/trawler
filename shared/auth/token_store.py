"""Credential persistence — updates the [platform.auth] section in cookies.toml."""

from __future__ import annotations

# pyright: basic
from pathlib import Path

import tomlkit

COOKIES_FILENAME = "cookies.toml"


def update_auth_section(config_path: str | Path, platform: str, auth_dict: dict) -> None:
    """Update only the [platform.auth] section in cookies.toml, preserving all other content.

    The target file is ``cookies.toml`` in the same directory as ``config_path``.
    If the file does not exist, it will be created.

    Args:
        config_path: Base config path (used to derive directory for cookies.toml)
        platform: Platform name ("bilibili" | "xiaohongshu" | "weibo")
        auth_dict: Key-value pairs to update in [platform.auth]
    """
    p = Path(config_path)
    cookies_path = p.with_name(COOKIES_FILENAME)

    if cookies_path.exists():
        doc = tomlkit.parse(cookies_path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    # Ensure platform table exists
    if platform not in doc:
        doc.add(platform, tomlkit.table(is_super_table=True))
    platform_table = doc[platform]

    # Ensure auth sub-table exists
    if "auth" not in platform_table:
        platform_table.add("auth", tomlkit.table())
    auth_table = platform_table["auth"]

    # Update auth fields
    for key, value in auth_dict.items():
        auth_table[key] = value

    cookies_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
