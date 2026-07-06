# 订阅通知端点 API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"绑定/解绑 endpoint 到订阅"逻辑抽到 `core/subscription_cli.py`，API 补全 endpoint CRUD 端点，并保留 `default_notify_endpoint` 单次调用语法糖（含回滚保护）。

**Architecture:** 方案 B —— core 抽离 endpoint 管理 + API 复用 core + Web 改成调 core。endpoint 存在性校验在 core 层做（调 `load_config()`），订阅不存在返回 `(False, "未找到订阅")` 而非抛 HTTPException，与现有 add/remove 订阅的错误语义一致。

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, tomlkit, pytest (+ asyncio)。所有源文件顶部 `from __future__ import annotations`，async + await 模式。

**参考文档：** spec `docs/superpowers/specs/2026-07-06-subscription-endpoint-api-design.md`

---

## File Structure

| 文件 | 改动类型 | 责任 |
|------|---------|------|
| `core/subscription_cli.py` | 扩展 | 新增 `add_endpoint_to_subscription` / `remove_endpoint_from_subscription`；扩展 `add_subscription` 加 `default_notify_endpoint` 参数 + 回滚 |
| `api/schemas.py` | 扩展 | `SubscriptionAddRequest` 加 `default_notify_endpoint` 字段；新增 `EndpointBindRequest` |
| `api/routes/subscriptions.py` | 扩展 | `add_sub` 透传新字段；新增 `bind_endpoint` / `unbind_endpoint` 路由 |
| `web/routes/subscriptions.py` | 重构 | `subscription_endpoint_add/remove` 改成调 core（保留 toast_key 重定向语义） |
| `web/templates/base.html` | 1 行追加 | `TOAST_KEY_MAP` 加 `subscription.endpoint_unknown` |
| `tests/test_subscription_cli.py` | 扩展 | 10 个新单元测试（Task 1: 4 + Task 2: 3 + Task 3: 3，含回滚） |
| `tests/test_api_subscriptions.py` | 扩展 | 7 个新集成测试（spec §7.2 列 5 + 兼容性 1 + 鉴权 1） |
| `tests/test_web_subscriptions.py` | 扩展 | 3 个新路由集成测试（Task 7.5b，spec §7 漏列） |

**依赖顺序**：core 实现 → schema → api route → web 重构 → 测试 → 验证。schema 可与 core 测试并行。

---

## Task 1: core — 新增 `add_endpoint_to_subscription` 函数

**Files:**
- Modify: `core/subscription_cli.py`（在 `remove_subscription` 之后、`search_by_name` section 之前插入新函数；约 line 226 之后）
- Test: `tests/test_subscription_cli.py`（追加 `TestAddEndpoint` class）

**复用要点（来自现有代码）：**
- `_load_doc(path)` —— 已存在（line 37-43），返回 `TOMLDocument | None`
- `_key_value(platform, identifier)` —— line 76-83，处理 int/str 类型转换
- `PLATFORM_TO_SECTION` —— line 21-25，CLI 短名（`bili/xhs/weibo`）→ 全名
- `SUBSCRIPTION_KEY` —— line 28-32，每平台的 id 字段名（`uid` vs `user_id`）
- `_match_sub(item, key, value)` —— line 86-88，字符串比对

- [ ] **Step 1.1: 写失败测试 `test_add_endpoint_to_subscription_ok`**

追加到 `tests/test_subscription_cli.py` 末尾（`TestSearchByName` class 之后）：

```python
# ── add_endpoint_to_subscription ──────────────────────────────────────


class TestAddEndpoint:
    """add_endpoint_to_subscription 用例。load_config 必须 monkeypatch。"""

    @pytest.fixture
    def mock_known_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """让 load_config 返回含 'gotify-main' 的 endpoints 列表。"""
        from shared.config import EndpointConfig
        from core import subscription_cli

        async def _fake_load(*_a, **_kw):
            from shared.config import Config
            cfg = Config()
            cfg.endpoints = [EndpointConfig(name="gotify-main", url="http://x", token="t")]
            return cfg

        monkeypatch.setattr(subscription_cli, "load_config", _fake_load)

    async def test_add_endpoint_to_subscription_ok(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok
        assert "已绑定" in msg
        # 验证落盘
        subs = await list_subscriptions(path=str(subs_file))
        assert "gotify-main" in subs["bilibili"][0]["notify_endpoints"]

    async def test_add_endpoint_to_subscription_idempotent(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        # 先加一次
        ok1, _ = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok1
        # 再加一次 — 幂等
        ok2, msg2 = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok2
        assert "已绑定" in msg2
        subs = await list_subscriptions(path=str(subs_file))
        # 不应重复
        eps = subs["bilibili"][0]["notify_endpoints"]
        assert eps.count("gotify-main") == 1

    async def test_add_endpoint_to_subscription_unknown_ep(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="nonexistent", path=str(subs_file),
        )
        assert not ok
        assert "未知 endpoint" in msg

    async def test_add_endpoint_to_subscription_no_sub(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import add_endpoint_to_subscription
        ok, msg = await add_endpoint_to_subscription(
            platform="bili", identifier=99999999,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert not ok
        assert "未找到订阅" in msg
```

- [ ] **Step 1.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_subscription_cli.py::TestAddEndpoint -v
```

预期：`ImportError: cannot import name 'add_endpoint_to_subscription' from 'core.subscription_cli'`。

- [ ] **Step 1.3: 实现函数**

在 `core/subscription_cli.py` 末尾（`remove_subscription` 函数后，`# Search by name` 注释 section 前，约 line 227）插入：

```python
# ═══════════════════════════════════════════════════════════
# Endpoint binding (notify_endpoints)
# ═══════════════════════════════════════════════════════════


async def add_endpoint_to_subscription(
    platform: str,
    identifier: int | str,
    endpoint_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """绑定 endpoint 到订阅的 ``notify_endpoints`` 列表。

    endpoint 存在性校验在 core 层做：调 ``load_config("config/config.toml")``
    检查 ``endpoint_name in {ep.name for ep in cfg.endpoints}``。

    返回值:
      ``(True, "已绑定: {endpoint_name}")``     # 成功或已存在（幂等）
      ``(False, "未找到订阅")``
      ``(False, "未知 endpoint: {endpoint_name}")``
      ``(False, "无效平台: {platform}, ...")``
    """
    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    # endpoint 存在性校验（spec §4.2 要点）
    from shared.config import load_config
    cfg = await load_config("config/config.toml")
    known = {ep.name for ep in cfg.endpoints}
    if endpoint_name not in known:
        logger.warning("📋 未知 endpoint: %s", endpoint_name)
        return False, f"未知 endpoint: {endpoint_name}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    doc = _load_doc(path)
    if doc is None:
        return False, "未找到订阅"

    doc_dict = cast(dict[str, Any], doc)  # tomlkit TOMLDocument 兼容 dict 访问
    plat_section_raw = doc_dict.get(section, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    subs = plat_section_raw.get("subscriptions", [])
    if not isinstance(subs, list):
        return False, "未找到订阅"

    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get(key, ""))
        if sub_id == str(typed_id):
            eps_arr = sub.get("notify_endpoints", [])
            eps_list = [str(e) for e in eps_arr] if eps_arr else []
            if endpoint_name not in eps_list:
                eps_list.append(endpoint_name)
                sub["notify_endpoints"] = eps_list
            found = True
            break

    if not found:
        return False, "未找到订阅"

    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("📋 endpoint 绑定: %s/%s += %s", section, typed_id, endpoint_name)
    return True, f"已绑定: {endpoint_name}"
```

**注意 import 位置**：`from shared.config import load_config` 写在函数内部（lazy import），避免在模块顶部与现有 `search_by_name` 风格不一致；现有代码所有 `load_config` import 都是函数内 lazy（见 `core/subscription_cli.py:275` 和 `:307`）。顶部加 import 会被 ruff 的循环导入检测触发。

**`cast` 必须显式断言**：`TOMLDocument` 不是 `dict[str, Any]` 的子类（tomlkit 自定义容器类型），直接 `doc_dict: dict[str, Any] = doc` 会让 pyright strict 报 `"TOMLDocument" is incompatible with "dict[str, Any]"`。必须用 `cast(dict[str, Any], doc)` 显式断言（与 `web/routes/subscriptions.py:118` 现有做法一致）。

**配套 import 改动**：把 `core/subscription_cli.py:8` 的 `from typing import Any` 改成：

```python
from typing import Any, cast
```

Task 2 的 `remove_endpoint_from_subscription` 沿用此处引入的 `cast`，**不重复加 import**。

- [ ] **Step 1.4: 运行测试确认通过**

```bash
uv run pytest -x tests/test_subscription_cli.py::TestAddEndpoint -v
```

预期：4 个测试全部 PASS。

- [ ] **Step 1.5: 提交**

```bash
git add core/subscription_cli.py tests/test_subscription_cli.py
git commit -m "feat(core): add_endpoint_to_subscription with endpoint existence check"
```

**工作量：** 15 min

---

## Task 2: core — 新增 `remove_endpoint_from_subscription` 函数

**依赖：** Task 1（共用 fixture `mock_known_endpoint`，**但 remove 不需要 endpoint 存在性校验**，所以 fixture 可选）

**Files:**
- Modify: `core/subscription_cli.py`（在 `add_endpoint_to_subscription` 函数后追加）
- Test: `tests/test_subscription_cli.py`（追加 `TestRemoveEndpoint` class）

- [ ] **Step 2.1: 写失败测试**

追加到 `tests/test_subscription_cli.py`：

```python
# ── remove_endpoint_from_subscription ─────────────────────────────────


class TestRemoveEndpoint:
    async def test_remove_endpoint_from_subscription_ok(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """先 add 再 remove，验证落盘后列表为空。"""
        from core.subscription_cli import (
            add_endpoint_to_subscription,
            remove_endpoint_from_subscription,
        )
        await add_endpoint_to_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        ok, msg = await remove_endpoint_from_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert ok
        assert "已解绑" in msg
        subs = await list_subscriptions(path=str(subs_file))
        eps = subs["bilibili"][0].get("notify_endpoints", [])
        assert "gotify-main" not in eps

    async def test_remove_endpoint_from_subscription_idempotent(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """remove 不存在的 endpoint 也返回 True（幂等）。"""
        from core.subscription_cli import remove_endpoint_from_subscription
        ok, msg = await remove_endpoint_from_subscription(
            platform="bili", identifier=2137589551,
            endpoint_name="never-bound", path=str(subs_file),
        )
        assert ok
        assert "已解绑" in msg

    async def test_remove_endpoint_from_subscription_no_sub(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        from core.subscription_cli import remove_endpoint_from_subscription
        ok, msg = await remove_endpoint_from_subscription(
            platform="bili", identifier=99999999,
            endpoint_name="gotify-main", path=str(subs_file),
        )
        assert not ok
        assert "未找到订阅" in msg
```

- [ ] **Step 2.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_subscription_cli.py::TestRemoveEndpoint -v
```

预期：`ImportError: cannot import name 'remove_endpoint_from_subscription'`。

- [ ] **Step 2.3: 实现函数**

在 `core/subscription_cli.py` 中 `add_endpoint_to_subscription` 之后追加：

```python
async def remove_endpoint_from_subscription(
    platform: str,
    identifier: int | str,
    endpoint_name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
    """从订阅的 ``notify_endpoints`` 列表移除一个 endpoint。

    幂等：endpoint 本来就不在列表里也返回成功。
    **不做 endpoint 存在性校验**（解绑一个不存在的 endpoint 引用无害，
    也能清理历史脏数据）。

    返回值:
      ``(True, "已解绑: {endpoint_name}")``     # 成功或本来就没有（幂等）
      ``(False, "未找到订阅")``
      ``(False, "无效平台: ...")``
    """
    if platform not in VALID_PLATFORMS:
        return False, f"无效平台: {platform}，有效平台: {', '.join(sorted(VALID_PLATFORMS))}"

    section = PLATFORM_TO_SECTION[platform]
    key, typed_id = _key_value(platform, identifier)
    p = Path(path)

    doc = _load_doc(path)
    if doc is None:
        return False, "未找到订阅"

    doc_dict = cast(dict[str, Any], doc)
    plat_section_raw = doc_dict.get(section, {})
    if not isinstance(plat_section_raw, dict):
        plat_section_raw = {}
    subs = plat_section_raw.get("subscriptions", [])
    if not isinstance(subs, list):
        return False, "未找到订阅"

    found = False
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        sub_id = str(sub.get(key, ""))
        if sub_id == str(typed_id):
            eps_arr = sub.get("notify_endpoints", [])
            eps_list = [str(e) for e in eps_arr if str(e) != endpoint_name]
            sub["notify_endpoints"] = eps_list
            found = True
            break

    if not found:
        return False, "未找到订阅"

    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("📋 endpoint 解绑: %s/%s -= %s", section, typed_id, endpoint_name)
    return True, f"已解绑: {endpoint_name}"
```

**关键决策**：`remove` **不调 `load_config`**（与 `add` 不对称），因为：(1) spec §4.2 只要求 add 做 endpoint 校验；(2) 解绑引用无害，能清理历史脏数据（如 toml 里手动写了已删除的 endpoint name）。

- [ ] **Step 2.4: 运行测试确认通过**

```bash
uv run pytest -x tests/test_subscription_cli.py::TestRemoveEndpoint -v
```

预期：3 个测试全部 PASS。

- [ ] **Step 2.5: 提交**

```bash
git add core/subscription_cli.py tests/test_subscription_cli.py
git commit -m "feat(core): remove_endpoint_from_subscription (idempotent, no existence check)"
```

**工作量：** 10 min

---

## Task 3: core — 扩展 `add_subscription` 加 `default_notify_endpoint` + 回滚

**依赖：** Task 1（`add_subscription` 在 default_notify_endpoint 非空时调 `add_endpoint_to_subscription`）、Task 2（回滚时调 `remove_subscription`，已存在）

**Files:**
- Modify: `core/subscription_cli.py:120-162`（`add_subscription` 函数签名 + 末尾分支）
- Test: `tests/test_subscription_cli.py`（追加 `TestAddSubscriptionDefaultEndpoint` class）

- [ ] **Step 3.1: 写失败测试**

追加到 `tests/test_subscription_cli.py`：

```python
# ── add_subscription with default_notify_endpoint ─────────────────────


class TestAddSubscriptionDefaultEndpoint:
    """语法糖 + 回滚用例。"""

    @pytest.fixture
    def mock_known_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared.config import Config, EndpointConfig
        from core import subscription_cli

        async def _fake_load(*_a, **_kw):
            cfg = Config()
            cfg.endpoints = [EndpointConfig(name="gotify-main", url="http://x", token="t")]
            return cfg

        monkeypatch.setattr(subscription_cli, "load_config", _fake_load)

    async def test_add_subscription_with_default_endpoint(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """default_notify_endpoint 合法 → 订阅被加 + endpoint 被绑定。"""
        ok, msg = await add_subscription(
            platform="bili", identifier=88888, name="新UP",
            path=str(subs_file), default_notify_endpoint="gotify-main",
        )
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        names = [s["name"] for s in subs["bilibili"]]
        assert "新UP" in names
        # endpoint 被绑定
        target = next(s for s in subs["bilibili"] if s["uid"] == 88888)
        assert "gotify-main" in target["notify_endpoints"]

    async def test_add_subscription_with_bad_default_endpoint(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """default_notify_endpoint 不存在 → 回滚，订阅不应被加入文件。"""
        ok, msg = await add_subscription(
            platform="bili", identifier=77777, name="回滚UP",
            path=str(subs_file), default_notify_endpoint="bad-ep",
        )
        assert not ok
        assert "默认 endpoint 绑定失败" in msg
        # 关键断言：订阅被回滚删除
        subs = await list_subscriptions(path=str(subs_file))
        uids = [s["uid"] for s in subs.get("bilibili", [])]
        assert 77777 not in uids

    async def test_add_subscription_without_default_endpoint(
        self, subs_file: Path, mock_known_endpoint: None
    ) -> None:
        """不传 default_notify_endpoint → 行为完全不变（向后兼容）。"""
        ok, msg = await add_subscription(
            platform="bili", identifier=66666, name="纯加",
            path=str(subs_file),
        )
        assert ok
        assert "已添加" in msg
        subs = await list_subscriptions(path=str(subs_file))
        target = next(s for s in subs["bilibili"] if s["uid"] == 66666)
        # 不应有 notify_endpoints 字段（保持现有行为）
        assert "notify_endpoints" not in target or target["notify_endpoints"] == []
```

- [ ] **Step 3.2: 运行测试确认失败**

```bash
uv run pytest -x tests/test_subscription_cli.py::TestAddSubscriptionDefaultEndpoint -v
```

预期：`TypeError: add_subscription() got an unexpected keyword argument 'default_notify_endpoint'`。

- [ ] **Step 3.3: 修改 `add_subscription` 签名 + 加回滚**

修改 `core/subscription_cli.py:120-162`。

**改前签名（line 120-125）：**

```python
async def add_subscription(
    platform: str,
    identifier: int | str,
    name: str,
    path: str = "config/subscriptions.toml",
) -> tuple[bool, str]:
```

**改后签名：**

```python
async def add_subscription(
    platform: str,
    identifier: int | str,
    name: str,
    path: str = "config/subscriptions.toml",
    default_notify_endpoint: str | None = None,
) -> tuple[bool, str]:
```

**改前末尾 return（位于 `core/subscription_cli.py` `# Write back` block 之后，约 line 160-162）：**

> **唯一锚点是 `return True, f"已添加: {name}"` 这一行**。用 `edit` 工具时把它作为 oldString 的尾部标识，匹配前方 `# Write back` 三行（`p.parent.mkdir(...) / p.write_text(...) / logger.info(...)`）+ 该 return 共 4 行作为完整 oldString。

```python
    # Write back
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("Added subscription: %s/%s = %s (%s)", section, key, typed_id, name)
    return True, f"已添加: {name}"
```

**改后末尾（加回滚分支）：**

```python
    # Write back
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomlkit.dumps(doc), encoding="utf-8")
    logger.info("Added subscription: %s/%s = %s (%s)", section, key, typed_id, name)

    # ── default_notify_endpoint 语法糖（spec §4.3）──────────────
    # 落盘后再调 endpoint 绑定；失败时回滚（删订阅 + 重写文件）。
    if default_notify_endpoint is not None:
        ok_ep, msg_ep = await add_endpoint_to_subscription(
            platform=platform,
            identifier=identifier,
            endpoint_name=default_notify_endpoint,
            path=path,
        )
        if not ok_ep:
            logger.warning("📋 默认 endpoint 绑定失败，回滚订阅: %s", msg_ep)
            await remove_subscription(platform=platform, identifier=identifier, path=path)
            return False, f"默认 endpoint 绑定失败: {msg_ep}"

    return True, f"已添加: {name}"
```

**关键决策**：
- 回滚直接 `await remove_subscription(...)`，复用现有删除逻辑（已处理"未找到"等边界，但此处不会触发，因为刚加完）。
- 文件已落盘后再调 endpoint 绑定 —— `add_endpoint_to_subscription` 内部会再读一次盘，找到刚加的条目，符合 spec §4.3 行为1。
- `default_notify_endpoint=None` 完全跳过分支，向后兼容。

- [ ] **Step 3.4: 运行测试确认通过**

```bash
uv run pytest -x tests/test_subscription_cli.py::TestAddSubscriptionDefaultEndpoint -v
```

预期：3 个测试全部 PASS。

- [ ] **Step 3.5: 跑全部现有测试，确认无回归**

```bash
uv run pytest -x tests/test_subscription_cli.py -v
```

预期：全部 PASS（31 个，含原 21 个 + Task 1 的 4 + Task 2 的 3 + 本 Task 的 3 = 31）。

- [ ] **Step 3.6: 提交**

```bash
git add core/subscription_cli.py tests/test_subscription_cli.py
git commit -m "feat(core): add_subscription supports default_notify_endpoint with rollback"
```

**工作量：** 15 min

---

## Task 4: api/schemas.py — 加字段 + 新 `EndpointBindRequest`

**依赖：** 无（可与 Task 1-3 并行）

**Files:**
- Modify: `api/schemas.py:173-181`（`SubscriptionAddRequest` 加字段）
- 新增（同文件末尾，约 line 195 后）：`EndpointBindRequest` class

- [ ] **Step 4.1: 修改 `SubscriptionAddRequest`**

`api/schemas.py:173-181`：

```python
class SubscriptionAddRequest(BaseModel):
    """``POST /subscriptions`` 请求体。

    ``identifier`` 在 API 层统一为 str，``add_subscription`` 内部按平台转 int/str。
    ``default_notify_endpoint`` 可选，传入时会在添加订阅后绑定该 endpoint；
    endpoint 不存在时回滚订阅添加，返回 ``success=False``。
    """

    platform: str
    identifier: str
    name: str
    default_notify_endpoint: str | None = None
```

- [ ] **Step 4.2: 追加 `EndpointBindRequest`**

在 `api/schemas.py` 末尾（`SubscriptionRemoveResponse` 之后）追加：

```python
class EndpointBindRequest(BaseModel):
    """``POST /subscriptions/{platform}/{identifier}/endpoints`` 请求体。

    仅一个字段 —— ``endpoint_name``。响应复用 ``SubscriptionAddResponse``，
    不为 endpoint 端点新建 schema（YAGNI，spec §4.4）。
    """

    endpoint_name: str
```

- [ ] **Step 4.3: 验证（无需测试，import 检查即可）**

```bash
uv run python -c "from api.schemas import EndpointBindRequest, SubscriptionAddRequest; print(EndpointBindRequest(endpoint_name='x').endpoint_name); print(SubscriptionAddRequest(platform='bili', identifier='1', name='n').default_notify_endpoint is None)"
```

预期输出：

```
x
True
```

- [ ] **Step 4.4: 提交**

```bash
git add api/schemas.py
git commit -m "feat(api): add EndpointBindRequest + default_notify_endpoint field"
```

**工作量：** 5 min

---

## Task 5: api/routes/subscriptions.py — 透传 + 新增 endpoint CRUD 路由

**依赖：** Task 4（需要 `EndpointBindRequest` 和加字段的 `SubscriptionAddRequest`）。Task 1-3 不强依赖（mock 测试时 core 函数已存在即可，建议 Task 5 在 Task 3 之后执行避免 import error）。

**Files:**
- Modify: `api/routes/subscriptions.py:43-55`（`add_sub` 透传新字段）
- 同文件末尾追加两个路由（`bind_endpoint` / `unbind_endpoint`）
- 同文件 imports（line 20-26）追加 `EndpointBindRequest`

- [ ] **Step 5.1: 修改 imports**

`api/routes/subscriptions.py:20-26`：

```python
from api.schemas import (
    EndpointBindRequest,
    SubscriptionAddRequest,
    SubscriptionAddResponse,
    SubscriptionListResponse,
    SubscriptionRemoveResponse,
)
from core.subscription_cli import (
    add_endpoint_to_subscription,
    add_subscription,
    list_subscriptions,
    remove_endpoint_from_subscription,
    remove_subscription,
)
```

- [ ] **Step 5.2: 修改 `add_sub` 透传字段**

`api/routes/subscriptions.py:43-55`：

```python
@router.post("/subscriptions", response_model=SubscriptionAddResponse)
async def add_sub(
    body: SubscriptionAddRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    """添加订阅。

    ``add_subscription`` 返回 ``(False, "已存在: ...")`` 也是 200 正常响应
    （``success=False``），不映射成 4xx —— 重复 / 无效平台是业务可恢复态。
    ``default_notify_endpoint`` 非空时，底层会尝试绑定，失败会回滚订阅添加
    并返回 ``(False, "默认 endpoint 绑定失败: ...")``。
    """
    success, message = await add_subscription(
        body.platform,
        body.identifier,
        body.name,
        default_notify_endpoint=body.default_notify_endpoint,
    )
    return SubscriptionAddResponse(success=success, message=message)
```

- [ ] **Step 5.3: 追加 `bind_endpoint` / `unbind_endpoint` 路由**

在 `api/routes/subscriptions.py` 末尾（`remove_sub` 函数后）追加：

```python
# ── endpoint 绑定/解绑（spec §4.5）──────────────────────────────────────
# 与 add/remove 订阅端点对称：业务可恢复态（未找到订阅 / 未知 endpoint / 幂等）
# 全部返回 200 + success 字段，不映射 4xx。


@router.post(
    "/subscriptions/{platform}/{identifier}/endpoints",
    response_model=SubscriptionAddResponse,
)
async def bind_endpoint(
    platform: str,
    identifier: str,
    body: EndpointBindRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    """绑定 endpoint 到订阅。

    ``platform`` 使用全名（``bilibili`` / ``xiaohongshu`` / ``weibo``），
    与现有 ``DELETE /subscriptions/{platform}/{identifier}`` 一致。

    响应语义：
    - 成功（首次/幂等）→ ``success=True``
    - 订阅不存在 → ``success=False``，message="未找到订阅"
    - endpoint 不在 ``[[endpoints]]`` 中 → ``success=False``，message="未知 endpoint: ..."
    """
    success, message = await add_endpoint_to_subscription(
        platform, identifier, body.endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)


@router.delete(
    "/subscriptions/{platform}/{identifier}/endpoints/{endpoint_name}",
    response_model=SubscriptionAddResponse,
)
async def unbind_endpoint(
    platform: str,
    identifier: str,
    endpoint_name: str,
    request: Request,
    _token_name: str = Depends(require_token),
) -> SubscriptionAddResponse:
    """解绑 endpoint。订阅不存在返回 ``success=False``，其余（含幂等）返回 True。

    不做 endpoint 存在性校验（解绑引用无害，能清理历史脏数据）。
    """
    success, message = await remove_endpoint_from_subscription(
        platform, identifier, endpoint_name
    )
    return SubscriptionAddResponse(success=success, message=message)
```

**关键说明**：
- `EndpointBindRequest` 作为 `body: EndpointBindRequest` 注入，FastAPI 自动从 JSON body 解析 `endpoint_name`。
- `unbind_endpoint` 的 `endpoint_name` 来自 URL path 参数（不在 body）—— 与 spec §4.5 一致。
- 所有路由保留 `request: Request` 参数（虽然函数体未用，但与现有 `add_sub` / `remove_sub` 风格一致，便于后续扩展 logging）。

- [ ] **Step 5.4: 运行 ruff / pyright 自检**

```bash
uv run ruff check api/routes/subscriptions.py
uv run pyright
```

预期：0 error。

- [ ] **Step 5.5: 提交**

```bash
git add api/routes/subscriptions.py
git commit -m "feat(api): add endpoint bind/unbind routes + default_notify_endpoint passthrough"
```

**工作量：** 10 min

---

## Task 6: API 集成测试 — `tests/test_api_subscriptions.py`

**依赖：** Task 4 + Task 5（schema 和路由就绪）。沿用现有 `authed_client` fixture（文件 line 29-55）+ `@patch("api.routes.subscriptions.<func>", new_callable=AsyncMock)` 模式（与 line 138、196 一致）。

**Files:**
- Modify: `tests/test_api_subscriptions.py`（末尾追加 `TestBindEndpoint` / `TestUnbindEndpoint` / `TestAddSubscriptionWithDefault` classes）

- [ ] **Step 6.1: 写 5 个测试**

追加到 `tests/test_api_subscriptions.py` 末尾（`TestRemoveSubscription` class 之后）：

```python
# ── POST /subscriptions/{platform}/{identifier}/endpoints ─────────────


class TestBindEndpoint:
    @patch("api.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_api_bind_endpoint_ok(
        self,
        mock_bind: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """add_endpoint_to_subscription 返回 (True, "已绑定: ...") → 200 success=True。"""
        mock_bind.return_value = (True, "已绑定: gotify-main")
        resp = await authed_client.post(
            "/api/v1/subscriptions/bilibili/123/endpoints",
            json={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "已绑定" in data["message"]
        mock_bind.assert_awaited_once_with("bilibili", "123", "gotify-main")

    @patch("api.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_api_bind_endpoint_unknown(
        self,
        mock_bind: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """未知 endpoint → 200 success=False（不映射 4xx）。"""
        mock_bind.return_value = (False, "未知 endpoint: bad-ep")
        resp = await authed_client.post(
            "/api/v1/subscriptions/bilibili/123/endpoints",
            json={"endpoint_name": "bad-ep"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "未知 endpoint" in data["message"]

    @patch("api.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_api_bind_endpoint_no_sub(
        self,
        mock_bind: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """订阅不存在 → 200 success=False。"""
        mock_bind.return_value = (False, "未找到订阅")
        resp = await authed_client.post(
            "/api/v1/subscriptions/bilibili/9999/endpoints",
            json={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["message"] == "未找到订阅"

    async def test_api_bind_endpoint_no_token_returns_401(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 token → 401。"""
        c = await _make_no_token_client(tmp_path, monkeypatch)
        try:
            resp = await c.post(
                "/api/v1/subscriptions/bilibili/123/endpoints",
                json={"endpoint_name": "gotify-main"},
            )
            assert resp.status_code == 401
        finally:
            await c.__aexit__(None, None, None)


# ── DELETE /subscriptions/{platform}/{identifier}/endpoints/{name} ────


class TestUnbindEndpoint:
    @patch("api.routes.subscriptions.remove_endpoint_from_subscription", new_callable=AsyncMock)
    async def test_api_unbind_endpoint_ok(
        self,
        mock_unbind: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """remove 返回 (True, "已解绑: ...") → 200 success=True。"""
        mock_unbind.return_value = (True, "已解绑: gotify-main")
        resp = await authed_client.delete(
            "/api/v1/subscriptions/bilibili/123/endpoints/gotify-main"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "已解绑" in data["message"]
        mock_unbind.assert_awaited_once_with("bilibili", "123", "gotify-main")


# ── POST /subscriptions with default_notify_endpoint ─────────────────


class TestAddSubscriptionWithDefault:
    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_api_add_subscription_with_default(
        self,
        mock_add: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """请求体含 default_notify_endpoint → 透传给 add_subscription。"""
        mock_add.return_value = (True, "已添加: UP1")
        resp = await authed_client.post(
            "/api/v1/subscriptions",
            json={
                "platform": "bili",
                "identifier": "123",
                "name": "UP1",
                "default_notify_endpoint": "gotify-main",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        # 关键：default_notify_endpoint 透传到 core
        mock_add.assert_awaited_once_with(
            "bili", "123", "UP1", default_notify_endpoint="gotify-main"
        )

    @patch("api.routes.subscriptions.add_subscription", new_callable=AsyncMock)
    async def test_api_add_subscription_without_default_omits_kwarg(
        self,
        mock_add: AsyncMock,
        authed_client: AsyncClient,
    ) -> None:
        """请求体不含 default_notify_endpoint → pydantic 默认 None，
        add_subscription 仍被以关键字参数形式调用（值为 None）。"""
        mock_add.return_value = (True, "已添加: UP1")
        resp = await authed_client.post(
            "/api/v1/subscriptions",
            json={"platform": "bili", "identifier": "123", "name": "UP1"},
        )
        assert resp.status_code == 200
        mock_add.assert_awaited_once_with(
            "bili", "123", "UP1", default_notify_endpoint=None
        )
```

**关键点**：
- 沿用现有 `authed_client` fixture（不重写）。
- 所有 core 函数全 mock，**不触真 `config/subscriptions.toml`**（与文件 docstring line 11-12 的约定一致）。
- 路径前缀 `/api/v1/`（与现有 line 85、101、117 测试一致）。
- 实际新增 **7 条测试**（spec §7.2 列 5 条 + 兼容性 1 条 `test_api_add_subscription_without_default_omits_kwarg` + 鉴权 1 条 `test_api_bind_endpoint_no_token_returns_401`）。
  - `test_api_add_subscription_without_default_omits_kwarg`：验证请求体不含 `default_notify_endpoint` 时，pydantic 默认 None，`add_subscription` 仍被以 kwarg 形式调用，保证 core `if default_notify_endpoint is not None` 分支能正确判空。
  - `test_api_bind_endpoint_no_token_returns_401`：常规鉴权测试，归到 `TestBindEndpoint` class 内（与现有 `test_*_no_token_returns_401` 风格一致）。

- [ ] **Step 6.2: 运行测试**

```bash
uv run pytest -x tests/test_api_subscriptions.py -v
```

预期：全部 PASS（原 12 个 + 新增 7 个 = **19 个**）。

- [ ] **Step 6.3: 提交**

```bash
git add tests/test_api_subscriptions.py
git commit -m "test(api): endpoint bind/unbind + default_notify_endpoint integration tests"
```

**工作量：** 20 min

---

## Task 7: web/routes/subscriptions.py — 重构 endpoint 路由调 core

**依赖：** Task 1 + Task 2（core 函数就绪）。

**Files:**
- Modify: `web/routes/subscriptions.py:101-194`（替换两个函数体）
- Modify: `web/routes/subscriptions.py:1-13`（imports：删 `tomlkit`、`Path`、`cast`、`Any` 中已不再用的部分；加 core 函数 import）

**净行数变化**：原 94 行 → 约 50 行（spec §4.1 估算 -60 行）。

- [ ] **Step 7.1: 修改 imports**

`web/routes/subscriptions.py:1-13`，**改前**：

```python
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import tomlkit
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
from shared.config import load_config
from web.app import TEMPLATES
```

**改后**（删 `Path`、`Any`、`cast`、`tomlkit`，加 `add_endpoint_to_subscription` / `remove_endpoint_from_subscription`）：

```python
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from core.subscription_cli import (
    add_endpoint_to_subscription,
    add_subscription,
    list_subscriptions,
    remove_endpoint_from_subscription,
    remove_subscription,
    search_by_name,
)
from shared.config import load_config
from web.app import TEMPLATES
```

**注意**：`Any` 保留（`subscriptions_page` 函数 line 28 的 `platforms_data: list[dict[str, Any]]` 还在用）。

- [ ] **Step 7.2: 重写 `subscription_endpoint_add`**

替换 `web/routes/subscriptions.py:101-148`，**改前**是 48 行的 tomlkit 直写逻辑，**改后**：

```python
@router.post("/subscriptions/{platform}/{identifier}/endpoints/add")
async def subscription_endpoint_add(
    platform: str,
    identifier: str,
    endpoint_name: str = Form(...),
) -> RedirectResponse:
    """绑定 endpoint 到订阅（重构后调 core 函数）。

    Web 路径用短名 ``bili/xhs/weibo``，core 函数要全名，转换在调用前做。
    """
    plat_name = _platform_key_to_name(platform)
    ok, msg = await add_endpoint_to_subscription(plat_name, identifier, endpoint_name)
    if not ok and "未找到订阅" in msg:
        toast_key, t = "subscription.not_found", "error"
    elif not ok:  # 未知 endpoint
        toast_key, t = "subscription.endpoint_unknown", "error"
    else:
        toast_key, t = "subscription.endpoint_added", "success"
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}", status_code=303
    )
```

- [ ] **Step 7.3: 重写 `subscription_endpoint_remove`**

替换 `web/routes/subscriptions.py:151-194`：

```python
@router.post("/subscriptions/{platform}/{identifier}/endpoints/remove")
async def subscription_endpoint_remove(
    platform: str,
    identifier: str,
    endpoint_name: str = Form(...),
) -> RedirectResponse:
    """从订阅解绑 endpoint（重构后调 core 函数）。

    订阅不存在 → ``subscription.not_found``；其余（含幂等）→ success。
    """
    plat_name = _platform_key_to_name(platform)
    ok, msg = await remove_endpoint_from_subscription(plat_name, identifier, endpoint_name)
    if not ok and "未找到订阅" in msg:
        toast_key, t = "subscription.not_found", "error"
    else:
        toast_key, t = "subscription.endpoint_removed", "success"
    return RedirectResponse(
        url=f"/subscriptions?toast_key={toast_key}&type={t}", status_code=303
    )
```

**关键说明（spec §4.6）**：
- toast_key 区分逻辑用 `"未找到订阅" in msg` 字符串匹配（spec 明确这么写，决策4）。
- Web 路径**仍是** `/subscriptions/{platform}/{identifier}/endpoints/add`（短名 `bili/xhs/weibo`），与 API 路径 `/api/v1/subscriptions/{platform}/...`（全名）解耦。
- `_platform_key_to_name` 已存在（line 96-98），保留不动。

- [ ] **Step 7.4: 加 toast_key 到 `TOAST_KEY_MAP`**

`web/templates/base.html:128-139`，**改前**：

```javascript
    var TOAST_KEY_MAP = {
      'settings.saved': '设置已保存',
      'endpoint.saved': '端点已保存',
      'endpoint.deleted': '端点已删除',
      'endpoint.name_exists': '端点名称已存在',
      'endpoint.not_found': '端点不存在',
      'subscription.not_found': '订阅不存在',
      'subscription.endpoint_added': '端点已添加',
      'subscription.endpoint_removed': '端点已移除',
      'message.retry_success': '消息已重置，cron 将重新处理',
      'message.retry_failed': '重试失败：消息状态异常'
    };
```

**改后**（在 `subscription.endpoint_added` 行之前插入一行 `subscription.endpoint_unknown`）：

```javascript
    var TOAST_KEY_MAP = {
      'settings.saved': '设置已保存',
      'endpoint.saved': '端点已保存',
      'endpoint.deleted': '端点已删除',
      'endpoint.name_exists': '端点名称已存在',
      'endpoint.not_found': '端点不存在',
      'subscription.not_found': '订阅不存在',
      'subscription.endpoint_unknown': '通知端点不存在',
      'subscription.endpoint_added': '端点已添加',
      'subscription.endpoint_removed': '端点已移除',
      'message.retry_success': '消息已重置，cron 将重新处理',
      'message.retry_failed': '重试失败：消息状态异常'
    };
```

- [ ] **Step 7.5: 运行 Web 现有测试，确认无回归**

```bash
uv run pytest -x tests/test_web_subscriptions.py -v
```

预期：原有测试全部 PASS（Web 路由行为对外不变，仅实现层重构）。

**如果 `test_web_subscriptions.py` 有针对 endpoint add/remove 的细粒度断言（比如断言 toml 内容），需要查看并按需更新**。先跑一遍看哪些挂。

- [ ] **Step 7.5b：补 Web endpoint 路由集成测试**

**文件**：`tests/test_web_subscriptions.py`（沿用现有 fixture 风格）

**背景**：现有 `test_web_subscriptions.py` 完全没有覆盖 `subscription_endpoint_add` /
`subscription_endpoint_remove`。重构后必须补测试，避免下次改坏。

**新增 3 个测试**（参考现有 `test_add_redirects` 的 mock + 路由调用模式）：

1. `test_endpoint_add_success_redirects_to_added`
   - mock `web.routes.subscriptions.add_endpoint_to_subscription` 返回 `(True, "已绑定: gotify-main")`
   - POST `/subscriptions/bili/123/endpoints/add` body `endpoint_name=gotify-main`
   - 断言 redirect status_code == 303
   - 断言 Location 含 `toast_key=subscription.endpoint_added&type=success`

2. `test_endpoint_add_unknown_redirects_to_unknown`
   - mock 返回 `(False, "未知 endpoint: xxx")`
   - 断言 Location 含 `toast_key=subscription.endpoint_unknown&type=error`

3. `test_endpoint_add_no_sub_redirects_to_not_found`
   - mock 返回 `(False, "未找到订阅")`
   - 断言 Location 含 `toast_key=subscription.not_found&type=error`

**测试代码骨架**（贴到 `tests/test_web_subscriptions.py` 末尾）：

```python
# ── subscription_endpoint_add（spec §4.6 / Task 7.5b）──────────────────


class TestEndpointAddRedirect:
    @patch("web.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_endpoint_add_success_redirects_to_added(
        self,
        mock_add: AsyncMock,
        web_client: AsyncClient,
    ) -> None:
        mock_add.return_value = (True, "已绑定: gotify-main")
        resp = await web_client.post(
            "/subscriptions/bili/123/endpoints/add",
            data={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=subscription.endpoint_added" in loc
        assert "type=success" in loc

    @patch("web.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_endpoint_add_unknown_redirects_to_unknown(
        self,
        mock_add: AsyncMock,
        web_client: AsyncClient,
    ) -> None:
        mock_add.return_value = (False, "未知 endpoint: bad-ep")
        resp = await web_client.post(
            "/subscriptions/bili/123/endpoints/add",
            data={"endpoint_name": "bad-ep"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=subscription.endpoint_unknown" in loc
        assert "type=error" in loc

    @patch("web.routes.subscriptions.add_endpoint_to_subscription", new_callable=AsyncMock)
    async def test_endpoint_add_no_sub_redirects_to_not_found(
        self,
        mock_add: AsyncMock,
        web_client: AsyncClient,
    ) -> None:
        mock_add.return_value = (False, "未找到订阅")
        resp = await web_client.post(
            "/subscriptions/bili/123/endpoints/add",
            data={"endpoint_name": "gotify-main"},
        )
        assert resp.status_code == 303
        loc = resp.headers["location"]
        assert "toast_key=subscription.not_found" in loc
        assert "type=error" in loc
```

**注意**：`web_client` fixture 沿用现有 `test_web_subscriptions.py` 中的命名（如叫 `client` 则全局替换）。

**验证**：

```bash
uv run pytest -x tests/test_web_subscriptions.py -v
```

预期：原 5 个 + 新增 3 个 = **8 个 PASS**。

- [ ] **Step 7.6: 落地检查 toast_key**

```bash
grep 'subscription.endpoint_unknown' web/templates/base.html
```

预期输出 1 行（不是 0 行）。

- [ ] **Step 7.7: 提交**

```bash
git add web/routes/subscriptions.py web/templates/base.html
git commit -m "refactor(web): endpoint add/remove routes delegate to core; add endpoint_unknown toast"
```

**工作量：** 15 min

---

## Task 8: 全量验证 — ruff + pyright + pytest + grep

**依赖：** Task 1-7 全部完成。

**Files:** 无（只跑命令）。

- [ ] **Step 8.1: ruff**

```bash
uv run ruff check .
```

预期：`All checks passed!`。

**如果有 import 顺序问题**，跑 `uv run ruff check --fix .` 自动修，然后人工 diff 确认仅 import 顺序变化。

- [ ] **Step 8.2: pyright**

```bash
uv run pyright
```

**注意：不要加 `.` 参数！见 AGENTS.md gotcha。** 加 `.` 会让 pyright 扫 `.venv/`，9601 文件卡死。

预期：`0 errors, 0 warnings`。

**常见可能告警**：
- ~~`core/subscription_cli.py` 新函数中 `doc_dict: dict[str, Any] = doc` 给 TOMLDocument 做类型断言，pyright 可能提示。如出问题，改成 `doc_dict = cast(dict[str, Any], doc)` 并 `from typing import cast`。~~ **已在 Task 1 Step 1.3 + Task 2 Step 2.3 改用 `cast(dict[str, Any], doc)`（与 Web 路由一致），不会触发该告警。**
- 测试中 `mock_known_endpoint` fixture 返回 None 但参数标 `-> None`，pyright 可能挑刺；如出问题改成 `-> None` 显式（已是这样）。

- [ ] **Step 8.3: core 单元测试**

```bash
uv run pytest -x tests/test_subscription_cli.py -v
```

预期：31 个测试 PASS（原 21 + Task 1 的 4 + Task 2 的 3 + Task 3 的 3 = 31）。

- [ ] **Step 8.4: API 集成测试**

```bash
uv run pytest -x tests/test_api_subscriptions.py -v
```

预期：19 个测试 PASS（原 12 + Task 6 新增 7）。

- [ ] **Step 8.5: Web 回归测试**

```bash
uv run pytest -x tests/test_web_subscriptions.py -v
```

预期：**8 个测试 PASS**（原 5 + Task 7.5b 新增 3）。

- [ ] **Step 8.6: 全量回归**

```bash
uv run pytest -x
```

预期：所有测试 PASS。如有 flaky 或无关失败，单独排查，不要在本 PR 修。

- [ ] **Step 8.7: toast_key 落地检查**

```bash
grep 'subscription.endpoint_unknown' web/templates/base.html
```

预期：1 行匹配。

- [ ] **Step 8.8: （可选）手动验证**

仅当前 7 个 Task 全绿且要在本地确认端到端可用时跑：

```bash
# 启动 API（具体入口看 run_check.py / pyproject [project.scripts]）
uv run trawler serve &

# 1. 语法糖 add+bind
curl -X POST http://localhost:8000/api/v1/subscriptions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"platform":"bilibili","identifier":"123","name":"UP","default_notify_endpoint":"gotify-main"}'

# 2. 事后绑定
curl -X POST http://localhost:8000/api/v1/subscriptions/bilibili/123/endpoints \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"endpoint_name":"gotify-backup"}'

# 3. 解绑
curl -X DELETE http://localhost:8000/api/v1/subscriptions/bilibili/123/endpoints/gotify-main \
  -H "Authorization: Bearer <token>"

# 4. 检查 toml
cat config/subscriptions.toml

# 5. 确认前端字典
grep 'subscription.endpoint_unknown' web/templates/base.html
```

**工作量：** 10 min

---

## Self-Review 结果

### Spec coverage

| spec 章节 | 对应 Task |
|----------|----------|
| §4.1 文件改动总览 | 全部 Task 覆盖（6 源文件 + 2 测试文件） |
| §4.2 core 新增函数 | Task 1 (`add_endpoint_to_subscription`) + Task 2 (`remove_endpoint_from_subscription`) |
| §4.3 `add_subscription` 扩展 + 回滚 | Task 3 |
| §4.4 api/schemas.py 扩展 | Task 4 |
| §4.5 api/routes 扩展 | Task 5 |
| §4.6 web 重构 + toast_key | Task 7 |
| §6 错误处理矩阵 | Task 1/2/5 实现 + Task 6 测试覆盖 |
| §7.1 core 单元测试 10 条 | Task 1 (4) + Task 2 (3) + Task 3 (3) = **10**；spec §7.1 列表去重后实际就是 10 个测试，与 plan 一致。**注**：现有 `tests/test_subscription_cli.py` 已有 21 个测试（TestList 6 + TestAdd 7 + TestRemove 5 + TestSearchByName 3），加 10 后共 31 个，见 Task 3.5 / Task 8.3 预期。 |
| §7.2 API 集成测试 5 条 | Task 6 写了 **7 条**（spec 列 5 条 + 兼容性 1 条 `test_api_add_subscription_without_default_omits_kwarg` + 鉴权 1 条 `test_api_bind_endpoint_no_token_returns_401`）。原 `tests/test_api_subscriptions.py` 已有 12 个，加 7 后共 19 个。 |
| **Web endpoint 路由测试（spec §7 漏列）** | Task 7.5b 补 3 个 `TestEndpointAddRedirect` 测试，覆盖 toast_key 三种分支（added / endpoint_unknown / not_found）。原 `tests/test_web_subscriptions.py` 已有 5 个，加 3 后共 8 个。 |
| §9 验证清单 | Task 8 全部命令齐备 |

### Placeholder scan

- 无 "TBD" / "TODO" / "implement later" / "add appropriate error handling"。
- 每个 Task 都有可直接 paste 的完整 Python 代码。
- 每个 step 都有具体命令 + 预期输出。

### Type consistency

- `add_endpoint_to_subscription(platform, identifier, endpoint_name, path="config/subscriptions.toml") -> tuple[bool, str]` — Task 1 定义，Task 3/5/7 调用签名一致。
- `remove_endpoint_from_subscription` 同上。
- `add_subscription` 新增关键字参数 `default_notify_endpoint: str | None = None` — Task 3 定义，Task 5 调用一致。
- `EndpointBindRequest { endpoint_name: str }` — Task 4 定义，Task 5 路由用 `body: EndpointBindRequest`，Task 6 测试 POST `{"endpoint_name": ...}`。
- toast_key `subscription.endpoint_unknown` — Task 7 路由用，Task 7.5b 测试覆盖三种分支（added / endpoint_unknown / not_found），Task 8 grep 检查。
- 测试 fixture `mock_known_endpoint` 在 Task 1 和 Task 3 各定义一次（独立 class，无共享），签名一致。
- Task 7.5b 的 Web 测试 mock 的是 `web.routes.subscriptions.add_endpoint_to_subscription`（不是 `core.subscription_cli.add_endpoint_to_subscription`），与现有 `test_web_subscriptions.py` 的 `@patch("web.routes.subscriptions.add_subscription")` 模式一致。

---

## 估算总览

| Task | 工作量 | 阻塞关系 |
|------|--------|---------|
| 1. core: add_endpoint_to_subscription | 15 min | — |
| 2. core: remove_endpoint_from_subscription | 10 min | Task 1（共用 fixture） |
| 3. core: add_subscription + 回滚 | 15 min | Task 1（调用）+ Task 2（回滚调 remove） |
| 4. api/schemas.py 扩展 | 5 min | — （可与 Task 1-3 并行） |
| 5. api/routes/subscriptions.py | 10 min | Task 4 + Task 3 完成 |
| 6. API 集成测试 | 20 min | Task 5 完成 |
| 7. web 重构 + toast_key + Web 测试 | 20 min | Task 1 + Task 2 完成 |
| 8. 全量验证 | 10 min | Task 1-7 全部完成 |

**串行总时长：105 min。**
**最优并行（Task 4 与 Task 1-3 并行）：约 95 min。**
