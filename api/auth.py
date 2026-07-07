"""API token 鉴权 — ``Authorization: Bearer <token>``。

与 Web UI 的 session/CSRF 鉴权完全隔离（中间件层对 ``/api/*`` 豁免，
本模块通过 FastAPI 依赖 ``require_token`` 在路由层兜底鉴权）。

存储：``data/auth.toml`` 的 ``[[api_tokens]]`` AoT，复用 ``web/auth.py`` 的
``load_auth_config`` / ``save_auth_config`` 读写（同文件，与 admin 密码共管）。
存 SHA-256 hash 不存明文：token 是高熵随机串，无需 argon2（argon2 ~50ms/次，
bot 高频调用不划算）。常量时间比对（``hmac.compare_digest``）防 timing attack。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request

from shared.config import ApiTokenEntry
from web.auth import AUTH_TOML_PATH, load_auth_config, save_auth_config

logger = logging.getLogger(__name__)


def _hash_token(plain: str) -> str:
    """SHA-256 hexdigest。Token 是高熵随机串，无需 argon2。"""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _verify_token(plain: str, expected_hash: str) -> bool:
    """常量时间比对（``hmac.compare_digest``），防 timing attack。"""
    actual = _hash_token(plain)
    return hmac.compare_digest(actual, expected_hash)


async def require_token(request: Request) -> str:
    """FastAPI 依赖：校验 ``Authorization: Bearer <token>``。

    - 无 header / 格式错误（非 ``Bearer`` 前缀）/ token 不匹配 → 401
    - 通过 → 返回 token name（供日志/审计，路由不依赖返回值即可）

    与 ``Authorization: Bearer`` 不匹配的 scheme（如 ``Token xxx``）按格式错处理。
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    plain = auth[len("Bearer ") :]
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if _verify_token(plain, entry.token_hash):
            return entry.name
    raise HTTPException(status_code=401, detail="invalid or missing token")


def create_token(
    name: str,
    scopes: list[str] | None = None,
    auth_path: Path = AUTH_TOML_PATH,
) -> str:
    """生成新 token，hash 后存 ``data/auth.toml``，返回明文（仅此一次）。

    同名 token 覆盖（先删后加），保证唯一性。
    ``scopes`` 为 None 或空 list → 空 list 落盘（= 全权限，spec §5）。
    ``auth_path`` 参数供测试 monkeypatch。
    """
    plain = secrets.token_urlsafe(32)
    cfg = load_auth_config()
    cfg.api_tokens = [t for t in cfg.api_tokens if t.name != name]
    cfg.api_tokens.append(
        ApiTokenEntry(
            name=name,
            token_hash=_hash_token(plain),
            created_at=datetime.now(timezone.utc).timestamp(),
            scopes=list(scopes) if scopes else [],
        )
    )
    save_auth_config(cfg)
    return plain


def revoke_token(name: str, auth_path: Path = AUTH_TOML_PATH) -> bool:
    """按 name 删除 token，返回是否删除成功。

    无 name 匹配返回 False，不抛异常。``auth_path`` 参数供测试 monkeypatch。
    """
    cfg = load_auth_config()
    before = len(cfg.api_tokens)
    cfg.api_tokens = [t for t in cfg.api_tokens if t.name != name]
    if len(cfg.api_tokens) == before:
        return False
    save_auth_config(cfg)
    return True
