"""Subscription CRUD — read/write config/subscriptions.toml via tomlkit."""

from __future__ import annotations

# pyright: basic
import logging
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import AoT
from tomlkit.toml_document import TOMLDocument

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# Platform mapping
# ═══════════════════════════════════════════════════════════

# CLI short name → TOML section name
PLATFORM_TO_SECTION: dict[str, str] = {
    "bili": "bilibili",
    "xhs": "xiaohongshu",
    "weibo": "weibo",
}

# CLI short name → subscription key field + type
SUBSCRIPTION_KEY: dict[str, tuple[str, type]] = {
    "bili": ("uid", int),
    "xhs": ("user_id", str),
    "weibo": ("user_id", str),
}

VALID_PLATFORMS = set(PLATFORM_TO_SECTION)


def _load_doc(path: str) -> TOMLDocument | None:
    """Load TOML document, return None if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8")
    return tomlkit.parse(raw) if raw.strip() else None


def _ensure_platform_array(doc: TOMLDocument, section: str) -> list[Any]:
    """Ensure doc[section]['subscriptions'] exists and is a list (prefer AoT)."""
    if section not in doc:
        doc[section] = tomlkit.table()
    tbl: Any = doc[section]
    if "subscriptions" not in tbl:
        tbl["subscriptions"] = tomlkit.aot()
        return tbl["subscriptions"]

    arr: Any = tbl["subscriptions"]

    # Convert regular Array → AoT if needed (e.g. inline format from hand-edited file)
    if isinstance(arr, AoT):
        return arr
    if isinstance(arr, list):
        aot = tomlkit.aot()
        for item in arr:
            # Wrap InlineTable items into Table
            t = tomlkit.table()
            for k, v in dict(item).items():
                t[k] = v
            aot.append(t)
        tbl["subscriptions"] = aot
        return aot

    # Fallback: create new AoT
    tbl["subscriptions"] = tomlkit.aot()
    return tbl["subscriptions"]


def _key_value(platform: str, identifier: int | str) -> tuple[str, int | str]:
    """Get the key field name and cast identifier to correct type."""
    key, typ = SUBSCRIPTION_KEY[platform]
    if typ is int and isinstance(identifier, str):
        return key, int(identifier)
    if isinstance(identifier, typ):  # type: ignore[arg-type]
        return key, identifier
    return key, typ(identifier)  # type: ignore[call-arg]


def _match_sub(item: dict[str, Any], key: str, value: int | str) -> bool:
    """Check if a subscription item matches the given key/value."""
    return str(item.get(key, "")) == str(value)


# ── Public API ─────────────────────────────────────────────────────


async def list_subscriptions(
    platform: str | None = None, path: str = "config/subscriptions.toml"
) -> dict[str, list[dict[str, Any]]]:
    """List subscriptions, optionally filtered by platform.

    Returns a dict mapping TOML section name → list of subscription dicts.
    Example: ``{"bilibili": [{"uid": 123, "name": "UP1"}]}``
    """
    doc = _load_doc(path)
    if doc is None:
        return {}

    result: dict[str, list[dict[str, Any]]] = {}
    sections = [PLATFORM_TO_SECTION[platform]] if platform else list(PLATFORM_TO_SECTION.values())

    for section in sections:
        entry = doc.get(section)
        if entry is None:
            continue
        subs_list = entry.get("subscriptions") if isinstance(entry, dict) else None
        if subs_list and isinstance(subs_list, list):
            result[section] = [dict(item) for item in subs_list]

    return result


async def add_subscription(
    platform: str,
    identifier: int | str,
    name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """Add a subscription. Returns (success, message)."""
    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    # Load or create document
    doc: TOMLDocument
    if p.exists():
        raw = p.read_text(encoding="utf-8")
        doc = tomlkit.parse(raw) if raw.strip() else tomlkit.document()
    else:
        doc = tomlkit.document()

    arr = _ensure_platform_array(doc, section)

    # Check duplicate
    for item in arr:
        if isinstance(item, dict) and str(item.get(key, "")) == str(typed_id):
            return False, f"已存在: {item.get('name', '')}"

    # Append new subscription
    new_entry = tomlkit.table()
    new_entry[key] = typed_id
    new_entry["name"] = name
    arr.append(new_entry)

    # Write back
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("Added subscription: %s/%s = %s (%s)", section, key, typed_id, name)
    return True, f"已添加: {name}"


async def remove_subscription(
    platform: str,
    identifier: int | str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """Remove a subscription. Returns (success, message)."""
    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    if not p.exists():
        return False, "未找到: 订阅文件不存在"

    raw = p.read_text(encoding="utf-8")
    if not raw.strip():
        return False, "未找到: 订阅列表为空"

    doc = tomlkit.parse(raw)
    entry = doc.get(section)
    if entry is None:
        return False, "未找到: 该平台无订阅"

    subs_list = entry.get("subscriptions") if isinstance(entry, dict) else None
    if not subs_list or not isinstance(subs_list, list):
        return False, "未找到: 该平台无订阅"

    removed_name = ""
    new_list = tomlkit.aot()
    found = False
    for item in subs_list:
        if isinstance(item, dict) and str(item.get(key, "")) == str(typed_id):
            removed_name = item.get("name", "")
            found = True
        else:
            # Convert InlineTable to Table for AoT compatibility
            if isinstance(item, dict) and not isinstance(item, AoT):
                t = tomlkit.table()
                for k, v in dict(item).items():
                    t[k] = v
                new_list.append(t)
            else:
                new_list.append(item)

    if not found:
        return False, f"未找到: {platform} 平台未找到匹配的订阅"

    # Replace subscriptions; remove key if empty to avoid empty-AoT serialization bug
    if len(new_list) == 0:
        del doc[section]["subscriptions"]
    else:
        doc[section]["subscriptions"] = new_list
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("Removed subscription: %s/%s = %s (%s)", section, key, typed_id, removed_name)
    return True, f"已删除: {removed_name}"


# ═══════════════════════════════════════════════════════════
# Search by name
# ═══════════════════════════════════════════════════════════

# Platforms that support name search and their key field
SEARCH_CAPABLE: dict[str, str] = {
    "bili": "uid",
    "weibo": "user_id",
    "xhs": "user_id",
}

# Config key in each platform's auth section
_AUTH_COOKIE_KEY: dict[str, str] = {
    "bili": "sessdata",
    "weibo": "cookie",
    "xhs": "cookie",
}


async def search_by_name(
    platform: str,
    name: str,
    config_path: str = "config/config.toml",
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Search for a user by name on the given platform.

    Returns (success, message, candidates).
    Each candidate is a dict with key (uid/user_id) and name.
    """
    if platform not in SEARCH_CAPABLE:
        return False, f"{platform} 暂不支持按名字搜索", []

    if platform == "bili":
        return await _search_bili(name, config_path)
    elif platform == "weibo":
        return await _search_weibo(name, config_path)
    elif platform == "xhs":
        return await _search_xhs(name, config_path)

    return False, f"{platform} 暂不支持按名字搜索", []


async def _search_bili(name: str, config_path: str) -> tuple[bool, str, list[dict[str, Any]]]:
    """Search B站 user by name using name2uid API."""
    from bilibili_api import Credential
    from bilibili_api.user import name2uid

    from shared.config import load_config

    cfg = await load_config(config_path)
    auth = cfg.bilibili.auth

    if not auth.sessdata:
        return False, "B站搜索需要先登录 (trawler login --platform bili)", []

    cred = Credential(sessdata=auth.sessdata, bili_jct=auth.bili_jct, buvid3=auth.buvid3, dedeuserid=auth.dedeuserid)

    try:
        result = await name2uid(name, credential=cred)
    except Exception as exc:
        logger.warning("B站搜索失败: %s", exc)
        return False, f"B站搜索失败: {exc}", []

    uid_list = result.get("uid_list", []) if isinstance(result, dict) else []
    candidates = []
    for entry in uid_list:
        uid = entry.get("uid")
        uname = entry.get("name", name)
        if uid:
            candidates.append({"uid": uid, "name": uname})

    if not candidates:
        return False, f"未找到名为「{name}」的用户", []

    return True, f"找到 {len(candidates)} 个匹配", candidates


async def _search_weibo(name: str, config_path: str) -> tuple[bool, str, list[dict[str, Any]]]:
    """Search Weibo user by name using mobile suggestion API."""
    from shared.config import load_config

    cfg = await load_config(config_path)
    cookie = cfg.weibo.auth.cookie

    if not cookie:
        return False, "微博搜索需要先登录 (trawler login --platform weibo)", []

    from platforms.weibo.api import search_user_by_name

    try:
        users = await search_user_by_name(cookie, name)
    except Exception as exc:
        logger.warning("微博搜索失败: %s", exc)
        return False, f"微博搜索失败: {exc}", []

    candidates = []
    for u in users:
        uid = u.get("id")
        uname = u.get("screen_name", name)
        if uid:
            candidates.append({"user_id": str(uid), "name": uname})

    if not candidates:
        return False, f"未找到名为「{name}」的用户", []

    return True, f"找到 {len(candidates)} 个匹配", candidates


async def _search_xhs(name: str, config_path: str) -> tuple[bool, str, list[dict[str, Any]]]:
    """Search Xiaohongshu user by name."""
    from shared.config import load_config

    cfg = await load_config(config_path)
    cookie = cfg.xiaohongshu.auth.cookie

    if not cookie:
        return False, "小红书搜索需要先登录 (trawler login --platform xhs)", []

    from platforms.xiaohongshu.search import search_xhs_user_by_name

    try:
        users = await search_xhs_user_by_name(cookie, name)
    except Exception as exc:
        logger.warning("小红书搜索失败: %s", exc)
        return False, f"小红书搜索失败: {exc}", []

    candidates = []
    for u in users:
        uid = u.get("user_id")
        uname = u.get("nickname", name)
        if uid:
            candidates.append({"user_id": uid, "name": uname})

    if not candidates:
        return False, f"未找到名为「{name}」的用户", []

    return True, f"找到 {len(candidates)} 个匹配", candidates
