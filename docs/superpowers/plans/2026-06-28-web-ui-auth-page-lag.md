# Plan: Web UI「登录管理」切页卡顿优化

**日期**: 2026-06-28
**范围**: `web/routes/auth.py`、`web/templates/platform_auth.html`、`web/templates/_auth_card.html`、`tests/test_web_auth.py`
**作者**: @explorer (writing-plans 委托)
**状态**: draft（待 @oracle review）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「登录管理」(`/auth`) 切页响应在 200ms 内返回首屏 HTML（骨架），nickname 异步加载、并行拉取，不再阻塞页面切换。

**Architecture:** 后端 `/auth` 路由返回骨架（无 nickname）+ 新增 `/auth/nicknames` 批量 JSON 接口；前端切页立即渲染卡片骨架，DOM ready 后并发 fetch 该接口填充 nickname，单卡加载/失败独立处理。后端把现有的 `asyncio` 串行 `for p in PLATFORM_INFO: await _fetch_nickname(...)` 改成 `asyncio.gather()` 并行；nickname 调用包 `asyncio.wait_for(..., timeout=3)`，单平台超时降级 None，不阻塞其他平台。

**Tech Stack:** Python 3.12 / FastAPI / Jinja2 / 原生 `fetch` (无新前端依赖) / pytest + httpx ASGITransport

---

## 1. 背景与根因

### 1.1 现象

从侧栏点「登录管理」(`/auth`) 切换过去时有 0.5~3s 的明显卡顿，期间浏览器标签卡在原页面，没有立即出现新页面。

### 1.2 根因（已调查确认）

`web/routes/auth.py:104-125` 的 `auth_page()` handler：

```python
@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request) -> HTMLResponse:
    config = await load_config()
    platforms: list[dict[str, Any]] = []
    for p in PLATFORM_INFO:                                        # ← 串行 3 次
        status, expires, has_auth = _get_auth_status(config, p["key"])
        nickname = await _fetch_nickname(config, p["key"]) if has_auth else None  # ← 阻塞远程调用
        platforms.append({...})
    return TEMPLATES.TemplateResponse(...)                          # ← 全部完成后才返回
```

`_fetch_nickname()` (auth.py:69-101) 内部调 `auth.get_user_nickname(tokens)`，三个平台的实现都是**远程 HTTP 调用**：

| 平台 | 实现位置 | 调用 |
|---|---|---|
| bili | `platforms/bilibili/auth.py:219-247` | `bilibili_api.user.User.get_user_info()` → `api.bilibili.com` |
| xhs | `platforms/xiaohongshu/auth.py:388-408` | `AsyncXhsClient.get_self_info()` → `edith.xiaohongshu.com` |
| weibo | `platforms/weibo/auth.py:345-376` | `aiohttp GET https://weibo.com/` HTML 抓 `screen_name` |

三个调用**串行**（`for` 循环 + `await`），且 **没有 timeout**。即使每个 200~500ms，串行也要 0.6~1.5s；任一慢响应（GFW 风控、cookie 失效返回大页面、网络抖动）会直接拉到 3~10s。

已有 `_nickname_cache` (10min TTL) 缓解**重复访问**的痛感，但**冷缓存**首次切页仍然卡。

### 1.3 不是这次要修的（范围之外）

- `_get_auth_status()` 本身只读 config + `time.time()`，没有 IO，不需要动
- `load_config()` 是磁盘读 toml，已经够快（<10ms）
- `/auth/qr`、`/auth/poll`、`/auth/refresh`、`/auth/logout`、`/auth/card/{p}` 这些子路由按需触发，不在切页路径上，不动
- 前端整体框架（HTMX + 原生 JS）保持，不引入 React/SWR 等新依赖

---

## 2. 关键决策（已锁定，理由附后）

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 整体策略 | **后端并行 + 前端骨架**（两者结合） | 仅后端并行把 1.5s 砍到 0.5s 仍卡；仅前端骨架让用户看到卡片但首屏仍要等 0.5s。两者结合：后端 `/auth` 直接返回骨架（<50ms），前端并发拉 nickname（后端 gather 把 wall time 砍到单平台耗时，约 0.3~0.5s），用户视觉上"切页瞬间完成 + nickname 稍后填充" |
| D2 | 后端并行实现 | `asyncio.gather(*[_fetch_nickname(...) for p in platforms], return_exceptions=True)` + 单调用 `asyncio.wait_for(..., timeout=3)` | 3 个平台独立 IO，并行是教科书做法；`wait_for` 防止任一平台慢响应拖累整体；`return_exceptions=True` 防止单平台异常炸掉 gather |
| D3 | 前端骨架如何渲染 nickname | 卡片渲染时 nickname 留空 `<span class="nickname-slot" data-platform="bili">—</span>`；DOM ready 后单次 `fetch('/auth/nicknames')` 拿到 `{bili: "测试UP主", xhs: null, weibo: "用户xxx"}`，遍历 `.nickname-slot` 填充 | 单次批量请求避免 N+1；保持与已有 `_auth_card.html` 模板渲染链路兼容（partial refresh 路径不变） |
| D4 | 新增接口路径 | `GET /auth/nicknames` 返回 `dict[str, str \| None]` | 与已有 `/auth/card/{p}` 单卡 partial 风格一致；JSON 比 HTML 片段更适合"前端填多个 slot"场景 |
| D5 | nickname 拉取失败/超时如何展示 | 显示「—」（与未配置态视觉一致），不弹 toast | nickname 是装饰性信息，不是关键状态；用户已经看到 token 状态卡片，不会因为 nickname 缺失而误判登录状态。已有 `_fetch_nickname` 已经吞异常返回 None，行为一致 |
| D6 | 是否加 SWR-style 后台预热 | **不加**（YAGNI） | 10min TTL 缓存已经覆盖 99% 重复访问；预热需要后端 scheduler，复杂度不匹配收益。如果用户反馈"二次切页仍慢"再加 |
| D7 | `/auth/card/{p}` partial refresh 是否同步改造 | **不改**（partial refresh 走 `_fetch_nickname`，但调用方是 `renewToken`/`logoutPlatform`，用户已经点过按钮，等 0.5s 可接受） | 最小改动；保持本 plan 单一目标（切页体验）。partial refresh 走老路径仍然能拿到 nickname，不破坏现有 UX |
| D8 | 是否动 `_nickname_cache` TTL | **不动** | 现有 600s 已经合理；并行化后冷缓存命中延迟也降到可接受范围 |
| D9 | 是否给现有 3 个 `get_user_nickname` 加客户端 timeout | **不动**（在 `_fetch_nickname` 调用层用 `asyncio.wait_for` 统一加） | 改 3 个平台 authenticator 范围太大、要改它们各自的单元测试；在调用点包 wait_for 是单点收敛，最少改动 |
| D10 | 是否加前端 loading 视觉 | slot 内默认显示「加载中…」（dim 灰色），fetch 完成后替换为 nickname 或「—」 | 让用户知道 nickname 在加载而非异常缺失；与 base.html 已有的 `text-[var(--text-secondary)]` dim 样式一致 |

### 2.1 决策点（需要用户确认的）

**无强决策点** — 所有选择都是基于根因 + 最小改动原则推导的。但有一个**可选增强**，用户可选择是否纳入本 plan：

**Q1**: 是否在本 plan 内同时给 `/auth` handler 加**服务端 access log + 耗时统计**（`logger.info("🔑 /auth rendered in %.0fms", elapsed_ms)`）？

- 选项 A: **纳入**（多 1 个 task，~5 行代码）— 便于未来排查类似卡顿
- 选项 B: **不纳入** — YAGNI，等真有性能问题再加

**推荐**: A（成本极低，长期收益明确）

---

## 3. 文件清单（增/改/删行数估计）

| 文件 | 操作 | 改动 | 估计行数 |
|---|---|---|---|
| `web/routes/auth.py` | 改 | (1) `auth_page()` 删除 nickname 拉取，返回骨架；(2) 新增 `/auth/nicknames` 路由 + `_fetch_all_nicknames()` gather 辅助；(3) `_fetch_nickname` 包 `wait_for(timeout=3)` + `finally` 内 close 再包 `wait_for(timeout=2)`；(4) `/auth` 加耗时日志（决策 A 时） | -8 / +40 |
| `web/templates/platform_auth.html` | 改 | 末尾 `<script>` 加 nickname slot 填充逻辑（提取为 `fillNicknameSlots()` 具名函数，只填占位 slot，partial refresh 已填的跳过） | +35 |
| `web/templates/_auth_card.html` | 改 | nickname 行改成 slot：未配置时不渲染 slot（与现 has_auth 一致），有 auth 时 `<span class="nickname-slot" data-platform="{{ p.key }}">{{ p.nickname or '加载中…' }}</span>`（slot 内优先服务端 nickname，无则显示「加载中…」） | -3 / +5 |
| `tests/test_web_auth.py` | 改 | (1) 现有 4 个 nickname 测试改为：先 GET `/auth` 拿骨架（不含 nickname），再 GET `/auth/nicknames` 验证返回；(2) 新增并行性测试；(3) 新增超时降级测试（AC4：bili 卡 10s 整体 <4s）；(4) 新增失败降级测试（RuntimeError → None）；(5) 新增 TTL 缓存命中测试；(6) 新增 `/auth/nicknames` 端到端测试；(7) Task 1 timeout 测试加 wall time 断言 | -30 / +160 |

总计：~200 行净改动。

---

## 4. 任务分解（TDD，每个 task 自包含可提交）

### Task 1: 后端 — `_fetch_nickname` 加 timeout + 异常收敛

**Files:**
- Modify: `web/routes/auth.py:69-101` (`_fetch_nickname`)
- Test: `tests/test_web_auth.py` (新增 `TestFetchNicknameTimeout`)

**目的**: 把"远程慢响应阻塞整个 `/auth`"独立修掉。即使后续 task 不做，这一步也能把单平台 30s 慢响应压到 3s。

- [ ] **Step 1: 写失败测试 — 验证 `_fetch_nickname` 在 authenticator 卡 10s 时 3s 内返回 None**

追加到 `tests/test_web_auth.py` 末尾：

```python
class TestFetchNicknameTimeout:
    """验证 _fetch_nickname 有 timeout 保护，慢 authenticator 不会阻塞调用方。"""

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_fetch_nickname_returns_none_on_slow_authenticator(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        import asyncio as _asyncio
        import time as _time

        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        # bili 登录有效
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.bilibili.auth.sessdata = "fake"
        mock_load.return_value.bilibili.auth.bili_jct = "fake"
        mock_load.return_value.bilibili.auth.dedeuserid = "12345"
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

        async def slow_nickname(_tokens):  # noqa: ANN001
            await _asyncio.sleep(10.0)
            return "should_not_reach"

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = slow_nickname
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        from web.routes.auth import _fetch_nickname

        # 用 asyncio.wait_for 包外层超时保护，防止测试本身卡 10s。
        # 外层 4.0s vs 内部 nickname timeout 3.0s + close timeout 2.0s：
        # 最坏路径 nickname 先 3s 超时返回 None，然后 close 走快速 AsyncMock 立即返回，
        # 总耗时约 3.0s；外层 4.0s 给 1s 余量。
        t0 = _time.monotonic()
        nick = await _asyncio.wait_for(_fetch_nickname(mock_load.return_value, "bili"), timeout=4.0)
        elapsed = _time.monotonic() - t0
        assert nick is None  # 内部 timeout=3 触发 → 返回 None
        # wall time 应在 [2.9, 3.5]：略大于 3s（timeout 触发 + close AsyncMock 开销）
        assert 2.9 <= elapsed <= 3.5, f"timeout 未在预期区间触发，实际 {elapsed:.2f}s"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/test_web_auth.py::TestFetchNicknameTimeout -xvs
```

预期：FAIL（测试会在 4s 后超时或断言失败，因为当前 `_fetch_nickname` 没有 timeout，会等 authenticator 的 10s）。**注意**：测试本身有 4s 外层保护，CI 上若 4s 不够可调到 6s（但 wall time 断言区间 [2.9, 3.5] 是基于内部 3s timeout，调外层不影响该断言）。

- [ ] **Step 3: 修改 `_fetch_nickname`，加 `asyncio.wait_for(..., timeout=3)` + 异常收敛**

`web/routes/auth.py`，把 `_fetch_nickname` 改为：

```python
import asyncio  # 加到文件顶部 import 区（在 `import io` 之后）

# 模块顶部常量区加：
_NICKNAME_FETCH_TIMEOUT_SECONDS = 3.0


async def _fetch_nickname(config: Config, platform_key: str) -> str | None:
    """获取带 TTL 缓存的账号昵称；失败/未登录/超时返回 None。

    不会抛异常——调用方用 None 表示"显示 —"，不影响 status 渲染。
    单次远程调用超过 _NICKNAME_FETCH_TIMEOUT_SECONDS 自动取消并降级 None，
    防止慢响应/网络抖动阻塞整个页面渲染。
    """
    cached = _nickname_cache.get(platform_key)
    if cached is not None and (time.time() - cached[1]) < _NICKNAME_TTL_SECONDS:
        return cached[0]

    tokens = _build_tokens_from_config(platform_key, config)
    if tokens is None:
        _nickname_cache[platform_key] = (None, time.time())
        return None

    auth = get_authenticator(platform_key)
    try:
        try:
            nick = await asyncio.wait_for(
                auth.get_user_nickname(tokens),
                timeout=_NICKNAME_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("🔑 %s nickname 获取超时 (%.0fs)", platform_key, _NICKNAME_FETCH_TIMEOUT_SECONDS)
            nick = None
        except Exception as exc:
            logger.warning("🔑 %s nickname 获取异常: %s", platform_key, exc)
            nick = None
        _nickname_cache[platform_key] = (nick, time.time())
        return nick
    finally:
        # close 本身也可能阻塞（cancel 时底层 socket 正在 read/write，
        # 尤其 weibo 的 aiohttp session），再包一层 2s 超时。
        try:
            await asyncio.wait_for(auth.close(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("🔑 %s 关闭 authenticator 超时 (2s)", platform_key)
        except Exception as exc:
            logger.warning("🔑 %s 关闭 authenticator 失败: %s", platform_key, exc)
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
uv run pytest tests/test_web_auth.py::TestFetchNicknameTimeout -xvs
```

预期：PASS（3s 后返回 None，测试在 4s 内完成；wall time 断言 [2.9, 3.5] 满足）。

- [ ] **Step 5: 运行整个 test_web_auth.py，确认没破坏现有测试**

```bash
uv run pytest tests/test_web_auth.py -xvs
```

预期：全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add web/routes/auth.py tests/test_web_auth.py
git commit -m "perf(auth): 给 _fetch_nickname 加 3s timeout，防止慢平台阻塞页面"
```

---

### Task 2: 后端 — 新增 `/auth/nicknames` 批量并行接口

**Files:**
- Modify: `web/routes/auth.py` (新增 `_fetch_all_nicknames()` 辅助 + `/auth/nicknames` 路由)
- Test: `tests/test_web_auth.py` (新增 `TestAuthNicknamesEndpoint`)

**目的**: 提供前端骨架填充所需的批量并行接口。这一步独立于 `/auth` 路由改造，可以单独测试。

- [ ] **Step 1: 写失败测试 — `/auth/nicknames` 返回三个平台的 nickname 字典**

追加到 `tests/test_web_auth.py`：

```python
class TestAuthNicknamesEndpoint:
    """验证 GET /auth/nicknames 批量并行返回 nickname。"""

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_returns_dict_for_all_platforms(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        # 三个平台都登录
        for section, attr in [("bilibili", "bilibili"), ("xiaohongshu", "xiaohongshu"), ("weibo", "weibo")]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"  # bili 需要
            cfg_section.auth.cookie = "SUB=fake"  # weibo 需要

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(side_effect=lambda _t: {"bili": "B站UP", "xhs": "小红书博主", "weibo": "微博用户"}[_t.platform])
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth/nicknames")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"bili", "xhs", "weibo"}
        assert data["bili"] == "B站UP"
        assert data["xhs"] == "小红书博主"
        assert data["weibo"] == "微博用户"

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_parallel_not_sequential(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """三个平台每个 sleep 0.3s，并行应在 <0.7s 完成（串行需 0.9s+）。"""
        import asyncio as _asyncio
        import time as _time

        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        async def slow_nick(_t):  # noqa: ANN001
            await _asyncio.sleep(0.3)
            return "name"

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = slow_nick
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        start = _time.monotonic()
        resp = await client.get("/auth/nicknames")
        elapsed = _time.monotonic() - start

        assert resp.status_code == 200
        # 并行：~0.3s + overhead；串行会 0.9s+。给 0.7s 上限留 buffer。
        assert elapsed < 0.7, f"并行未生效，耗时 {elapsed:.2f}s"

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_unconfigured_platform_returns_none(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        # 全部未配置
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0
        mock_get_auth.return_value = MagicMock()

        resp = await client.get("/auth/nicknames")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"bili": None, "xhs": None, "weibo": None}

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_slow_platform_does_not_block_others(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """AC4：bili 卡 10s 时 /auth/nicknames 仍应在 4s 内返回（其他平台正常值）。

        bili 走 wait_for 内部 3s timeout 降级 None；xhs/weibo 立即返回。
        整体 wall time < 4s（= 3s timeout + ~1s overhead buffer）。
        """
        import asyncio as _asyncio
        import time as _time

        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        # 每个平台返回不同 nickname，便于断言哪个被降级
        async def bili_slow(_t):  # noqa: ANN001
            await _asyncio.sleep(10.0)
            return "should_not_reach"

        async def xhs_fast(_t):  # noqa: ANN001
            return "小红书博主"

        async def weibo_fast(_t):  # noqa: ANN001
            return "微博用户"

        # get_authenticator 返回的 mock_auth 需按 platform 区分实现；
        # 用 side_effect 按 tokens.platform 路由
        async def nickname_dispatch(_t):  # noqa: ANN001
            return {
                "bili": await bili_slow(_t),
                "xhs": await xhs_fast(_t),
                "weibo": await weibo_fast(_t),
            }[_t.platform]

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = nickname_dispatch
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        start = _time.monotonic()
        resp = await client.get("/auth/nicknames")
        elapsed = _time.monotonic() - start

        assert resp.status_code == 200
        # AC4：bili 卡 10s 时整体仍 < 4s
        assert elapsed < 4.0, f"慢平台未隔离，耗时 {elapsed:.2f}s"
        data = resp.json()
        # bili 被 wait_for 3s timeout 降级 None；其他正常
        assert data == {"bili": None, "xhs": "小红书博主", "weibo": "微博用户"}

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_returns_none_on_runtime_error(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """覆盖原 test_auth_page_nickname_failure_falls_back_gracefully 契约：
        authenticator.get_user_nickname 抛 RuntimeError 时，/auth/nicknames
        对该平台返回 None（不影响其他平台）。
        """
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        async def nickname_dispatch(_t):  # noqa: ANN001
            if _t.platform == "bili":
                raise RuntimeError("bili authenticator exploded")
            return {"xhs": "小红书博主", "weibo": "微博用户"}[_t.platform]

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = nickname_dispatch
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth/nicknames")
        assert resp.status_code == 200
        data = resp.json()
        # bili 异常被 _fetch_nickname 吞掉降级 None；其他正常
        assert data == {"bili": None, "xhs": "小红书博主", "weibo": "微博用户"}

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_nicknames_endpoint_caches_within_ttl(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """覆盖原 test_auth_page_nickname_cached_within_ttl 契约：
        TTL 窗口内二次调用 /auth/nicknames 时，get_user_nickname 只调一次（缓存命中）。
        """
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        for attr in ["bilibili", "xiaohongshu", "weibo"]:
            cfg_section = getattr(mock_load.return_value, attr)
            cfg_section.auth.expires_at = 9999999999.0
            cfg_section.auth.sessdata = "fake"
            cfg_section.auth.dedeuserid = "1"
            cfg_section.auth.cookie = "SUB=fake"

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="缓存测试")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        # 第一次：拉取并写缓存
        resp1 = await client.get("/auth/nicknames")
        assert resp1.status_code == 200
        assert resp1.json() == {"bili": "缓存测试", "xhs": "缓存测试", "weibo": "缓存测试"}

        first_call_count = mock_auth.get_user_nickname.await_count
        assert first_call_count == 3  # 三平台各拉一次（并行）

        # 第二次：TTL 窗口内，应命中缓存，不再调 authenticator
        resp2 = await client.get("/auth/nicknames")
        assert resp2.status_code == 200
        assert resp2.json() == {"bili": "缓存测试", "xhs": "缓存测试", "weibo": "缓存测试"}
        assert mock_auth.get_user_nickname.await_count == first_call_count  # 无新增调用
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
uv run pytest tests/test_web_auth.py::TestAuthNicknamesEndpoint -xvs
```

预期：6 个测试全 FAIL（404，路由不存在）。

- [ ] **Step 3: 实现 `_fetch_all_nicknames()` + `/auth/nicknames` 路由**

在 `web/routes/auth.py` 的 `auth_page` 路由之前（紧接 `_fetch_nickname` 之后），加：

```python
async def _fetch_all_nicknames(config: Config) -> dict[str, str | None]:
    """并行拉取所有已配置平台的 nickname。

    未配置或拉取失败/超时的平台值为 None。
    用 asyncio.gather(return_exceptions=True) 保证单平台异常不影响其他平台。
    """
    tasks = []
    keys = []
    for p in PLATFORM_INFO:
        section, _ = CONFIG_AUTH_KEYS[p["key"]]
        has_auth = getattr(config, section).auth.expires_at > 0
        if has_auth:
            tasks.append(_fetch_nickname(config, p["key"]))
            keys.append(p["key"])
        # 未配置的不进 tasks（_fetch_nickname 内部也会缓存 None，但跳过更干净）

    if not tasks:
        return {p["key"]: None for p in PLATFORM_INFO}

    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, str | None] = {p["key"]: None for p in PLATFORM_INFO}
    for key, result in zip(keys, results, strict=False):
        if isinstance(result, Exception):
            logger.warning("🔑 %s nickname gather 异常: %s", key, result)
            out[key] = None
        else:
            out[key] = result
    return out


@router.get("/auth/nicknames")
async def auth_nicknames(request: Request) -> dict[str, str | None]:  # noqa: ARG001
    """批量返回所有平台 nickname，供前端骨架填充。

    并行拉取，单平台 3s timeout。返回格式：{"bili": "UP名" | None, "xhs": ..., "weibo": ...}
    """
    config = await load_config()
    return await _fetch_all_nicknames(config)
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
uv run pytest tests/test_web_auth.py::TestAuthNicknamesEndpoint -xvs
```

预期：6 个全 PASS。

- [ ] **Step 5: 运行整个 test_web_auth.py**

```bash
uv run pytest tests/test_web_auth.py -xvs
```

预期：全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add web/routes/auth.py tests/test_web_auth.py
git commit -m "feat(auth): 新增 /auth/nicknames 并行批量接口"
```

---

### Task 3: 后端 — `/auth` 路由返回骨架（移除内联 nickname 拉取）

**Files:**
- Modify: `web/routes/auth.py:104-125` (`auth_page`)
- Test: `tests/test_web_auth.py` (改现有 4 个 nickname 测试)

**目的**: 把 `/auth` 的 wall time 从"3 平台串行 nickname 拉取"降到"仅 config 读取"（<50ms）。nickname 改由前端 Task 4 异步拉取。

- [ ] **Step 1: 改写现有 4 个 nickname 测试为"骨架 + 异步"契约**

这 4 个测试目前断言 `resp.text` 直接含 nickname。新契约：`/auth` 返回骨架不含 nickname，nickname 由 `/auth/nicknames` 提供（已在 Task 2 覆盖）。改写如下：

把 `tests/test_web_auth.py` 中的 `class TestAuthNickname` 整体替换为：

```python
class TestAuthNickname:
    """验证 /auth 页面骨架渲染不再阻塞于 nickname 拉取。

    新契约（D1+D3）：
    - GET /auth 不再调用任何 authenticator，nickname 字段恒为 None
    - nickname 由前端通过 GET /auth/nicknames 异步拉取（见 TestAuthNicknamesEndpoint）
    """

    def _configure_logged_in_bili(self, mock_load: AsyncMock) -> None:
        mock_load.return_value.bilibili.auth.expires_at = 9999999999.0
        mock_load.return_value.bilibili.auth.sessdata = "fake_sess"
        mock_load.return_value.bilibili.auth.bili_jct = "fake_jct"
        mock_load.return_value.bilibili.auth.dedeuserid = "12345"
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

    @patch("web.routes.auth.get_authenticator")
    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_does_not_call_authenticator(
        self,
        mock_load: AsyncMock,
        mock_get_auth: MagicMock,
        client: AsyncClient,
    ) -> None:
        """骨架路由必须不触发任何 authenticator.get_user_nickname 调用。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        self._configure_logged_in_bili(mock_load)

        mock_auth = MagicMock()
        mock_auth.get_user_nickname = AsyncMock(return_value="测试UP主")
        mock_auth.close = AsyncMock()
        mock_get_auth.return_value = mock_auth

        resp = await client.get("/auth")
        assert resp.status_code == 200
        # 骨架不拉 nickname
        assert mock_auth.get_user_nickname.await_count == 0
        # 但登录卡片的 nickname slot 应该存在（供前端填充）
        assert "nickname-slot" in resp.text or "加载中" in resp.text

    @patch("web.routes.auth.load_config", new_callable=AsyncMock)
    async def test_auth_page_unconfigured_has_no_nickname_slot(
        self,
        mock_load: AsyncMock,
        client: AsyncClient,
    ) -> None:
        """未配置平台不应渲染 nickname slot（与 _auth_card.html has_auth 一致）。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache.clear()
        mock_load.return_value.bilibili.auth.expires_at = 0.0
        mock_load.return_value.xiaohongshu.auth.expires_at = 0.0
        mock_load.return_value.weibo.auth.expires_at = 0.0

        resp = await client.get("/auth")
        assert resp.status_code == 200
        assert "账号:" not in resp.text  # 未配置不显示账号行

    @patch("web.routes.auth.clear_auth_section", new_callable=AsyncMock)
    async def test_auth_logout_clears_nickname_cache(
        self,
        mock_clear: AsyncMock,
        client: AsyncClient,
    ) -> None:
        """登出仍应清 nickname 缓存（避免下次拉到旧账号名）。"""
        from web.routes.auth import _nickname_cache

        _nickname_cache["bili"] = ("测试UP主", 0.0)
        mock_clear.return_value = True

        resp = await client.post(
            "/auth/logout/bili",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "message": "已注销"}
        assert "bili" not in _nickname_cache
```

注意：原 `test_auth_page_nickname_failure_falls_back_gracefully` 和 `test_auth_page_nickname_cached_within_ttl` 删除 —— 这些契约转移到 `TestAuthNicknamesEndpoint`：
- **失败降级** → `test_nicknames_endpoint_returns_none_on_runtime_error`（RuntimeError 场景）
- **TTL 缓存命中** → `test_nicknames_endpoint_caches_within_ttl`（二次调用只调一次 authenticator）

加上 `test_nicknames_endpoint_slow_platform_does_not_block_others`（AC4），`TestAuthNicknamesEndpoint` 共 6 个测试，**测试覆盖无净丢失**（删 2 加 3+1）。

- [ ] **Step 2: 运行测试，确认失败（断言改变 + nickname-slot 还没加）**

```bash
uv run pytest tests/test_web_auth.py::TestAuthNickname -xvs
```

预期：FAIL（`get_user_nickname.await_count == 0` 失败，因为旧 `auth_page` 仍调用；`nickname-slot` 不在 HTML）。

- [ ] **Step 3: 改 `auth_page` 路由，移除 nickname 拉取**

`web/routes/auth.py`，把 `auth_page` 改为：

```python
@router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request) -> HTMLResponse:
    """Login management page.

    返回骨架：仅渲染 token 状态卡片，不在此处拉 nickname。
    nickname 由前端通过 GET /auth/nicknames 异步拉取并填充 slot，
    避免远程调用阻塞页面切换。
    """
    t0 = time.monotonic()
    config = await load_config()
    platforms: list[dict[str, Any]] = []
    for p in PLATFORM_INFO:
        status, expires, has_auth = _get_auth_status(config, p["key"])
        platforms.append(
            {
                **p,
                "token_status": status,
                "expires": expires,
                "has_auth": has_auth,
                "nickname": None,  # 骨架：由前端填充
            }
        )
    elapsed_ms = (time.monotonic() - t0) * 1000
    logger.info("🔑 /auth 骨架渲染 %.0fms", elapsed_ms)
    return TEMPLATES.TemplateResponse(
        request,
        "platform_auth.html",
        {"active_nav": "auth", "platforms": platforms},
    )
```

（耗时日志对应决策 Q1 选项 A；若用户选 B，删除 `t0` / `elapsed_ms` / `logger.info` 三行。模块顶部已 `import time`（auth.py:5），**不要**在函数内重复 `import time as _time`。）

- [ ] **Step 4: 改 `_auth_card.html` 模板，把 nickname 行改成 slot**

`web/templates/_auth_card.html`，把第 18-20 行：

```html
  {% if p.nickname %}
  <div class="text-xs text-[var(--text-secondary)] mb-1">账号: <span class="font-medium text-[var(--text-primary)]">{{ p.nickname }}</span></div>
  {% endif %}
```

改为：

```html
  {% if p.has_auth %}
  <div class="text-xs text-[var(--text-secondary)] mb-1">账号: <span class="font-medium text-[var(--text-primary)] nickname-slot" data-platform="{{ p.key }}">{{ p.nickname or '加载中…' }}</span></div>
  {% endif %}
```

**关键说明**：
- 用 `has_auth` 而非 `nickname`，因为骨架期 nickname 恒为 None；登录卡片就该有 slot。
- slot 内 **优先用服务端 `p.nickname`**，只在没有时显示「加载中…」。
  - 整页 `/auth`（骨架）传 `p.nickname=None` → slot 显示「加载中…」，等前端 Task 4 异步拉取填充。
  - partial refresh `/auth/card/{p}`（续期/注销后）传 `p.nickname=<真实>` → slot **直接显示真实 nickname**，无需二次 fetch，避免「加载中…」永远停留（见风险 R6 修正后行为）。

- [ ] **Step 5: 运行测试，确认通过**

```bash
uv run pytest tests/test_web_auth.py::TestAuthNickname -xvs
```

预期：3 个测试全 PASS。

- [ ] **Step 6: 运行整个 test_web_auth.py**

```bash
uv run pytest tests/test_web_auth.py -xvs
```

预期：全部 PASS（包括 Task 1/2 的新测试）。

- [ ] **Step 7: 手动启动 web 确认骨架能渲染**

```bash
uv run python run_web.py
```

浏览器开 `http://localhost:8080/auth`，确认：
1. 页面秒开（不再卡）
2. 卡片显示「账号: 加载中…」
3. 几秒后变成实际账号名或「—」（需要前端 Task 4 才会填充，此时应保持「加载中…」）

注意：此步骤仅验证骨架渲染不报错；nickname 填充在 Task 4 完成后才生效。如果手动测试不方便，可跳过此步，依赖 Task 4 完成后整体手测。

- [ ] **Step 8: Commit**

```bash
git add web/routes/auth.py web/templates/_auth_card.html tests/test_web_auth.py
git commit -m "perf(auth): /auth 改为骨架渲染，移除内联 nickname 阻塞调用"
```

---

### Task 4: 前端 — 骨架加载后并发 fetch `/auth/nicknames` 填充 slot

**Files:**
- Modify: `web/templates/platform_auth.html:45-46` (`<script>` 块开头加 nickname 填充)

**目的**: 闭环用户体验 —— 切页瞬间看到卡片骨架，nickname 在后台并行拉完后平滑填充。这一步无需单测（纯前端 JS，没有前端测试基建；用 Task 5 的手动验证 + 现有后端测试覆盖 API 契约）。

- [ ] **Step 1: 在 `platform_auth.html` 的 `<script>` 块开头（`var qrPollInterval = null;` 之前）加 nickname 填充逻辑**

`web/templates/platform_auth.html`，把：

```javascript
<script>
  // Override base.html placeholders with real implementation
  var qrPollInterval = null;
```

改为：

```javascript
<script>
  // ── Nickname 异步填充（骨架渲染后并发拉取）──────────────────
  // /auth 返回的骨架中 nickname 恒为空，slot 显示「加载中…」。
  // DOM ready 后单次批量请求 /auth/nicknames（后端并行拉取 3 平台），
  // 拿到结果后遍历 .nickname-slot 填充。
  //
  // ⚠️ slot 渲染规则（_auth_card.html）：`{{ p.nickname or '加载中…' }}`
  //   - 整页 /auth（骨架）：p.nickname=None → 显示「加载中…」→ 下面 fetch 填充
  //   - partial refresh /auth/card/{p}：p.nickname=<真实> → 直接显示真实 nickname
  //     此时 slot.textContent 已是真实值，fillNicknameSlots() 不会覆盖
  //     （仅当 slot 仍为「加载中…」占位时才填，避免破坏 partial refresh 路径）
  function fillNicknameSlots() {
    var slots = document.querySelectorAll('.nickname-slot');
    if (slots.length === 0) return;  // 无登录平台，无需请求

    // 只处理仍是占位「加载中…」的 slot（partial refresh 已填好的跳过）
    var pending = [];
    slots.forEach(function(slot) {
      if (slot.textContent.trim() === '加载中…') pending.push(slot);
    });
    if (pending.length === 0) return;

    fetch('/auth/nicknames', { headers: { 'Accept': 'application/json' } })
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(data) {
        pending.forEach(function(slot) {
          var platform = slot.getAttribute('data-platform');
          var nick = data[platform];
          slot.textContent = nick || '—';
        });
      })
      .catch(function(err) {
        // 整体失败（网络错误）—— 降级显示「—」，不弹 toast（nickname 是装饰信息）
        console.warn('nickname fetch failed', err);
        pending.forEach(function(slot) { slot.textContent = '—'; });
      });
  }

  // 整页 /auth 首次加载：DOM ready 后填充骨架 slot
  fillNicknameSlots();

  // Override base.html placeholders with real implementation
  var qrPollInterval = null;
```

**关键设计**：
- `fillNicknameSlots()` 提取为具名函数（不再是 IIFE），可被复用。
- 函数内**只填充仍是「加载中…」占位的 slot** —— partial refresh（`refreshAuthCard`）替换卡片后，新 slot 的 textContent 已是服务端渲染的真实 nickname（见 Task 3 Step 4 模板 `{{ p.nickname or '加载中…' }}`），不会被本函数误覆盖，也**无需二次 fetch**（与 Issue 1 修复配套）。
- 极端边角：若 partial refresh 返回 `has_auth=True` 但 `nickname=None`（如 nickname 拉取超时降级），slot 会显示「加载中…」并永远停留 —— 此场景下用户刚操作完（续期/登出），可接受保持「加载中…」或下次切页时刷新；不为此加复杂二次 fetch（YAGNI，且 partial refresh 路径不在切页性能目标内）。

- [ ] **Step 2: 手动启动 web，端到端验证**

```bash
uv run python run_web.py
```

浏览器验证（参考 Task 5 验收清单）：
1. 从其他页面（如 `/`）点「登录管理」→ **瞬间**切过去（不再卡）
2. 卡片立即显示「账号: 加载中…」（dim 灰色）
3. 0.3~1s 后 nickname 出现，或显示「—」（未配置/失败）
4. 浏览器 DevTools Network 面板看到 `GET /auth`（<50ms）+ `GET /auth/nicknames`（0.3~1s）两个独立请求
5. 多次切换页面验证：10 分钟内 nickname 直接出现（缓存命中），10 分钟后再次走「加载中→填充」流程

- [ ] **Step 3: Commit**

```bash
git add web/templates/platform_auth.html
git commit -m "feat(auth): 前端骨架加载后异步填充 nickname"
```

---

### Task 5: 整体验收 + lint/type check

**Files:** 无（验证 task）

- [ ] **Step 1: 跑全套检查**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -x
```

预期：全绿。**注意 pyright 命令无参数**（见 AGENTS.md gotcha）。

- [ ] **Step 2: 端到端手测（参考 Task 4 Step 2 清单）**

确认 5 条验收点全过。

- [ ] **Step 3: 部署到测试环境验证（可选）**

```bash
# 如果有 staging 环境
ssh usa 'docker logs trawler --since 2m 2>&1 | grep "/auth"'
```

确认 `/auth` 请求日志显示 `🔑 /auth 骨架渲染 XXms`（应在 50ms 以内）。

- [ ] **Step 4: 最终 commit（如有 lint 修复）**

```bash
git status  # 确认无残留改动
```

---

## 5. 验收标准（Definition of Done）

| # | 标准 | 验证方式 |
|---|---|---|
| AC1 | 从任意页面切换到 `/auth` 的**首屏 HTML 响应**在 100ms 内返回 | 浏览器 DevTools Network 或 `curl -w "%{time_total}"` |
| AC2 | `/auth` 响应 HTML 不再包含具体 nickname（只有「加载中…」slot） | `curl /auth \| grep -c 加载中` ≥ 已配置平台数 |
| AC3 | `/auth/nicknames` 在 3 个平台都登录时 <700ms 返回（并行） | Task 2 Step 1 的并行性测试 |
| AC4 | 任一平台 `get_user_nickname` 卡 10s 时，`/auth/nicknames` 仍在 4s 内返回（其他平台正常值，卡住的平台为 None） | Task 1 测试 + Task 2 测试组合 |
| AC5 | `uv run pytest -x` 全绿 | Task 5 Step 1 |
| AC6 | `uv run ruff check .` + `uv run pyright` 无新增问题 | Task 5 Step 1 |
| AC7 | 手动切换页面无卡顿，nickname 平滑填充 | Task 4 Step 2 |

---

## 6. 风险与缓解

| # | 风险 | 缓解 |
|---|---|---|
| R1 | `asyncio.wait_for` 取消 authenticator 调用时，底层 HTTP session 可能未干净关闭；且 `auth.close()` 本身在 cancel 后也可能阻塞（socket 正在 read/write，weibo aiohttp 尤甚） | `_fetch_nickname` 的 `finally` 用 `asyncio.wait_for(auth.close(), timeout=2.0)` 二次超时保护，close 超时/异常均吞掉只记 warning。最坏情况：单平台总耗时 = 3s（nickname timeout）+ 2s（close timeout）= 5s，仍远好于现状的 10s+。Task 1 测试验证返回 None 而非挂起 |
| R2 | 前端 JS 在 `/auth` 是 HTMX 局部替换场景下不执行（如果未来 `/auth` 改成 HTMX 局部刷新） | 当前 `/auth` 是整页跳转（侧栏 `<a href="/auth">`），JS 一定执行。如未来改 HTMX，需把 `fillNicknameSlots()` 改成 HTMX 事件钩子。本 plan 不预防（YAGNI） |
| R3 | `/auth/nicknames` 与 `/auth` 之间有竞态：用户切走时 nickname 请求仍在飞 | 无害 —— fetch 结果丢弃（slot 已不在 DOM）；不会污染其他页面状态 |
| R4 | `platform_auth.html` 末尾的 `<script>` 是页面级，重复 include 会重复执行 | `fillNicknameSlots()` 每个页面只调用一次（整页加载时）；且函数内只填充「加载中…」占位 slot，重复调用幂等。模板本身不会被 partial include（partial 是 `_auth_card.html`） |
| R5 | bili authenticator 的 `bilibili_api.user.User` 内部可能有自己的 timeout，与我们加的 3s 冲突 | 我们的 `wait_for` 是更外层，先触发就先取消；bili 内部 timeout 若 <3s 会先返回，不冲突。Task 1 测试已覆盖"authenticator 卡 10s 我们 3s 兜底" |
| R6 | `_auth_card.html` 改 `{% if p.nickname %}` → `{% if p.has_auth %}` 后，slot 写死「加载中…」会导致 partial refresh（`/auth/card/{p}`，仍传 `nickname=<真实>`）后永远显示「加载中…」 | **已修复** —— slot 内用 `{{ p.nickname or '加载中…' }}`：partial refresh 传真实 nickname 时直接显示，整页 `/auth` 骨架期 nickname=None 时显示「加载中…」等前端填。两条路径都不需要二次 fetch。`fillNicknameSlots()` 还会跳过已是非占位的 slot，避免误覆盖。详见 Task 3 Step 4 + Task 4 Step 1 |
| R7 | Task 1 测试用 `asyncio.wait_for` 包外层 4s 保护，CI 慢机器上可能误判；wall time 断言区间 [2.9, 3.5] 也可能因 CI 调度抖动超出 | 外层 4s vs 内部 3s + close 快速返回，约 1s 余量。wall time 下限 2.9s 防止"timeout 没触发"误 PASS；上限 3.5s 给 0.5s 调度抖动。若 CI 仍 flaky，可放宽上限到 3.8s 或去掉 wall time 断言只保留 `nick is None`。注释已写明 |

---

## 7. 后续可选清理（不在本 plan 范围）

- `/auth/card/{p}` partial refresh 也可改成"返回 slot 骨架 + 前端再 fetch 单平台 nickname"，统一所有 nickname 渲染路径（当前为了最小改动不动）
- 把 `_nickname_cache` 升级为带 LRU + 主动失效（token 续期/登出时已清，足够）
- 给前端加 SWR-style 后台预热（决策 D6 已说明 YAGNI）
- `/auth/nicknames` 加 ETag/304 支持进一步降带宽（当前 JSON < 100B，收益微小）
