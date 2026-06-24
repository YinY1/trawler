"""Web 站点访问鉴权 — 密码 hash + auth.toml I/O + setup 检测 + CSRF。

存储位置：``data/auth.toml``（与主 ``config/config.toml`` 隔离）。
username 固定为 ``admin``（常量 :data:`WEB_ADMIN_USERNAME`）。

设计要点：
- 密码用 :class:`argon2.PasswordHasher` 直调（argon2id），不经过 passlib。
  历史原因：``passlib 1.7.x`` 与 ``bcrypt>=4.1`` 不兼容（``bcrypt.__about__``
  被移除），passlib CryptContext 探测 backend 时崩溃、静默回退到默认 bcrypt，
  偏离设计意图。argon2-cffi 是密码哈希竞赛冠军算法的官方实现，维护活跃。
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
from urllib.parse import urlparse

import tomlkit
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHash, VerificationError, VerifyMismatchError
from fastapi import Request

from shared.config import WebAuthConfig

logger = logging.getLogger(__name__)

# ── 模块常量 ────────────────────────────────────────────────────

WEB_ADMIN_USERNAME = "admin"
AUTH_TOML_PATH = Path("data/auth.toml")
# argon2-cffi 默认参数已是 PHC 推荐值（memory_cost=19456, time_cost=2, parallelism=1）。
# 这里显式构造一次，未来调参集中在此。
_hasher = PasswordHasher()

# ── 密码 hash ───────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    """返回 argon2id hash（``$argon2id$`` 前缀，自带随机 salt）。"""
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文与 hash。

    - 空 hash / 格式错误 / hash 不匹配 / 任何 argon2 内部异常 → 返回 False
    - 不向上抛异常（路由层依赖此契约：失败即 False，不 500）

    彻底切换后不再兼容旧 bcrypt 哈希（``$2b$``）。已部署实例需通过 ``/setup``
    或 ``/settings/account`` 重设密码升级到 argon2id。
    """
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except InvalidHash, VerificationError, Argon2Error:
        # 畸形 hash、被篡改的 hash、或非 argon2 串（含旧 bcrypt）→ 验证失败
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
