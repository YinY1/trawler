"""Web 站点访问鉴权 — 密码 hash + auth.toml I/O + setup 检测 + CSRF。

存储位置：``data/auth.toml``（与主 ``config/config.toml`` 隔离）。
username 固定为 ``admin``（常量 :data:`WEB_ADMIN_USERNAME`）。

设计要点：
- 密码用 ``passlib[bcrypt]`` 的 :class:`CryptContext`，bcrypt scheme
- ``data/auth.toml`` 不存在或无 ``admin_password_hash`` → setup 未完成
- :func:`set_password` 更新密码同时**轮转** ``session_secret``，
  让 starlette ``SessionMiddleware`` 在 ``secret_key`` 变化后无法验签旧 cookie，
  从而使所有旧 session 失效（cookie 是签名的，纯客户端状态）
- :func:`verify_csrf` 简单 CSRF 校验：HTMX 头 OR 同源 referer
"""

from __future__ import annotations

import logging
import secrets
import tomllib
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import tomlkit
from fastapi import Request
from passlib.context import CryptContext

from shared.config import WebAuthConfig

logger = logging.getLogger(__name__)

# ── 模块常量 ────────────────────────────────────────────────────

WEB_ADMIN_USERNAME = "admin"
AUTH_TOML_PATH = Path("data/auth.toml")
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── 密码 hash ───────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """返回 bcrypt hash（``$2...`` 前缀，自带随机 salt）。"""
    # passlib 无类型 stub；通过 Any 规避 pyright reportUnknownMemberType
    return cast(Any, _pwd_context).hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与 hash。空 hash / verify 失败均返回 False，不抛异常。"""
    if not hashed:
        return False
    try:
        return bool(cast(Any, _pwd_context).verify(plain, hashed))
    except Exception:
        return False


# ── auth.toml I/O ───────────────────────────────────────────────


def load_auth_config() -> WebAuthConfig:
    """从 :data:`AUTH_TOML_PATH` 加载。

    文件不存在或字段缺失时返回默认 :class:`WebAuthConfig`。
    """
    if not AUTH_TOML_PATH.exists():
        return WebAuthConfig()
    with open(AUTH_TOML_PATH, "rb") as f:
        raw = tomllib.load(f)
    return WebAuthConfig(
        admin_password_hash=raw.get("admin_password_hash", ""),
        session_secret=raw.get("session_secret", ""),
        session_max_age_seconds=raw.get("session_max_age_seconds", 604800),
    )


def save_auth_config(cfg: WebAuthConfig) -> None:
    """原子写入 :data:`AUTH_TOML_PATH`（先写 ``.tmp`` 再 rename）。"""
    AUTH_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["admin_password_hash"] = cfg.admin_password_hash
    doc["session_secret"] = cfg.session_secret
    doc["session_max_age_seconds"] = cfg.session_max_age_seconds
    tmp = AUTH_TOML_PATH.with_suffix(".toml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        tomlkit.dump(doc, f)
    tmp.replace(AUTH_TOML_PATH)


# ── setup 检测 + 改密码 ─────────────────────────────────────────


def is_setup_complete() -> bool:
    """``data/auth.toml`` 存在且 ``admin_password_hash`` 非空。"""
    cfg = load_auth_config()
    return bool(cfg.admin_password_hash)


def set_password(plain: str) -> None:
    """更新密码 + **轮转** ``session_secret``（让所有旧 session 失效）。"""
    cfg = load_auth_config()
    cfg.admin_password_hash = hash_password(plain)
    cfg.session_secret = secrets.token_urlsafe(64)
    save_auth_config(cfg)


# ── CSRF ────────────────────────────────────────────────────────


def verify_csrf(request: Request) -> bool:
    """简单 CSRF 防护：HTMX 请求 OR 同源 referer。

    - 已登录用户的写操作（POST/PUT/PATCH/DELETE）必须通过此校验。
    - ``/login`` ``/setup`` 本身由 :data:`_CSRF_EXEMPT_PATHS` 豁免
      （用户还没登录，没 session 可盗）。
    - HTMX 默认带 ``X-Requested-With: XMLHttpRequest`` 头。
    - 非 HTMX 表单依赖浏览器自动带的 ``Referer``（同源校验）。

    返回 True 表示通过；False 表示拦截（中间件返回 403）。
    """
    # 1. HTMX header
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return True
    # 2. 同源 referer
    referer = request.headers.get("referer", "")
    host = request.headers.get("host", "")
    if referer and host:
        try:
            parsed = urlparse(referer)
            if parsed.netloc == host:
                return True
        except Exception:
            pass
    return False
