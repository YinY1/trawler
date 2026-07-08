"""API token 鉴权 — ``Authorization: Bearer <token>``。

与 Web UI 的 session/CSRF 鉴权完全隔离（中间件层对 ``/api/*`` 豁免，
本模块通过 FastAPI 依赖 ``require_scopes`` 在路由层兜底鉴权 + scope 校验：
所有 12 个生产路由均挂 ``Security(require_scopes, scopes=[...])``）。
``require_token`` 保留作为测试桩，以及未来「不要求 scope」路由的等价依赖
（行为等价于 ``Security(require_scopes)`` 不传 scopes，见其 docstring）。

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
from fastapi.security import SecurityScopes

from shared.config import ApiTokenEntry, ResourceRules
from web.auth import AUTH_TOML_PATH, load_auth_config, save_auth_config

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# Token scopes（spec §4）
# ═══════════════════════════════════════════════════════════

#: Scope 常量。命名规范 ``<resource>:<action>``，全小写单数资源名。
#:
#: 消费 scope（6 个，路由层校验）：
SCOPE_SUBSCRIPTIONS_READ = "subscriptions:read"
SCOPE_SUBSCRIPTIONS_WRITE = "subscriptions:write"
SCOPE_MESSAGES_READ = "messages:read"
SCOPE_MESSAGES_WRITE = "messages:write"
SCOPE_CHECK_READ = "check:read"
SCOPE_CHECK_RUN = "check:run"
#:
#: 占位 scope（spec §3、§12）。**本 PR 不在路由层消费**，
#: 仅供未来 ``tokens:manage`` HTTP endpoint 或 CLI 校验白名单引用。
SCOPE_TOKENS_MANAGE = "tokens:manage"

#: 所有合法 scope（CLI ``--scope`` 白名单校验用）。包含 tokens:manage 占位。
ALL_SCOPES: tuple[str, ...] = (
    SCOPE_SUBSCRIPTIONS_READ,
    SCOPE_SUBSCRIPTIONS_WRITE,
    SCOPE_MESSAGES_READ,
    SCOPE_MESSAGES_WRITE,
    SCOPE_CHECK_READ,
    SCOPE_CHECK_RUN,
    SCOPE_TOKENS_MANAGE,
)

#: write → read 隐含规则映射（spec §4.3）。check:run / check:read 正交，不在此表。
_WRITE_IMPLIES_READ: dict[str, str] = {
    SCOPE_SUBSCRIPTIONS_WRITE: SCOPE_SUBSCRIPTIONS_READ,
    SCOPE_MESSAGES_WRITE: SCOPE_MESSAGES_READ,
}


def scope_implies(granted: str, required: str) -> bool:
    """判断 granted scope 是否满足 required（含 write→read 隐含）。

    - ``granted == required`` → True
    - granted 是某 resource 的 write，required 是同 resource read → True
    - 其余（不同 resource、read→write、check:run↔check:read）→ False
    """
    if granted == required:
        return True
    return _WRITE_IMPLIES_READ.get(granted) == required


def token_has_scope(token: ApiTokenEntry, required: str) -> bool:
    """token 是否满足 required scope。

    空 scopes（``[]``）= 全权限，永远返回 True（spec §5，仅运行时）。
    非 list 遍历 granted scope，调 ``scope_implies`` 判断。
    """
    if not token.scopes:
        return True
    return any(scope_implies(g, required) for g in token.scopes)


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


async def require_scopes(
    security_scopes: SecurityScopes,
    request: Request,
) -> str:
    """FastAPI 依赖：校验 ``Authorization: Bearer`` + 所需 scope（spec §6）。

    用 ``Security(require_scopes, scopes=[...])`` 挂到路由，FastAPI 自动：
    - 把 scope 列表注入 ``security_scopes.scopes``
    - 在 OpenAPI docs 渲染 security 字段（redoc / swagger UI 直接可见）

    错误码语义：
    - 无 header / 格式错 / token 不匹配 → 401（与 ``require_token`` 一致）
    - token 合法但缺 scope → 403 ``insufficient scope: requires xxx``
    - 通过 → 返回 token name（供日志/审计）

    特殊情况：
    - ``security_scopes.scopes == ()``（路由不要求 scope）→ 行为等价 ``require_token``
    - ``token.scopes == []``（空 list）= 全权限（spec §5），任何 required 都放行
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    plain = auth[len("Bearer ") :]
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if _verify_token(plain, entry.token_hash):
            # 身份通过，校验 scope
            for required in security_scopes.scopes:
                if not token_has_scope(entry, required):
                    raise HTTPException(
                        status_code=403,
                        detail=f"insufficient scope: requires {required}",
                    )
            return entry.name
    raise HTTPException(status_code=401, detail="invalid or missing token")


def create_token(
    name: str,
    scopes: list[str] | None = None,
    resource_rules: ResourceRules | None = None,
    auth_path: Path = AUTH_TOML_PATH,
) -> str:
    """生成新 token，hash 后存 ``data/auth.toml``，返回明文（仅此一次）。

    同名 token 覆盖（先删后加），保证唯一性。
    ``scopes`` 为 None 或空 list → 空 list 落盘（= 全权限，spec §5）。
    ``resource_rules`` 为 None → 默认 ``ResourceRules()``（两字段 None = 全权限，
    兼容老 token）。受限规则会序列化到 ``[resource_rules]`` 嵌套 table。
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
            resource_rules=resource_rules or ResourceRules(),
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
