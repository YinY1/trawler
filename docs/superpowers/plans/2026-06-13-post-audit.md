# Post-Audit Improvement Plan

日期: 2026-06-13 | 状态: 待执行

---

## 任务清单

### 1. [可独立] `@staticmethod build_tokens_from_config` → 重构 (GL2-17-002)

**范围**: `platforms/bilibili/auth.py`, `platforms/weibo/auth.py`, `platforms/xiaohongshu/auth.py`,
`shared/auth/base.py`, `shared/auth/scheduler.py`

**问题**: GL2-17-002 明确禁止 `@staticmethod`（除集成现有库 API 外）。当前 3 个平台的
`build_tokens_from_config` 都是 `@staticmethod`，不依赖 `cls`，且在 scheduler 中通过
`authenticator.build_tokens_from_config(config)` 多态调用。

**选项**:
- A) 改为 `@classmethod` — 保留多态，但 `cls` 未使用
- B) 改为模块级函数 + scheduler 内 dispatch — 最纯粹，但引入 dispatch 逻辑
- C) 保持现状（接口契约模式）— 可论证这是 BaseAuthenticator 的接口规范

**预估**: 30min

---

### 2. [需协作] `xiaohongshu/auth.py` `self._session` Optional 类型收窄

**范围**: `platforms/xiaohongshu/auth.py`

**问题**: `_ensure_session()` 设置 `self._session` 但 pyright 无法追踪副作用，
产生 9 处 `reportOptionalMemberAccess` 报错。

**选项**:
- A) `_ensure_session` 返回 `ClientSession` + 调用方用局部变量
- B) 加 type narrowing callback / TypeGuard
- C) `self._session` 改为 `assert self._session is not None` 模式

**预估**: 1h，需确认哪种风格偏好

---

### 3. [需协作] `uv sync` 失败：xhs-downloader 不在 PyPI

**范围**: `pyproject.toml`

**问题**: `[project.optional-dependencies]` 中 `xhs = ["XHS-Downloader>=1.0"]`，
但 `XHS-Downloader` 不在 PyPI，`uv sync` 无法解析。

**选项**:
- A) 添加 git dependency: `"xhs-downloader @ git+https://..."`  
- B) 改为本地 path dependency（vendor/spider_xhs/）
- C) 移出 optional-deps，文档说明手动安装

**预估**: 15min，需用户提供 xhs-downloader 源码来源

---

### 4. [需协作] 真实联通测试

**范围**: 全部平台

**问题**: 代码修改后需验证各平台 token 续期、内容拉取、下载等功能正常。
当前环境缺少真实 API 凭证。

**需要**:
- B站: SESSDATA / bili_jct
- 微博: cookie
- 小红书: cookie

**预估**: 30min，依赖凭证可用性