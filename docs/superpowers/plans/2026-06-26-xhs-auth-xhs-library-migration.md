# XHS Auth 迁移到 ReaJason/xhs 库 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 XHS QR 扫码登录 471 失败的根因(字段名 `codeStatus`→`code_status` + 缺 gid cookie + sec cookies 瞎猜),通过把 auth 层迁到 ReaJason/xhs 库(已真实抓包验证 + 自动补 gid)一次解决。不动 monitor/comments/search/downloader/client.py 主体。

**Architecture:** 三层独立改动 + 一层验证。Phase 1 装 xhs 依赖 + 建 `AsyncXhsClient` 异步包装;Phase 2 重写 `XhsAuthenticator`;Phase 3 删 client.py 三只废方法 + 清测试;Phase 4 全量验证 + 真机扫码。每 Phase 必须先 ruff/pyright/pytest 全过才进下一 Phase。

**Tech Stack:** Python 3.14(注意 pyproject 是 py314,虽然 AGENTS.md 说 3.12,以 pyproject 为准),`asyncio.to_thread` 包同步 xhs 库,requests+xhs+signer 三方并存。

**Spec 来源:** `docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md` (Approved, 所有决策已定)

---

## 关键约束(执行前必读)

- **不动** `client.py` 主体 — 只删 Phase 3 明确列出的 3 个方法(create_qrcode / check_qrcode_status / fetch_sec_cookies)
- **不动** `signer.py` (monitor 还在用)
- **不动** `monitor.py` / `comments.py` / `search.py` / `downloader.py`
- **不动** `web/routes/auth.py` (调用方契约:`poll_qr_status` 返回 `AuthStatus`,`get_tokens` 返回 `PlatformTokens`,接口不变)
- **不动** `web/app.py` / `web/logging_bridge.py`
- **不动** `shared/auth/base.py` (`BaseAuthenticator` 协议不变)
- **不动** `shared/exceptions.py` (现有 4 个异常类复用)
- 每个 Phase 结束必须 `ruff check` + `pyright` + `pytest` 全过才能进下一 Phase
- **当前分支**: `fix/xhs-qr-poll-missing-a1` (已含 spec commit `8dbbe03`,基于 master)
- **pytest asyncio 模式**: `pyproject.toml` 设 `asyncio_mode = "auto"`,所有 `async def test_*` 自动作为 asyncio 测试,**不需要** `@pytest.mark.asyncio` 装饰器。新写测试一律省略此标记。现有 `test_xhs_authenticator.py` 里若保留了标记可不动(冗余但不报错),但新写的 `test_async_xhs_wrapper.py` 和重写的 `test_xhs_authenticator.py` 测试**一律不加**。
- 已确认的 xhs 库 API(ReaJason/xhs master,2026-06 拉取):
  - `XhsClient(cookie=None, user_agent=None, timeout=10, proxies=None, sign=None)` — `sign=None` 时用内置 `sign()`
  - `get_qrcode()` / `check_qrcode(qr_id, code)` / `activate()` / `get_self_info()` 都无额外参数
  - `cookie` property — getter 返回 `"k1=v1;k2=v2"`,setter 接受 str
  - **`XhsClient` 没有 `close()` 方法** — wrapper 自己实现。`session` attribute 是否 public 待 P1-1b 硬性核实(getattr 兜底,详见 P1-1b 脚本)
  - 异常全在 `xhs.exception` 模块:`NeedVerifyError` / `IPBlockError` / `SignError` / `DataFetchError`,都继承 `requests.RequestException`
  - `check_qrcode` 返回字段是 **`code_status` (snake_case)**(spec 根因 #1)
  - `get_qrcode` / `check_qrcode` 返回 dict 的**完整 key 名**待 P1-1b 读源码钉死(plan 暂用 `{qr_id, code, url}` / `{code_status, ...}` 假设,P1-1b 验证后以源码为准)

---

## Phase 1: 装 xhs 依赖 + 建 async_xhs_wrapper.py

**Files:**
- Modify: `pyproject.toml` — `xhs` 从 `[xhs]` extra 移到核心 `dependencies`
- Create: `platforms/xiaohongshu/async_xhs_wrapper.py` — `AsyncXhsClient` 类
- Create: `tests/test_async_xhs_wrapper.py` — 7 个测试(wrapper 6 方法 + close)

### Task P1-1: 把 xhs 从 optional extra 移到核心 dependencies

- [ ] **P1-1a. 编辑 `pyproject.toml`**

  把 `"xhs>=0.1.9"` 从 `[project.optional-dependencies].xhs` 移到 `[project.dependencies]`,版本约束升级到 `>=0.2.13`(spec §7.1 指定 0.2.13)。

  **目标 diff:**

  ```diff
   dependencies = [
       "bilibili-api-python>=17.0",
       "click>=8.0",
       "rich>=13.0",
       "terminal-qrcode>=1.1",
       "tomlkit>=0.13",
       "aiohttp>=3.9",
       "httpx>=0.27",
       "faster-whisper>=1.1",
       "xhshow>=0.2.0",
  +    "xhs>=0.2.13",
   ]

   [project.optional-dependencies]
  -xhs = [
  -    "xhs>=0.1.9",
  -]
   dev = [
   ```

  ⚠️ **删掉整个 `[project.optional-dependencies].xhs` 块**。保留 dev / web 两块不动。

- [ ] **P1-1b. 装依赖 + 验证 import + 硬性核实 xhs 库 API 假设(前置 spike)**

  ⚠️ **这是 Phase 1 的前置 spike** — Phase 2/3 的 wrapper/auth/test 代码全部依赖此处的核实结果。fixer 必须按下面三个脚本顺序跑,把输出回填到下方"API 核实记录"。若实际结果与本 plan 的假设不符,**必须**先同步修改后续 P1-2/P1-3/P2-1/P2-2 的草稿,再继续。

  **脚本 ①:基本 import 验证**

  ```bash
  uv pip install -e ".[dev]"
  uv run python -c "import xhs; from xhs.core import XhsClient; from xhs.exception import NeedVerifyError, IPBlockError, SignError, DataFetchError; print('xhs OK, XhsClient:', XhsClient)"
  ```

  **预期输出:** `xhs OK, XhsClient: <class 'xhs.core.XhsClient'>`

  **脚本 ②:核实 `XhsClient.session` attribute 是否存在(Issue #2)**

  ```bash
  uv run python -c "
  from xhs.core import XhsClient
  c = XhsClient()
  print('has session:', hasattr(c, 'session'))
  print('has close:', hasattr(c, 'close'))
  if hasattr(c, 'session'):
      print('session type:', type(c.session).__name__)
  "
  ```

  **分支决策(回填到下方记录后,据此选择 wrapper.close() 实现):**
  - `has session=True` → P1-3a close() 用防御版 `getattr(sync_client, "session", None)` 写法(已是 plan 默认)
  - `has session=False` → wrapper.close() 改为不调 session.close(仅置 `self._client = None`,让 gc 回收)。**fixer 必须把 plan P1-3a close() 主体改成**:
    ```python
    async def close(self) -> None:
        sync_client = self._client
        if sync_client is None:
            return
        # XhsClient 不暴露 session(已 P1-1b 核实),无 sync close 可调;
        # 释放引用,依赖 gc 回收底层 requests.Session。
        self._client = None
    ```

  **脚本 ③:核实 `get_qrcode` / `check_qrcode` 返回 dict 的真实 key 名(Issue #3,防止循环自证)**

  ```bash
  uv run python -c "
  from xhs.core import XhsClient
  import inspect
  print('=== get_qrcode source ===')
  print(inspect.getsource(XhsClient.get_qrcode))
  print('=== check_qrcode source ===')
  print(inspect.getsource(XhsClient.check_qrcode))
  "
  ```

  ⚠️ **关键约束 — 钉死真实 key 名:**
  - plan 暂用假设:`get_qrcode → {qr_id, code, url, multi_flag}` / `check_qrcode → {code_status, ...}`
  - 如果源码显示的 key 名与假设**不同**(例如 `qrcode_id` 而非 `qr_id`,或 `code_status` 是其他变体),**以脚本 ③ 抓取的源码为准**:
    1. 同步修改 P1-2a `TestGetQrcode` 的 `return_value` dict
    2. 同步修改 P2-1a `TestGenerateQrCode.test_returns_qr_code_result_with_correct_fields` 的 mock return + 断言
    3. 同步修改 P2-2a `XhsAuthenticator.generate_qr_code` 的 `qr_data.get("qr_id", "")` / `qr_data.get("code", "")` / `qr_data.get("url", "")` 取值 key
    4. 同步修改 P2-2a `XhsAuthenticator.poll_qr_status` 的 `result.get("code_status", 0)`
  - **不允许**在测试和实现里都用同一组错误的 key 名(那是循环自证,重蹈 PR #40 覆辙)

  **API 核实记录(fixer 回填此处,P1-1b 完成前必须填):**
  ```
  - has session:    [True/False]
  - has close:      [True/False]
  - session type:   [类型名 或 N/A]
  - get_qrcode 返回 keys: [逗号分隔的实际 key 名, 如 "qr_id, code, url, multi_flag"]
  - check_qrcode 返回 keys: [逗号分隔的实际 key 名, 如 "code_status, code_msg"]
  - 与 plan 假设是否一致: [Yes / No + 不一致的 key 列表]
  - 据此调整的 task: [如 "P1-2a, P1-3a, P2-1a, P2-2a — 改了 X 处"]
  ```

  **回归验证** — 确认 `downloader.py` 引用 xhs 的地方仍能工作(downloader 用 try/except ImportError 容错,但现在 xhs 是核心依赖,应能成功 import):

  ```bash
  uv run python -c "from platforms.xiaohongshu.downloader import _try_xhs_downloader_lib; print('downloader import OK')"
  ```

  **验证:** 无 ImportError,`ruff check .` 不报新增问题,P1-1b "API 核实记录"已填。

---

### Task P1-2 (TDD): 写 tests/test_async_xhs_wrapper.py (RED)

- [ ] **P1-2a. 新建 `tests/test_async_xhs_wrapper.py`,先写失败测试**

  原则:**测包装正确性,不测 xhs 库内部**。mock `xhs.core.XhsClient` 类,断言 wrapper 调对应方法、返回值透传、异常透传。

  ```python
  """Tests for AsyncXhsClient — verify asyncio.to_thread wrapping of sync xhs library.

  Strategy: patch the underlying xhs.core.XhsClient class, then assert the async
  wrapper delegates to the right method and returns/raises the same value/error.
  Does NOT test the real xhs library HTTP layer.
  """

  from __future__ import annotations

  from unittest.mock import MagicMock, patch

  import pytest
  import requests

  from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient


  # ── ──


  class TestInit:
      async def test_constructor_passes_cookie_to_xhs_client(self):
          """AsyncXhsClient(cookie) instantiates xhs.XhsClient(cookie=...) underneath."""
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              mock_cls.return_value = mock_instance
              AsyncXhsClient(cookie="a1=foo; webId=bar")
              mock_cls.assert_called_once_with(cookie="a1=foo; webId=bar")


  class TestGetQrcode:
      async def test_delegates_to_get_qrcode_and_returns_dict(self):
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              mock_instance.get_qrcode.return_value = {"qr_id": "q1", "code": "c1", "url": "u1"}
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              result = await client.get_qrcode()

              mock_instance.get_qrcode.assert_called_once_with()
              assert result == {"qr_id": "q1", "code": "c1", "url": "u1"}


  class TestCheckQrcode:
      async def test_delegates_with_qr_id_and_code(self):
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              # Regression: real field name is snake_case 'code_status'
              mock_instance.check_qrcode.return_value = {"code_status": 2, "code_msg": "ok"}
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              result = await client.check_qrcode("qr_abc", "code_123")

              mock_instance.check_qrcode.assert_called_once_with("qr_abc", "code_123")
              assert result["code_status"] == 2


  class TestActivate:
      async def test_delegates_to_activate(self):
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              mock_instance.activate.return_value = {"web_session": "ws"}
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              result = await client.activate()

              mock_instance.activate.assert_called_once_with()
              assert result == {"web_session": "ws"}


  class TestGetSelfInfo:
      async def test_delegates_to_get_self_info_returns_dict(self):
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              mock_instance.get_self_info.return_value = {"nickname": "alice", "user_id": "u1"}
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              result = await client.get_self_info()

              mock_instance.get_self_info.assert_called_once_with()
              assert result["nickname"] == "alice"


  class TestCookieProperty:
      async def test_cookie_getter_returns_underlying_str(self):
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              # The underlying cookie property returns a "k=v;k=v" string
              type(mock_instance).cookie = property(lambda self: "a1=v1; web_session=ws")
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              assert client.cookie == "a1=v1; web_session=ws"


  class TestClose:
      async def test_close_closes_underlying_session(self):
          """XhsClient has no close(); wrapper must close its .session instead."""
          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              mock_session = MagicMock()
              mock_instance.session = mock_session
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              await client.close()

              mock_session.close.assert_called_once_with()


  class TestExceptionPassthrough:
      async def test_xhs_data_fetch_error_propagates(self):
          """xhs library exceptions must propagate unchanged (translation is auth.py's job)."""
          from xhs.exception import DataFetchError

          with patch("platforms.xiaohongshu.async_xhs_wrapper.XhsClient") as mock_cls:
              mock_instance = MagicMock()
              mock_instance.get_self_info.side_effect = DataFetchError("boom")
              mock_cls.return_value = mock_instance

              client = AsyncXhsClient(cookie="")
              with pytest.raises(DataFetchError, match="boom"):
                  await client.get_self_info()
  ```

- [ ] **P1-2b. 跑测试看 RED**

  ```bash
  uv run pytest tests/test_async_xhs_wrapper.py -x
  ```

  **预期:** `ImportError: cannot import name 'AsyncXhsClient' from 'platforms.xiaohongshu.async_xhs_wrapper'`(模块还不存在)。

---

### Task P1-3 (TDD): 写 platforms/xiaohongshu/async_xhs_wrapper.py (GREEN)

- [ ] **P1-3a. 新建 `platforms/xiaohongshu/async_xhs_wrapper.py`**

  设计要点:
  - `from __future__ import annotations` 在顶部(项目规范)
  - `# pyright: basic` 注释(参考 client.py / auth.py 的做法)
  - 用 `asyncio.to_thread` 包每个同步方法 — 不暴露 `requests.Session` 给上层
  - `close()` 防御性实现 — 用 `getattr(sync_client, "session", None)` 兜底,P1-1b 已核实 session 是否为 public attribute;如不是则跳过关闭(详见 P1-1b 检查脚本)

  ```python
  """Async wrapper around the synchronous ReaJason/xhs library.

  Design:
  - All public methods of XhsClient are wrapped with asyncio.to_thread so
    callers stay in async land without holding the event loop.
  - Does NOT expose the underlying requests.Session — auth.py talks only
    to the high-level methods declared here.
  - The wrapper owns the XhsClient lifecycle; close() releases its session.

  Rationale: see docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md
  """

  from __future__ import annotations

  # pyright: basic
  import asyncio
  import logging

  from xhs.core import XhsClient

  logger = logging.getLogger(__name__)


  class AsyncXhsClient:
      """Asynchronous facade over the synchronous xhs.XhsClient.

      Each method delegates to the underlying sync client via asyncio.to_thread.
      Exceptions from xhs.exception (DataFetchError / IPBlockError / SignError /
      NeedVerifyError, all subclasses of requests.RequestException) propagate
      unchanged; translation to trawler's exception hierarchy is the caller's
      responsibility (see platforms.xiaohongshu.auth._wrap_xhs_call).

      Args:
          cookie: Initial cookie string (``"k1=v1; k2=v2"``). May be empty.
      """

      def __init__(self, cookie: str = "") -> None:
          # sign=None → xhs library uses its built-in sign() (spec §2 决策表)
          self._client: XhsClient = XhsClient(cookie=cookie or None, sign=None)

      async def get_qrcode(self) -> dict:
          return await asyncio.to_thread(self._client.get_qrcode)

      async def check_qrcode(self, qr_id: str, code: str) -> dict:
          return await asyncio.to_thread(self._client.check_qrcode, qr_id, code)

      async def activate(self) -> dict:
          return await asyncio.to_thread(self._client.activate)

      async def get_self_info(self) -> dict:
          return await asyncio.to_thread(self._client.get_self_info)

      @property
      def cookie(self) -> str:
          """Current cookie jar as ``'k1=v1; k2=v2'`` string."""
          return self._client.cookie

      async def close(self) -> None:
          """关闭内部 xhs 库的 requests.Session。

          防御性实现:P1-1b 已硬性核实 XhsClient 是否暴露 .session attribute
          (见 P1-1b 检查脚本输出分支)。即使没有也安全返回,不抛异常。
          """
          sync_client = self._client
          if sync_client is None:
              return
          session = getattr(sync_client, "session", None)
          if session is not None:
              await asyncio.to_thread(session.close)
          self._client = None
   ```

- [ ] **P1-3b. 跑测试看 GREEN**

  ```bash
  uv run pytest tests/test_async_xhs_wrapper.py -v
  ```

  **预期:** 全部 8 个测试通过(TestInit / TestGetQrcode / TestCheckQrcode / TestActivate / TestGetSelfInfo / TestCookieProperty / TestClose / TestExceptionPassthrough)。

---

### Task P1-4: Phase 1 收尾验证

- [ ] **P1-4a. ruff + pyright + pytest 全过**

  ```bash
  uv run ruff check platforms/xiaohongshu/async_xhs_wrapper.py tests/test_async_xhs_wrapper.py
  uv run pyright platforms/xiaohongshu/async_xhs_wrapper.py
  uv run pytest tests/test_async_xhs_wrapper.py
  ```

  **预期:** ruff 无 lint 报错;pyright 0 errors;pytest 8/8 通过。

- [ ] **P1-4b. 回归 pytest 全量(确认没破坏其他测试)**

  ```bash
  uv run pytest
  ```

  **预期:** 通过数应 ≥ 原数(此时 auth 测试还在用旧 client.py 实现,但因为 client.py 还没动,旧测试应仍通过)。如出现新 fail,先修再进 Phase 2。

---

## Phase 2: 重写 auth.py XhsAuthenticator

**Files:**
- Rewrite: `platforms/xiaohongshu/auth.py` — `XhsAuthenticator` 类整体重写
- Rewrite: `tests/test_xhs_authenticator.py` — 测试整体重写
- **保留不动** (auth.py 内的同模块辅助函数): `get_xhs_cookie`, `generate_a1`, `generate_web_id`, `build_tokens_from_config`

### Task P2-1 (TDD): 重写 tests/test_xhs_authenticator.py (RED)

- [ ] **P2-1a. 整体重写测试文件**

  原则:**测编排,不测库**。Mock `AsyncXhsClient` 而非 xhs 库本身。测试组织参考 bilibili 的 `tests/test_bilibili_authenticator.py` 风格。

  保留 / 删除决策:
  - **删:** `TestRefreshTokens`(xhs 库无 refresh 概念,refresh 退化为 validate)
  - **删:** `TestEnsureClientClosesOldClient`(新实现没有 `_ensure_client` 私有方法)
  - **改字段名:** `codeStatus` → `code_status` (spec §1.2 根因 #1)
  - **改 mock 目标:** `XhsClient` (sync) → `AsyncXhsClient` (async wrapper)
  - **新增:** `TestExceptionTranslation`, `TestGetUserNickname`

  ```python
  """Tests for XhsAuthenticator — fully mocked AsyncXhsClient.

  Rewrite (2026-06-26): auth moved to ReaJason/xhs library via AsyncXhsClient.
  All XHS HTTP is mocked; no real network calls.

  See docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md
  """

  from __future__ import annotations

  import time
  from unittest.mock import AsyncMock, MagicMock, patch

  import pytest
  import requests

  from platforms.xiaohongshu.auth import XhsAuthenticator
  from shared.auth.base import (
      AuthStatus,
      BaseAuthenticator,
      PlatformTokens,
      QRExpiredError,
      QRCodeResult,
      QRStatus,
  )
  from shared.exceptions import CaptchaError, DataError, IpBlockError, RetryableError

  # ── ──


  def _sample_cookies() -> dict[str, str]:
      return {
          "a1": "test_a1_value",
          "web_session": "test_web_session",
          "webId": "test_web_id",
          "gid": "test_gid",
      }


  def _make_tokens(cookies: dict[str, str] | None = None) -> PlatformTokens:
      return PlatformTokens(
          platform="xhs",
          cookies=cookies or _sample_cookies(),
          obtained_at=time.time(),
          expires_at=time.time() + 7 * 86400,
      )


  # ── ──


  class TestGenerateQrCode:
      """generate_qr_code returns QRCodeResult sourced from AsyncXhsClient.get_qrcode.

      Flow (spec §4.1):
        a1 = generate_a1()
        web_id = generate_web_id(a1)
        client = AsyncXhsClient(cookie=f"a1={a1};webId={web_id}")
        qr = await client.get_qrcode()  # {qr_id, code, url, multi_flag}
        cache qr_id+code on instance
        return QRCodeResult(qr_url=qr["url"], qr_key=qr["qr_id"], expires_in=180)
      """

      async def test_returns_qr_code_result_with_correct_fields(self):
          auth = XhsAuthenticator()

          mock_client = MagicMock()
          mock_client.get_qrcode = AsyncMock(
              return_value={
                  "qr_id": "qr_abc",
                  "code": "code_123",
                  "url": "https://qr.xhs.com/abc",
                  "multi_flag": 0,
              }
          )
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client) as mock_cls:
              result = await auth.generate_qr_code()

          assert isinstance(result, QRCodeResult)
          assert result.qr_key == "qr_abc"
          assert result.qr_url == "https://qr.xhs.com/abc"
          assert result.expires_in == 180
          # Verify cookie passed to AsyncXhsClient contains a1 + webId
          mock_cls.assert_called_once()
          init_cookie = mock_cls.call_args.kwargs.get("cookie") or mock_cls.call_args.args[0]
          assert "a1=" in init_cookie
          assert "webId=" in init_cookie
          # Verify client cached for later poll/get_tokens
          assert auth._client is mock_client
          # Verify qr_id + code cached for poll_qr_status
          assert auth._qr_id == "qr_abc"
          assert auth._code == "code_123"

      async def test_propagates_get_qrcode_error_as_retryable(self):
          """If get_qrcode raises a non-translated exception, it bubbles up.

          _wrap_xhs_call translates known xhs exceptions; unknown exceptions
          should still propagate (caller decides).
          """
          from xhs.exception import DataFetchError

          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_qrcode = AsyncMock(side_effect=DataFetchError("server down"))
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              with pytest.raises(DataError, match="server down"):
                  await auth.generate_qr_code()


  # ── ──


  class TestPollQrStatus:
      """poll_qr_status reads code_status (snake_case!). Regression for spec §1.2 #1.

      Mapping: 2=SUCCESS, 1=SCANNED, 3=EXPIRED, else=WAITING
      """

      async def test_code_status_2_returns_success(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.check_qrcode = AsyncMock(return_value={"code_status": 2})
          auth._client = mock_client
          auth._qr_id = "q1"
          auth._code = "c1"

          status = await auth.poll_qr_status("q1")
          assert status.status == QRStatus.SUCCESS
          assert status.success is True
          mock_client.check_qrcode.assert_awaited_once_with("q1", "c1")

      async def test_code_status_1_returns_scanned(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.check_qrcode = AsyncMock(return_value={"code_status": 1})
          auth._client = mock_client
          auth._qr_id = "q1"
          auth._code = "c1"

          status = await auth.poll_qr_status("q1")
          assert status.status == QRStatus.SCANNED

      async def test_code_status_3_returns_expired(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.check_qrcode = AsyncMock(return_value={"code_status": 3})
          auth._client = mock_client
          auth._qr_id = "q1"
          auth._code = "c1"

          status = await auth.poll_qr_status("q1")
          assert status.status == QRStatus.EXPIRED

      async def test_code_status_0_or_other_returns_waiting(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.check_qrcode = AsyncMock(return_value={"code_status": 0})
          auth._client = mock_client
          auth._qr_id = "q1"
          auth._code = "c1"

          status = await auth.poll_qr_status("q1")
          assert status.status == QRStatus.WAITING

      async def test_missing_code_status_defaults_to_waiting(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.check_qrcode = AsyncMock(return_value={})
          auth._client = mock_client
          auth._qr_id = "q1"
          auth._code = "c1"

          status = await auth.poll_qr_status("q1")
          assert status.status == QRStatus.WAITING

      async def test_exception_returns_waiting(self):
          """Any poll exception → WAITING (never raise; UI polls in loop)."""
          from xhs.exception import DataFetchError

          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.check_qrcode = AsyncMock(side_effect=DataFetchError("net down"))
          auth._client = mock_client
          auth._qr_id = "q1"
          auth._code = "c1"

          status = await auth.poll_qr_status("q1")
          assert status.status == QRStatus.WAITING
          assert not status.success


  # ── ──


  class TestGetTokens:
      """get_tokens: SUCCESS → activate() → read cookie str → parse into PlatformTokens."""

      async def test_activate_then_returns_cookies_from_client_cookie_str(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.activate = AsyncMock(return_value={})
          # cookie property returns "k=v; k=v" string
          type(mock_client).cookie = property(lambda self: "a1=v1; web_session=ws123; gid=g1")
          auth._client = mock_client

          tokens = await auth.get_tokens("qr_abc")

          mock_client.activate.assert_awaited_once()
          assert tokens.platform == "xhs"
          assert tokens.cookies["a1"] == "v1"
          assert tokens.cookies["web_session"] == "ws123"
          assert tokens.cookies["gid"] == "g1"
          assert tokens.expires_at > time.time()

      async def test_returns_empty_cookies_when_no_client(self):
          auth = XhsAuthenticator()
          auth._client = None

          tokens = await auth.get_tokens("qr_abc")
          assert tokens.cookies == {}
          assert tokens.expires_at <= time.time() + 5

      async def test_propagates_activate_error_as_data_error(self):
          from xhs.exception import DataFetchError

          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.activate = AsyncMock(side_effect=DataFetchError("activate failed"))
          auth._client = mock_client

          with pytest.raises(DataError):
              await auth.get_tokens("qr_abc")


  # ── ──


  class TestExceptionTranslation:
      """_wrap_xhs_call decorator translates xhs library exceptions to trawler types.

      Mapping (spec §5.2):
        NeedVerifyError → CaptchaError
        IPBlockError    → IpBlockError
        SignError       → RetryableError
        DataFetchError  → DataError
        RequestException→ RetryableError  (catch-all)
      """

      @pytest.mark.parametrize(
          "xhs_exc, trawler_exc",
          [
              ("NeedVerifyError", CaptchaError),
              ("IPBlockError", IpBlockError),
              ("SignError", RetryableError),
              ("DataFetchError", DataError),
          ],
      )
      async def test_decorator_translates_each_xhs_exception(self, xhs_exc, trawler_exc):
          """get_tokens wrapped → activate raises xhs_exc → caller sees trawler_exc."""
          import xhs.exception as xe

          auth = XhsAuthenticator()
          exc_class = getattr(xe, xhs_exc)
          mock_client = MagicMock()
          mock_client.activate = AsyncMock(side_effect=exc_class("boom"))
          auth._client = mock_client

          with pytest.raises(trawler_exc):
              await auth.get_tokens("q1")

      async def test_generic_requests_exception_becomes_retryable(self):
          """Any other requests.RequestException subclass → RetryableError."""
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.activate = AsyncMock(
              side_effect=requests.ConnectionError("network gone")
          )
          auth._client = mock_client

          with pytest.raises(RetryableError):
              await auth.get_tokens("q1")


  # ── ──


  class TestGetUserNickname:
      """get_user_nickname MUST NOT raise — failures return None."""

      async def test_returns_nickname_from_get_self_info(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(return_value={"nickname": "alice"})
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              nick = await auth.get_user_nickname(_make_tokens())

          assert nick == "alice"
          mock_client.close.assert_awaited_once()

      async def test_returns_none_on_xhs_exception(self):
          from xhs.exception import DataFetchError

          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(side_effect=DataFetchError("denied"))
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              nick = await auth.get_user_nickname(_make_tokens())

          assert nick is None

      async def test_returns_none_when_nickname_missing(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(return_value={"user_id": "u1"})  # no nickname
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              nick = await auth.get_user_nickname(_make_tokens())

          assert nick is None


  # ── ──


  class TestQrLogin:
      """qr_login 主流程 — 串联 generate_qr_code/poll_qr_status/get_tokens,CLI 入口依赖。

      覆盖 spec §4.1-4.3 的 deadline 循环、QRExpiredError 分支、asyncio.sleep。
      无此测试类 = qr_login 假绿(CLI 入口 run_check.py 的 trawler auth xhs 走这条路径)。

      不加 @pytest.mark.asyncio 装饰器(pyproject asyncio_mode=auto,见 plan 顶部说明)。
      """

      async def test_success_path_returns_tokens(self):
          """mock 三步全成功:generate_qr_code → QRCodeResult;
          poll_qr_status 先 SCANNED 再 SUCCESS;get_tokens → PlatformTokens;
          display_qr_in_terminal 被 patch 掉(避免终端输出)。断言返回值是 PlatformTokens 且 success。
          """
          auth = XhsAuthenticator()

          qr_result = QRCodeResult(
              qr_url="https://qr.xhs.com/abc",
              qr_key="qr_abc",
              expires_in=180,
          )
          tokens = _make_tokens()

          with (
              patch.object(auth, "generate_qr_code", new=AsyncMock(return_value=qr_result)),
              patch.object(
                  auth,
                  "poll_qr_status",
                  new=AsyncMock(side_effect=[
                      AuthStatus(success=False, status=QRStatus.SCANNED, message="scanned"),
                      AuthStatus(success=True, status=QRStatus.SUCCESS, message="ok"),
                  ]),
              ),
              patch.object(auth, "get_tokens", new=AsyncMock(return_value=tokens)),
              patch("platforms.xiaohongshu.auth.display_qr_in_terminal") as mock_display,
          ):
              result = await auth.qr_login()

          assert isinstance(result, PlatformTokens)
          assert result.cookies == tokens.cookies
          mock_display.assert_called_once_with("https://qr.xhs.com/abc")
          # poll 调了 2 次(SCANNED + SUCCESS),SUCCESS 后立即 return 不再 poll
          assert auth.poll_qr_status.await_count == 2
          # get_tokens 只在 SUCCESS 分支调一次
          auth.get_tokens.assert_awaited_once()

      async def test_expired_raises(self):
          """poll_qr_status 返回 EXPIRED → qr_login 立即抛 QRExpiredError,不再循环。
          """
          auth = XhsAuthenticator()

          qr_result = QRCodeResult(
              qr_url="https://qr.xhs.com/abc",
              qr_key="qr_abc",
              expires_in=180,
          )

          with (
              patch.object(auth, "generate_qr_code", new=AsyncMock(return_value=qr_result)),
              patch.object(
                  auth,
                  "poll_qr_status",
                  new=AsyncMock(return_value=AuthStatus(
                      success=False, status=QRStatus.EXPIRED, message="expired",
                  )),
              ),
              patch("platforms.xiaohongshu.auth.display_qr_in_terminal"),
              patch("platforms.xiaohongshu.auth.asyncio.sleep", new=AsyncMock()),
          ):
              with pytest.raises(QRExpiredError):
                  await auth.qr_login()

          # EXPIRED 立即 raise,get_tokens 不应被调
          auth.poll_qr_status.assert_awaited()

      async def test_timeout_raises(self):
          """deadline 超时(expires_in 极短 + poll 永远 WAITING)→ qr_login 抛 QRExpiredError。
          """
          auth = XhsAuthenticator()

          # expires_in=0 → deadline 立即到期,while 条件首次检查即 false 之前先 poll 一次
          qr_result = QRCodeResult(
              qr_url="https://qr.xhs.com/abc",
              qr_key="qr_abc",
              expires_in=0,
          )

          with (
              patch.object(auth, "generate_qr_code", new=AsyncMock(return_value=qr_result)),
              patch.object(
                  auth,
                  "poll_qr_status",
                  new=AsyncMock(return_value=AuthStatus(
                      success=False, status=QRStatus.WAITING, message="waiting",
                  )),
              ),
              patch("platforms.xiaohongshu.auth.display_qr_in_terminal"),
              patch("platforms.xiaohongshu.auth.asyncio.sleep", new=AsyncMock()),
              patch("platforms.xiaohongshu.auth.time.monotonic", side_effect=[0.0, 100.0]),
          ):
              with pytest.raises(QRExpiredError):
                  await auth.qr_login()


  # ── ──


  class TestValidateTokens:
      """validate_tokens: xhs lib has no refresh → validate == probe via get_self_info."""

      async def test_expired_at_returns_false(self):
          auth = XhsAuthenticator()
          tokens = PlatformTokens(
              platform="xhs",
              cookies={"a1": "x"},
              obtained_at=time.time() - 86400,
              expires_at=time.time() - 10,
          )
          assert await auth.validate_tokens(tokens) is False

      async def test_get_self_info_with_nickname_returns_true(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(return_value={"nickname": "x"})
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              result = await auth.validate_tokens(_make_tokens())

          assert result is True

      async def test_get_self_info_without_nickname_returns_false(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(return_value={})
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              result = await auth.validate_tokens(_make_tokens())

          assert result is False

      async def test_xhs_exception_returns_false(self):
          from xhs.exception import DataFetchError

          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(side_effect=DataFetchError("expired"))
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              result = await auth.validate_tokens(_make_tokens())

          assert result is False


  # ── ──


  class TestRefreshTokens:
      """refresh_tokens is degraded to validate-only (spec §4.5).

      xhs lib has no refresh concept. If get_self_info succeeds → return
      original tokens with bumped expires_at. If fails → raise (caller asks
      user to re-login).
      """

      async def test_valid_tokens_returned_with_bumped_expiry(self):
          auth = XhsAuthenticator()
          tokens = _make_tokens()
          original_expiry = tokens.expires_at
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(return_value={"nickname": "x"})
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              result = await auth.refresh_tokens(tokens)

          assert result.cookies == tokens.cookies
          assert result.expires_at >= original_expiry

      async def test_invalid_tokens_raises(self):
          from xhs.exception import DataFetchError

          auth = XhsAuthenticator()
          tokens = _make_tokens()
          mock_client = MagicMock()
          mock_client.get_self_info = AsyncMock(side_effect=DataFetchError("expired"))
          mock_client.close = AsyncMock()

          with patch("platforms.xiaohongshu.auth.AsyncXhsClient", return_value=mock_client):
              with pytest.raises(DataError):
                  await auth.refresh_tokens(tokens)


  # ── ──


  class TestClose:
      async def test_close_closes_cached_client(self):
          auth = XhsAuthenticator()
          mock_client = MagicMock()
          mock_client.close = AsyncMock()
          auth._client = mock_client

          await auth.close()

          mock_client.close.assert_awaited_once()
          assert auth._client is None

      async def test_close_when_no_client_is_noop(self):
          auth = XhsAuthenticator()
          auth._client = None
          await auth.close()  # must not raise


  # ── ──


  class TestIsAuthenticator:
      def test_is_subclass(self):
          assert issubclass(XhsAuthenticator, BaseAuthenticator)

      def test_supports_qr_login(self):
          assert XhsAuthenticator().supports_qr_login() is True

      def test_supports_refresh_returns_true(self):
          """Web UI refresh button needs supports_refresh()==True even though
          refresh is degraded to validate. Spec §4.5."""
          assert XhsAuthenticator().supports_refresh() is True
  ```

- [ ] **P2-1b. 跑测试看 RED**

  ```bash
  uv run pytest tests/test_xhs_authenticator.py -x
  ```

  **预期:** 大量 FAIL — 新测试 mock 的是 `AsyncXhsClient` 而旧实现 import 的是 `XhsClient`,字段名 `code_status` vs 旧的 `codeStatus`,实例字段 `_qr_id` / `_code` 不存在等。这是预期的 RED。

---

### Task P2-2 (TDD): 重写 platforms/xiaohongshu/auth.py (GREEN)

- [ ] **P2-2a. 重写 XhsAuthenticator 类(保留同模块辅助函数)**

  **保留不动(模块顶部辅助):**
  - `get_xhs_cookie(config)` (39-60 行)
  - `generate_a1()` (68-74 行)
  - `generate_web_id(a1)` (77-79 行)
  - `build_tokens_from_config(config)` (259-272 行)

  **删除:**
  - `from platforms.xiaohongshu.client import XhsClient` import
  - 旧 `XhsAuthenticator` 整个类(87-256 行)

  **新增:**
  - `from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient` import
  - `_wrap_xhs_call` 装饰器(异常转译,模块级私有)
  - 新 `XhsAuthenticator` 类

  新文件骨架(关键部分,完整代码由 fixer 按此实现):

  ```python
  """小红书认证模块 — 基于 ReaJason/xhs 库的 QR 登录 + Token Keepalive

  设计:
  - HTTP 通过 ``AsyncXhsClient`` (异步包装 xhs 库), 不再持有 aiohttp session
  - QR 登录使用 xhs 库 get_qrcode / check_qrcode (内置 sign + 自动补 gid)
  - xhs 库异常通过 ``_wrap_xhs_call`` 装饰器转译到 trawler 异常体系
  - refresh_tokens 退化为 validate-only (xhs 库无 refresh 概念)

  See docs/superpowers/specs/2026-06-26-xhs-auth-xhs-library-migration-design.md
  """

  from __future__ import annotations

  # pyright: basic
  import asyncio
  import binascii
  import functools
  import hashlib
  import logging
  import os
  import random
  import time
  from collections.abc import Callable
  from typing import Any, TypeVar

  import requests
  from xhs.exception import DataFetchError, IPBlockError, NeedVerifyError, SignError

  from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
  from shared.auth.base import (
      AuthStatus,
      BaseAuthenticator,
      PlatformTokens,
      QRExpiredError,
      QRStatus,
  )
  from shared.auth.qr_display import display_qr_in_terminal
  from shared.config import Config
  from shared.cookie_utils import build_cookie_str, parse_cookie_str
  # NOTE: trawler 的 IpBlockError(小写 p) 与 xhs.exception 的 IPBlockError(大写 P)
  # 拼写不同,本模块顶部同时 import 两者:xhs 版在 line 955 的 xhs.exception import,
  # trawler 版在此处。_wrap_xhs_call 里 except 块按名字区分(IPBlockError=捕获,
  # IpBlockError=raise)。
  from shared.exceptions import CaptchaError, DataError, IpBlockError, RetryableError

  # Import QRCodeResult from base (only used in type hint, kept for clarity)
  from shared.auth.base import QRCodeResult

  logger = logging.getLogger("trawler.xiaohongshu.auth")

  _A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"

  _F = TypeVar("_F", bound=Callable[..., Any])


  # ═══════════════════════════════════════════════════════════
  # Helper functions (UNCHANGED from previous auth.py)
  # ═══════════════════════════════════════════════════════════

  def get_xhs_cookie(config: Config) -> str:
      # ... unchanged ...

  def generate_a1() -> str:
      # ... unchanged ...

  def generate_web_id(a1: str) -> str:
      # ... unchanged ...


  # ═══════════════════════════════════════════════════════════
  # Exception translation decorator (spec §5)
  # ═══════════════════════════════════════════════════════════

  def _wrap_xhs_call(func: _F) -> _F:
      """Translate xhs library exceptions to trawler's exception hierarchy.

      Mapping (spec §5.2) — except 顺序不可调,具体在前,RequestException 兜底最后:
        NeedVerifyError → CaptchaError
        IPBlockError    → IpBlockError
        SignError       → RetryableError
        DataFetchError  → DataError
        RequestException→ RetryableError  (catch-all, ordered LAST)

      签名说明(最终版,不要再让 fixer 改):
      - 用 `_F = TypeVar("_F", bound=Callable[..., Any])` + `def _wrap_xhs_call(func: _F) -> _F`
        保证被装饰函数签名透传,pyright strict 通过。
      - `return wrapper  # type: ignore[return-value]` 是必须的:wrapper 是新 async 函数,
        与 _F 不同型,这是异步装饰器的标准 pyright 兜底,无需绕开。
      - 不用 ParamSpec(P.args/P.kwargs 在 pyright strict 下对 async 装饰器报错更多)。
      """
      @functools.wraps(func)
      async def wrapper(*args: Any, **kwargs: Any) -> Any:
          try:
              return await func(*args, **kwargs)
          except NeedVerifyError as e:
              raise CaptchaError(f"XHS captcha challenge: {e}") from e
          except IPBlockError as e:
              # NOTE: 这里 raise 的是 trawler 的 IpBlockError(shared.exceptions),
              # 不是 xhs 库的 IPBlockError(xhs.exception)。两者拼法不同
              # (trawler: Ip / xhs: IP)。顶部已 `from shared.exceptions import
              # IpBlockError`,本 except 块捕获的 IPBlockError 来自 xhs.exception
              # 的顶部 import,无需 lazy import。
              raise IpBlockError(f"XHS IP blocked: {e}") from e
          except SignError as e:
              raise RetryableError(f"XHS sign error: {e}") from e
          except DataFetchError as e:
              raise DataError(f"XHS data fetch error: {e}") from e
          except requests.RequestException as e:
              raise RetryableError(f"XHS network error: {e}") from e

      return wrapper  # type: ignore[return-value]


  # ═══════════════════════════════════════════════════════════
  # XhsAuthenticator — QR 登录 + validate-only refresh (via xhs lib)
  # ═══════════════════════════════════════════════════════════

  class XhsAuthenticator(BaseAuthenticator):
      """小红书 QR 扫码登录 (通过 ReaJason/xhs 库)。

      xhs 库无 refresh 概念, refresh_tokens 退化为 validate-only:
      成功 → 返回原 tokens + bumped expires_at; 失败 → 抛异常。
      """

      def __init__(self) -> None:
          self._client: AsyncXhsClient | None = None
          self._qr_id: str = ""
          self._code: str = ""

      @_wrap_xhs_call
      async def generate_qr_code(self) -> QRCodeResult:
          """生成 QR 二维码 (spec §4.1)."""
          logger.info("🔑 XhsAuthenticator 生成二维码...")
          a1 = generate_a1()
          web_id = generate_web_id(a1)
          init_cookie = f"a1={a1}; webId={web_id}"

          # New AsyncXhsClient per QR session; close any prior cached one.
          if self._client is not None:
              try:
                  await self._client.close()
              except Exception:
                  logger.debug("close prior client failed", exc_info=True)
          self._client = AsyncXhsClient(cookie=init_cookie)

          qr_data = await self._client.get_qrcode()
          self._qr_id = qr_data.get("qr_id", "")
          self._code = qr_data.get("code", "")

          return QRCodeResult(
              qr_url=qr_data.get("url", ""),
              qr_key=self._qr_id,
              expires_in=180,
          )

      async def poll_qr_status(self, qr_key: str) -> AuthStatus:
          """轮询 QR 状态 (spec §4.2). 字段名 code_status (snake_case!)."""
          logger.info("🔑 XhsAuthenticator 轮询扫码状态...")
          if self._client is None:
              return AuthStatus(success=False, status=QRStatus.WAITING, message="无 client")
          try:
              result = await self._client.check_qrcode(self._qr_id, self._code)
          except Exception as e:
              logger.warning("🔑 轮询异常: %s", e)
              return AuthStatus(success=False, status=QRStatus.WAITING, message=f"轮询失败: {e}")

          code_status = result.get("code_status", 0)  # ← 关键修复: snake_case
          if code_status == 2:
              return AuthStatus(success=True, status=QRStatus.SUCCESS, message="登录成功")
          elif code_status == 1:
              return AuthStatus(success=False, status=QRStatus.SCANNED, message="已扫描,请确认")
          elif code_status == 3:
              return AuthStatus(success=False, status=QRStatus.EXPIRED, message="二维码已过期")
          else:
              return AuthStatus(success=False, status=QRStatus.WAITING, message="等待扫描")

      @_wrap_xhs_call
      async def get_tokens(self, qr_key: str) -> PlatformTokens:
          """SUCCESS 后提取登录后 cookies (spec §4.3)."""
          logger.info("🔑 XhsAuthenticator 获取凭证...")
          now = time.time()
          if self._client is None:
              return PlatformTokens(platform="xhs", cookies={}, obtained_at=now, expires_at=now)

          await self._client.activate()
          full_cookie_str = self._client.cookie
          cookie_dict = parse_cookie_str(full_cookie_str)
          return PlatformTokens(
              platform="xhs",
              cookies={k: v for k, v in cookie_dict.items() if v},
              obtained_at=now,
              expires_at=now + 7 * 86400,
          )

      async def qr_login(
          self,
          on_status: Callable[[AuthStatus], None] | None = None,
      ) -> PlatformTokens:
          """QR 扫码登录全流程 (spec §4.1-4.3 串联).

          Note: 复用 BaseAuthenticator.qr_login 默认实现也行, 但这里覆盖以
          保留 generate_qr_code 内的 client caching 语义 + display_qr_in_terminal.
          """
          qr = await self.generate_qr_code()
          display_qr_in_terminal(qr.qr_url)

          deadline = time.monotonic() + qr.expires_in
          while time.monotonic() < deadline:
              status = await self.poll_qr_status(qr.qr_key)
              if on_status is not None:
                  on_status(status)
              if status.status == QRStatus.SUCCESS:
                  return await self.get_tokens(qr.qr_key)
              if status.status == QRStatus.EXPIRED:
                  raise QRExpiredError("二维码已过期")
              await asyncio.sleep(2)
          raise QRExpiredError("二维码轮询超时")

      @_wrap_xhs_call
      async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
          """Refresh 退化为 validate-only (spec §4.5).

          成功 → 返回原 tokens + bumped expires_at
          失败 → 抛异常 (caller 让用户重扫码)
          """
          logger.info("🔑 XhsAuthenticator 续期 token (validate-only)...")
          client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
          try:
              info = await client.get_self_info()
              if not info.get("nickname"):
                  raise DataError("cookie 无效: get_self_info 返回空 nickname")
          finally:
              await client.close()

          return PlatformTokens(
              platform="xhs",
              cookies=dict(tokens.cookies),
              obtained_at=time.time(),
              expires_at=time.time() + 7 * 86400,
          )

      async def validate_tokens(self, tokens: PlatformTokens) -> bool:
          """验证 cookie 有效性 (spec §4.5)."""
          if tokens.expires_at < time.time():
              return False
          client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
          try:
              try:
                  info = await client.get_self_info()
                  return bool(info.get("nickname"))
              except Exception as e:
                  logger.debug("validate_tokens probe failed: %s", e)
                  return False
          finally:
              await client.close()

      async def get_user_nickname(self, tokens: PlatformTokens) -> str | None:
          """获取当前用户昵称. MUST NOT raise — 失败返回 None (spec §4.4)."""
          client = AsyncXhsClient(cookie=build_cookie_str(tokens.cookies))
          try:
              try:
                  info = await client.get_self_info()
                  nick = info.get("nickname") if isinstance(info, dict) else None
                  return nick or None
              except Exception as e:
                  logger.warning("XHS nickname 获取失败: %s", e)
                  return None
          finally:
              await client.close()

      def supports_refresh(self) -> bool:
          return True

      async def close(self) -> None:
          """关闭缓存的 client (如有)."""
          if self._client is not None:
              try:
                  await self._client.close()
              except Exception as e:
                  logger.debug("close client failed: %s", e)
              self._client = None


  # ═══════════════════════════════════════════════════════════
  # build_tokens_from_config (UNCHANGED from previous auth.py)
  # ═══════════════════════════════════════════════════════════

  def build_tokens_from_config(config: Config) -> PlatformTokens | None:
      # ... unchanged ...
  ```

  ⚠️ **fixer 注意:**
  - `_wrap_xhs_call` 签名是**最终版**,不要再改:用 `_F = TypeVar("_F", bound=Callable[..., Any])`,`def _wrap_xhs_call(func: _F) -> _F`,wrapper 用 `*args: Any, **kwargs: Any`,`return wrapper  # type: ignore[return-value]`。装饰器代码块已直接给出最终版,逐字复制即可,不要"二选一"、不要"如 pyright 报错降级为..."。
  - `IpBlockError` 名字冲突已用顶部双 import 解决:`from xhs.exception import IPBlockError`(捕获) + `from shared.exceptions import IpBlockError`(raise)。两个名字拼法不同,Python 是大小写敏感的,共存无歧义。
  - except 顺序不可调:`NeedVerifyError` / `IPBlockError` / `SignError` / `DataFetchError` 都是 `requests.RequestException` 子类,具体异常必须排在 `requests.RequestException` 兜底之前。

- [ ] **P2-2b. 跑测试看 GREEN**

  ```bash
  uv run pytest tests/test_xhs_authenticator.py -v
  ```

  **预期:** 全部通过(约 30 个测试,含 parametrize 展开)。

  如出现 fail:
  - 字段名问题 → 确认 `code_status` vs `codeStatus`
  - mock target 不对 → 确认 patch 的是 `platforms.xiaohongshu.auth.AsyncXhsClient`
  - 异常转译漏 → 检查 except 顺序(具体在前,`requests.RequestException` 兜底在最后)

---

### Task P2-3: Phase 2 收尾验证

- [ ] **P2-3a. ruff + pyright 全过**

  ```bash
  uv run ruff check platforms/xiaohongshu/auth.py tests/test_xhs_authenticator.py
  uv run pyright platforms/xiaohongshu/auth.py
  ```

- [ ] **P2-3b. 回归 pytest 全量(此时 test_xhs_client.py 中的 create_qrcode / check_qrcode_status / fetch_sec_cookies 测试仍存在,client.py 也未动,应通过)**

  ```bash
  uv run pytest
  ```

  **预期:** 全过。Phase 3 才删 client.py 的废方法和对应测试。

---

## Phase 3: 删 client.py 废方法 + 清理测试

**Files:**
- Modify: `platforms/xiaohongshu/client.py` — 删 `create_qrcode` / `check_qrcode_status` / `fetch_sec_cookies`
- Modify: `tests/test_xhs_client.py` — 删对应 3 个测试方法

### Task P3-1: 确认 client.py 的其他方法不依赖被删方法

- [ ] **P3-1a. grep 验证被删方法在 client.py 内 / 项目内的所有引用**

  ```bash
  # 项目内引用(排除 client.py 自身定义和 test_xhs_client.py):
  rg -n "create_qrcode|check_qrcode_status|fetch_sec_cookies" \
     --type py \
     -g '!platforms/xiaohongshu/client.py' \
     -g '!tests/test_xhs_client.py' \
     -g '!tests/test_xhs_authenticator.py' \
     -g '!docs/**'
  ```

  **预期:** 0 输出(Phase 2 已把 auth.py 切到 AsyncXhsClient,无残留引用)。

  如出现引用 → 先解决(可能是注释/文档,记录后跳过;可能是代码 → 必修)。

- [ ] **P3-1b. 确认 `_request` 的 `_set_cookie_collect` 参数是否还被使用**

  `fetch_sec_cookies` 是 `_set_cookie_collect` 的唯一使用者。删 fetch_sec_cookies 后该参数变成 dead code。

  ```bash
  rg -n "_set_cookie_collect" --type py -g '!docs'
  ```

  **决策:** spec 没明说删 `_set_cookie_collect`,**保留参数**即可(默认 None,无害;未来可能复用)。仅删 3 个 auth 方法,不动 `_request` 签名。这避免改 `_request` 引发 client.py 其他方法的回归风险。

---

### Task P3-2: 删 client.py 三个 auth 方法

- [ ] **P3-2a. 删除 `create_qrcode` / `check_qrcode_status` / `fetch_sec_cookies`**

  ⚠️ **不要依赖固定行号** — 删除任一方法都会让后续行号漂移。用 grep 按方法名定位:

  ```bash
  rg -n "async def create_qrcode|async def check_qrcode_status|async def fetch_sec_cookies" \
     platforms/xiaohongshu/client.py
  ```

  参考:当前(client.py 未动时)这三个方法相邻,大致落在 client.py 第 319-406 行区域,但这只是参考,**fixer 必须用 grep 输出的实际行号**,逐个删:
  - `async def create_qrcode(...)` 到下一个 `async def` 之前
  - `async def check_qrcode_status(...)` 到下一个 `async def` 之前
  - `async def fetch_sec_cookies(...)` 到下一个 `async def` 之前

  删完后,用以下 grep 确认这三个方法名在 client.py 内已 0 命中:

  ```bash
  rg -n "async def create_qrcode|async def check_qrcode_status|async def fetch_sec_cookies" \
     platforms/xiaohongshu/client.py
  # 预期: 0 输出
  ```

  删除后,client.py 的 `# ── Auth APIs ──` 区域应只剩(同样用 grep 定位,不靠行号):
  - `async def get_user_info`
  - `async def probe`
  - `async def refresh_cookies`

  ⚠️ **不要删 `get_user_info` / `probe` / `refresh_cookies`** — 这些是 monitor/nickname 流程还在用的。`refresh_cookies` 现在虽没被新 auth.py 用,但 monitor.py 可能用;先 grep 确认:

  ```bash
  rg -n "refresh_cookies|\.probe\(\)|get_user_info" --type py -g '!tests/**' -g '!docs/**'
  ```

  如有引用 → 保留;如完全无引用 → 也保留(spec 未授权删,最小改动)。

- [ ] **P3-2b. 验证 client.py 仍 import-clean**

  ```bash
  uv run ruff check platforms/xiaohongshu/client.py
  ```

  **预期:** 无 unused import / unused argument 报错(`_set_cookie_collect` 保留就没事;若决定删,需同步改 `_request` 签名 + 删 `_set_cookie_collect` 的 `if _set_cookie_collect is not None` 分支)。

---

### Task P3-3: 删 tests/test_xhs_client.py 对应测试

- [ ] **P3-3a. 删除以下 4 个测试方法**

  ⚠️ **不要依赖固定行号** — 删除任一测试会让后续行号漂移。用 grep 按测试方法名定位:

  ```bash
  rg -n "async def test_create_qrcode|async def test_create_qrcode_payload_is_qr_type_int|async def test_check_qrcode_status|async def test_fetch_sec_cookies_both_succeed" \
     tests/test_xhs_client.py
  ```

  要删的 4 个方法(参考:client.py 未动时大致落在 test_xhs_client.py 272-323 行区域,但 fixer **必须以 grep 实际输出为准**):
  - `test_create_qrcode` (含其上 decorator)
  - `test_create_qrcode_payload_is_qr_type_int` (含其上 decorator)
  - `test_check_qrcode_status` (含其上 decorator)
  - `test_fetch_sec_cookies_both_succeed` (含其上 decorator)

  ⚠️ `test_create_qrcode` + `test_create_qrcode_payload_is_qr_type_int` 都测 `create_qrcode`,全删。共删 4 个方法(注意:任务名虽叫"3 个测试"对应 3 个被删 client 方法,但 create_qrcode 有两个测试,合计 4 个)。

  删完后用 grep 确认 0 残留:

  ```bash
  rg -n "test_create_qrcode|test_check_qrcode_status|test_fetch_sec_cookies_both_succeed" \
     tests/test_xhs_client.py
  # 预期: 0 输出
  ```

  `TestSpecificMethods` 类里应只剩(用 grep 确认,不靠行号):
  - `test_get_user_notes` / `test_get_note_detail` / `test_get_comments` / `test_search_users` / `test_get_user_info` 等
  - `test_probe_*` 系列
  - `test_refresh_cookies_*` 系列

- [ ] **P3-3b. 跑 test_xhs_client.py 看删干净**

  ```bash
  uv run pytest tests/test_xhs_client.py -v
  ```

  **预期:** 所有剩余测试通过,无 `KeyError`/`AttributeError` 残留。

---

### Task P3-4: Phase 3 收尾验证

- [ ] **P3-4a. ruff + pyright + pytest 全量**

  ```bash
  uv run ruff check .
  uv run pyright .
  uv run pytest
  ```

  **预期:**
  - ruff: 0 报错
  - pyright: 0 errors
  - pytest: 全过。数量 = 原数(509) - 删的 4 个 client 测试 + 新增 ~38 个测试(wrapper 8 + authenticator ~30) ≈ 540+

---

## Phase 4: 全量验证 + 真机扫码

### Task P4-1: CI 三件套最终确认

- [ ] **P4-1a. 跑完整三件套并截输出**

  ```bash
  uv run ruff check . && echo "RUFF_OK"
  uv run pyright . && echo "PYRIGHT_OK"
  uv run pytest && echo "PYTEST_OK"
  ```

  **预期:** 三行 OK 全输出。任何 nok 先修。

  记录:
  - ruff 输出 `All checks passed!`
  - pyright 输出 `0 errors, 0 warnings`
  - pytest 输出 `== N passed in Xs ==` (记录 N)

---

### Task P4-2: 真机扫码验证(用户参与,非自动化)

- [ ] **P4-2a. 重启 web 服务**

  ```bash
  # 用项目实际启动命令(参考 web/app.py 或 README),示例:
  uv run uvicorn web.app:app --reload --port 8000
  ```

- [ ] **P4-2b. 用户操作:浏览器打开 http://localhost:8000/auth,点小红书"扫码登录",手机扫码**

  **通过标准(用户确认):**
  1. 二维码图片正常显示(不再 471)
  2. 扫码后状态从 waiting → scanned → success
  3. 页面显示"登录成功" + nickname
  4. config 里 cookie 字段被写入
  5. 30 秒内不再卡 waiting

- [ ] **P4-2c. 失败处理**

  如仍失败:
  - 看 `uvicorn` 控制台日志找 `trawler.xiaohongshu.auth` 的 WARNING/ERROR
  - 看 web 日志找 `🔑 xhs 轮询异常:` 行
  - 抓 `AsyncXhsClient.check_qrcode` 真实返回 dict 字段名,确认是 `code_status` 不是其他变体
  - 若是 IP 风控 → 用户换网络/挂代理重试

  ⚠️ 真机失败**不要**回滚代码 — 先诊断是否 a1 风控(spec §9 风险点)。回滚是最后手段。

---

## 任务总结

| Phase | Task 数 | 性质 | 主要文件 |
|---|---|---|---|
| Phase 1 | 4 (P1-1 ~ P1-4) | 依赖 + 新建 wrapper | pyproject.toml, async_xhs_wrapper.py, test_async_xhs_wrapper.py |
| Phase 2 | 3 (P2-1 ~ P2-3) | 重写 auth | auth.py, test_xhs_authenticator.py |
| Phase 3 | 4 (P3-1 ~ P3-4) | 删废方法 | client.py, test_xhs_client.py |
| Phase 4 | 2 (P4-1 ~ P4-2) | 验证 | 无文件改动 |

**总 task 数:** 13
**总子步骤数:** ~30 (含每个 TDD task 的 RED/GREEN 子步)
**TDD task:** P1-2, P1-3 (一组), P2-1, P2-2 (一组) — 共 2 组 TDD 循环

**回滚策略:** `git revert` 这个 PR 即可。client.py 只删了 3 个方法,monitor/comments/search 不受影响(spec §7.3)。
