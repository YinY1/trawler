"""Password hash behavior for web/auth.py.

锁定 ``hash_password`` / ``verify_password`` 的 bcrypt 语义。
导入目标：``web.auth``。Task 3 实现前应全部 fail (ImportError)。
"""

from __future__ import annotations

from passlib.context import CryptContext

from web.auth import hash_password, verify_password


def test_hash_password_returns_bcrypt_string() -> None:
    h = hash_password("hello123")
    assert isinstance(h, str)
    assert h.startswith("$2"), f"expected bcrypt prefix $2, got {h[:4]!r}"


def test_verify_password_correct() -> None:
    h = hash_password("hello123")
    assert verify_password("hello123", h) is True


def test_verify_password_wrong() -> None:
    h = hash_password("hello123")
    assert verify_password("wrong-pw", h) is False


def test_hash_password_different_each_time() -> None:
    h1 = hash_password("hello123")
    h2 = hash_password("hello123")
    assert h1 != h2, "bcrypt 应自带随机 salt，两次 hash 应不同"


def test_verify_password_accepts_legacy_bcrypt() -> None:
    """与直接 passlib CryptContext 产生的 hash 互操作。"""
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    legacy = ctx.hash("interop-pw")
    assert verify_password("interop-pw", legacy) is True


def test_verify_password_empty_hash_returns_false() -> None:
    """空 hash 不应抛异常，应返回 False。"""
    assert verify_password("x", "") is False
