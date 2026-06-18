"""Credential persistence — updates the [platform.auth] section in cookies.toml."""

from __future__ import annotations

# pyright: basic
import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import tomlkit

logger = logging.getLogger(__name__)

COOKIES_FILENAME = "cookies.toml"

# 模块级锁：保护 cookies.toml 的 read-modify-write。
# 两条独立路径会并发写入同一文件：
#   - web /auth/refresh 和 /auth/poll → update_auth_section
#   - scheduler check_and_renew_tokens → update_auth_section (经 __init__.py 包装)
#   - web /auth/logout → clear_auth_section
# 不加锁会出现 last-writer-wins 覆盖丢失。
# asyncio.Lock 在单 worker uvicorn + asyncio.run() 的单事件循环下足够；
# 不跨进程、不跨线程（emit 路径都在 loop 线程）。
_file_lock = asyncio.Lock()


async def update_auth_section(config_path: str | Path, platform: str, auth_dict: dict[str, Any]) -> None:
    """Update only the [platform.auth] section in cookies.toml, preserving all other content.

    The target file is ``cookies.toml`` in the same directory as ``config_path``.
    If the file does not exist, it will be created.

    Args:
        config_path: Base config path (used to derive directory for cookies.toml)
        platform: Platform name ("bilibili" | "xiaohongshu" | "weibo")
        auth_dict: Key-value pairs to update in [platform.auth]
    """
    # 整段 read-modify-write 串行化：防止 web 与 scheduler 并发写覆盖。
    async with _file_lock:
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


async def clear_auth_section(config_path: str | Path, platform: str) -> bool:
    """Remove the [platform.auth] section from cookies.toml if it exists.

    Returns True if the section was present (and removed), False otherwise.
    All other content (subscriptions, other platforms) is preserved.
    """
    # 同 update_auth_section：read-modify-write 串行化。
    async with _file_lock:
        p = Path(config_path)
        cookies_path = p.with_name(COOKIES_FILENAME)
        if not cookies_path.exists():
            return False

        doc = tomlkit.parse(cookies_path.read_text(encoding="utf-8"))
        if platform not in doc:
            return False
        platform_table = doc[platform]
        if "auth" not in platform_table:
            return False

        # Safety backup: snapshot the current cookies.toml before destructive delete.
        # Prevents accidental credential loss (e.g. tests/curl hitting the real file).
        try:
            backup_path = cookies_path.with_suffix(".toml.bak")
            original = cookies_path.read_text(encoding="utf-8")
            header = (
                f"# Backup before clear_auth_section at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# Platform: {platform}\n\n"
            )
            backup_path.write_text(header + original, encoding="utf-8")
            logger.warning("⚠️ 已备份 %s 的 [%s.auth] 段到 %s", platform, platform, backup_path.name)
        except Exception as exc:  # noqa: BLE001
            # 备份失败必须中止删除：宁可保留过期 token 也不能丢凭证。
            logger.error("⚠️ 备份失败，已中止删除 [%s.auth]: %s", platform, exc)
            return False

        del platform_table["auth"]
        cookies_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return True
