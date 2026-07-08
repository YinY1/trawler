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
from typing import Sequence

from fastapi import HTTPException, Request
from fastapi.security import SecurityScopes

from api.resource_filter import TokenResourceFilter
from shared.config import ApiTokenEntry
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
#: superuser 标识 scope（issue #108）。持此 scope 的 token bypass 所有
#: owner/assigned 检查，看全部 sub / endpoint / messages。
#: 同时也是 assign/unassign/adopt 路由的必需 scope。
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
    """token 是否满足 required scope（issue #108 破坏性变更）。

    **#108 变更**：空 scopes 不再 = 全权限。#105 设计「空 = 全权限」是为了
    向后兼容老 token，但实际部署中没人创建空 scope token（CLI 默认就提示）。
    #108 把 superuser 收紧为「显式持 tokens:manage」，空 scopes token 无任何权限。

    要成为 superuser：token.scopes 必须包含 ``tokens:manage``。
    """
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


def _authenticate_and_check_scope(
    request: Request, required_scopes: Sequence[str]
) -> ApiTokenEntry:
    """身份校验 + scope 校验共享逻辑（spec §6.3 私有 helper）。

    ``require_scopes`` 与 ``get_resource_filter`` 共用：抽出来避免 401/403
    处理逻辑两份复制（spec §6.3 风险表「重复」缓解措施）。

    - 无 header / token 不匹配 → 401 ``invalid or missing token``
    - 缺任一 required scope → 403 ``insufficient scope: requires xxx``
    - 通过 → 返回匹配的 ``ApiTokenEntry``（含 ``resource_rules``）

    返回完整 ``ApiTokenEntry`` 而非 ``name``，让 ``get_resource_filter`` 能
    一次性拿到 ``resource_rules`` 构造 ``TokenResourceFilter``。
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    plain = auth[len("Bearer ") :]
    cfg = load_auth_config()
    for entry in cfg.api_tokens:
        if _verify_token(plain, entry.token_hash):
            # 身份通过，校验 scope
            for required in required_scopes:
                if not token_has_scope(entry, required):
                    raise HTTPException(
                        status_code=403,
                        detail=f"insufficient scope: requires {required}",
                    )
            return entry
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
    - issue #108 破坏性变更：``token.scopes == []``（空 list）**不再** = 全权限，
      空 scopes token 任何 required scope 都被拒（403）。
      要 superuser 必须显式持 ``tokens:manage``（见 ``token_has_scope``）。

    保留供「不需要行级过滤」的路由（如 check）使用，与 ``get_resource_filter``
    共享 ``_authenticate_and_check_scope`` 私有 helper 避免 401/403 逻辑重复。
    """
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return entry.name


async def get_resource_filter(
    security_scopes: SecurityScopes,
    request: Request,
) -> TokenResourceFilter:
    """FastAPI 依赖：scope 校验 + 行级过滤视图构造（spec §6.3）。

    一个依赖同时承担两层职责（``Security(get_resource_filter, scopes=[...])``）：

    - 无 header / token 不匹配 → 401（同 ``require_scopes``）
    - 缺 scope → 403（同 ``require_scopes``）
    - 通过 → 返回 ``TokenResourceFilter.from_token(entry)``（含 token 的行级规则视图）

    路由层用 ``filt.allows_message(m)`` / ``filt.allows_subscription(p, r)`` 判断
    可见性，不直接读 ``ApiTokenEntry.resource_rules``（避免路由层处理 list/None
    分支，集中到 ``TokenResourceFilter``）。
    """
    entry = _authenticate_and_check_scope(request, security_scopes.scopes)
    return TokenResourceFilter.from_token(entry)


def create_token(
    name: str,
    scopes: list[str] | None = None,
    auth_path: Path = AUTH_TOML_PATH,
) -> str:
    """生成新 token，hash 后存 ``data/auth.toml``，返回明文（仅此一次）。

    同名 token 覆盖（先删后加），保证唯一性。
    ``scopes`` 为 None 或空 list → 空 list 落盘（#108 后空 = 无权，spec §6.2）。
    要创建 superuser token 需显式传 ``scopes=["tokens:manage"]``。
    ``auth_path`` 参数供测试 monkeypatch。

    issue #108 删除 ``resource_rules`` 参数（趁 #107 未部署）。
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
