"""Password hash behavior for web/auth.py.

锁定 ``hash_password`` / ``verify_password`` 的 argon2id 语义。

历史：原实现用 ``passlib CryptContext(schemes=["bcrypt"])``，但 passlib 1.7.x
与 bcrypt 4.1+ 不兼容（``bcrypt.__about__`` 已被移除），导致 CryptContext 探测
backend 时崩溃、静默回退到默认 bcrypt。直接用 ``argon2-cffi`` 的
:class:`argon2.PasswordHasher` 绕开 passlib，argon2id 是设计意图。
"""

from __future__ import annotations

from argon2 import PasswordHasher

from web.auth import hash_password, verify_password


def test_hash_password_returns_argon2_string() -> None:
    h = hash_password("hello123")
    assert isinstance(h, str)
    assert h.startswith("$argon2id$"), f"expected argon2id prefix, got {h[:12]!r}"


def test_verify_password_correct() -> None:
    h = hash_password("hello123")
    assert verify_password("hello123", h) is True


def test_verify_password_wrong() -> None:
    h = hash_password("hello123")
    assert verify_password("wrong-pw", h) is False


def test_hash_password_different_each_time() -> None:
    h1 = hash_password("hello123")
    h2 = hash_password("hello123")
    assert h1 != h2, "argon2 应自带随机 salt，两次 hash 应不同"


def test_verify_password_accepts_argon2_cffi_direct_hash() -> None:
    """与直接 argon2-cffi PasswordHasher 产生的 hash 互操作。"""
    hasher = PasswordHasher()
    legacy = hasher.hash("interop-pw")
    assert verify_password("interop-pw", legacy) is True


def test_verify_password_empty_hash_returns_false() -> None:
    """空 hash 不应抛异常，应返回 False。"""
    assert verify_password("x", "") is False


def test_verify_password_malformed_hash_returns_false() -> None:
    """格式错误的 hash（非 argon2 串、非空）应被当成 verify 失败返回 False，
    不能让 InvalidHash 异常上抛导致 500。"""
    assert verify_password("x", "not-a-valid-hash") is False


def test_verify_password_legacy_bcrypt_hash_returns_false() -> None:
    """彻底切换后：旧 bcrypt 哈希（``$2b$`` 前缀）应 verify 失败返回 False，
    不能再被验证通过。已部署实例需 reset 密码（详见 docs 变更说明）。"""
    legacy_bcrypt = "$2b$12$aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert verify_password("any-password", legacy_bcrypt) is False
