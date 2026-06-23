# TODO: Fix passlib 1.7.x + bcrypt 5.x Incompatibility

**Severity**: High（生产风险）
**Discovered**: 2026-06-24 部署 trawler web auth 时发现

## 症状

容器内每次 `set_password()` / `verify_password()` 调用都打印：

```
(trapped) error reading bcrypt version
Traceback (most recent call last):
  File "/app/.venv/lib/python3.14/site-packages/passlib/handlers/bcrypt.py", line 620, in _load_backend_mixin
    version = _bcrypt.__about__.__version__
              ^^^^^^^^^
AttributeError: module 'bcrypt' has no attribute '__about__'
```

## 根因

- passlib 1.7.x 在初始化 bcrypt backend 时访问 `bcrypt.__about__.__version__`
- bcrypt 4.1+ 起把 `__about__` 属性删了（PEP 8 / 公共 API 整理），改用 `bcrypt.__version__`
- passlib 的 CryptContext 探测 backend 时崩溃 → scheme 链回退到默认 bcrypt（而非项目设计的 argon2）
- 结果：`auth.toml` 里实际落盘的是 `$2b$12$` bcrypt 哈希，不是 argon2 `$argon2id$`

## 为什么现在还能用

verify 和 hash 走同一个 fallback bcrypt backend，对称所以密码校验正常。但这是脆弱的"碰巧能跑"。

## 修复方向（任选其一）

### 方案 A：固定 bcrypt 版本（最小改动）

`pyproject.toml`:
```toml
dependencies = [
    ...
    "bcrypt<4.1",
    ...
]
```

**优点**：一行改动。**缺点**：bcrypt 老版本不会修安全 issue，治标不治本。

### 方案 B：绕开 passlib 直接用 argon2-cffi（推荐）

`web/auth.py` 改成直接调 `argon2.PasswordHasher()`（去掉 passlib CryptContext 依赖）：

```python
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

def hash_password(plain: str) -> str:
    return _hasher.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    try:
        _hasher.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False
```

**优点**：彻底去掉 passlib 依赖，argon2 是设计意图。
**缺点**：已有 bcrypt 哈希无法 verify（需要 reset 所有密码）。

### 方案 C：升级 passlib

passlib 1.7.4 是最后一版（项目似乎停更）。可能要等 1.8 或迁移到 `passwordHasher` 等替代库。**不推荐**。

## 建议执行

方案 B。理由：passlib 维护停滞、bcrypt fallback 偏离设计意图、argon2 是 password hashing competition 冠军算法。一次性切换最干净。

迁移策略：
1. 加 `pyproject.toml` 依赖 `argon2-cffi`
2. 改 `web/auth.py` 用 argon2 直接调
3. `set_password` 在 set 时检测旧 bcrypt 哈希并 log warning（"请通过 /settings/account 改密以升级到 argon2"）
4. 部署后手动改一次密码即可
