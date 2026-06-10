"""Credential persistence — updates only the [platform.auth] section of a TOML config."""

from __future__ import annotations

from pathlib import Path

import tomlkit


def update_auth_section(config_path: str | Path, platform: str, auth_dict: dict) -> None:
    """Update only the [platform.auth] section in config.toml, preserving all other content.

    Args:
        config_path: Path to config.toml
        platform: Platform name ("bilibili" | "xiaohongshu" | "weibo")
        auth_dict: Key-value pairs to update in [platform.auth]

    Raises:
        FileNotFoundError: If config file doesn't exist
    """
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p}")

    doc = tomlkit.parse(p.read_text(encoding="utf-8"))

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

    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
