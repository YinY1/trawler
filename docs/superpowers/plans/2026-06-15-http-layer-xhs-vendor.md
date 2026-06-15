# HTTP Layer & XHS Vendor Optimization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `vendor/spider_xhs` (44 MB, Node.js-dependent) with pure-Python `xhshow` signing; consolidate all XHS HTTP behind a single `XhsClient` class with real cookie keepalive; delete dead code.

**Architecture:** Five independently-committable phases: (P1) signer rewrite to xhshow pure Python (with xhshow+MediaCrawler API spike), (P2) new `XhsClient` + cookie utils + exception types (with Set-Cookie probe spike), (P3) migrate all callers (monitor/comments/downloader/search) to XhsClient + delete `_fetch_notes_fallback` + update test mocks, (P4) rewrite auth.py with pure-Python QR login via XhsClient, (P5) delete dead vendor code and docs. Each phase passes `ruff`, `pyright`, and `pytest` before moving on.

**Tech Stack:** Python 3.14, aiohttp (no httpx), xhshow v0.1.9+, pytest-asyncio, ruff, pyright

---

## Phase P1: Signer Rewrite (xhshow pure Python)

**Files:**
- Modify: `platforms/xiaohongshu/signer.py` — rewrite `get_xhs_sign()` to pure Python
- Modify: `pyproject.toml` — add `xhshow>=0.1.9` dependency; PRESERVE existing `[xhs]` optional extra
- Delete: `platforms/xiaohongshu/xhs_sign_wrapper.js` — no longer needed after signer rewrite
- Rewrite: `tests/test_xhs_signer.py` — drop Node.js subprocess mocks, add xhshow mocks
- Unchanged: `platforms/xiaohongshu/auth.py` — still uses `signer.get_xhs_sign()` via same signature

### Task P1-1: Spike — read xhshow source + MediaCrawler `playwright_sign.py` to verify GET workaround API surface

- [ ] **Spike: Read xhshow source and MediaCrawler's GET workaround**

  Read these sources to determine the exact API for the GET signing workaround:

  ```bash
  # Find xhshow installation path
  uv run python -c "import xhshow; print(xhshow.__file__)"
  # Read crypto_processor module
  cat "$(uv run python -c "import xhshow.crypto.crypto_processor; print(xhshow.crypto.crypto_processor.__file__)")"
  # Also read the main Xhshow class
  cat "$(dirname "$(uv run python -c "import xhshow; print(xhshow.__file__)")")/xhshow.py"
  ```

  Additionally, locate MediaCrawler's `playwright_sign.py` to confirm the workaround pattern:

  ```bash
  # Find MediaCrawler if available locally, otherwise fetch key snippets from GitHub
  # The target pattern from spec sketch:
  #   content_str = _build_get_content_string(api, data)
  #   payload_array = crypto_processor.build_payload_array(content_str, a1)
  #   headers = _xhs.sign_headers_get_from_array(payload_array)
  ```

  **Spike output (record as comment in implementation, NOT a separate task):** Document the exact method names, argument shapes, and import paths found. If `build_payload_array` or `sign_headers_get_from_array` don't exist with those names, find the equivalents. Write the implementation such that `get_xhs_sign(api, data, a1, method)` public signature stays identical — the spike only determines internal GET path implementation.

  In the final `signer.py`, cite the spike findings as code comments, e.g.:
  ```python
  # API verified via xhshow/crypto/__init__.py:build_payload_array(...)
  # Pattern lifted from MediaCrawler playwright_sign.py
  ```

### Task P1-2: Add xhshow dependency

- [ ] **Add xhshow to pyproject.toml dependencies**

  ```toml
  # in pyproject.toml, add to [project.dependencies]:
  dependencies = [
      ...
      "aiohttp>=3.9",
      "xhshow>=0.1.9",
      ...
  ]
  ```

  PRESERVE the existing `[xhs]` optional extra block — it's used by `downloader.py:_try_xhs_downloader_lib` and is out of scope to remove:
  ```toml
  [project.optional-dependencies]
  xhs = [
      "xhs>=0.1.9",
  ]
  ```

- [ ] **Install and verify xhshow resolves**

  ```bash
  uv pip install -e ".[dev]"
  uv run python -c "import xhshow; print(xhshow.__version__)"
  ```

### Task P1-3: Rewrite signer.py to pure Python

- [ ] **Write failing tests first: new test file for pure-Python signer**

  **File:** `tests/test_xhs_signer.py` (full rewrite)

  ```python
  """Tests for platforms.xiaohongshu.signer — pure Python xhshow-based signing."""

  from __future__ import annotations

  from unittest.mock import patch

  import pytest

  from platforms.xiaohongshu.signer import get_xhs_sign


  class TestGetXhsSign:
      """Tests for get_xhs_sign()."""

      API = "/api/sns/web/v1/login/qrcode/create"

      def test_sign_post_returns_expected_dict(self):
          """Returns dict with xs, xt, xs_common for POST requests."""
          mock_headers = {"x-s": "abc123", "x-t": 1712345678, "x-s-common": "def456"}
          with patch("platforms.xiaohongshu.signer._xhs") as mock_xhs:
              mock_xhs.sign_headers_post.return_value = mock_headers
              result = get_xhs_sign(self.API, data={"k": "v"}, a1="test_a1", method="POST")
          assert result == {"xs": "abc123", "xt": "1712345678", "xs_common": "def456"}

      def test_sign_get_returns_expected_dict(self):
          """Returns dict with xs, xt, xs_common for GET requests (workaround path)."""
          mock_headers = {"x-s": "get_xs", "x-t": 1712345679, "x-s-common": "get_common"}
          with (
              patch("platforms.xiaohongshu.signer._xhs") as mock_xhs,
              patch("platforms.xiaohongshu.signer.crypto_processor") as mock_crypto,
          ):
              mock_crypto.build_payload_array.return_value = ["arr_item1", "arr_item2"]
              mock_xhs.sign_headers_get_from_array.return_value = mock_headers
              result = get_xhs_sign(self.API, data="cursor=&num=20", a1="test_a1", method="GET")
          assert result == {"xs": "get_xs", "xt": "1712345679", "xs_common": "get_common"}
          mock_crypto.build_payload_array.assert_called_once()

      def test_default_method_is_post(self):
          """Defaults to POST when method is not specified."""
          mock_headers = {"x-s": "xs_val", "x-t": 1712345680, "x-s-common": "common_val"}
          with patch("platforms.xiaohongshu.signer._xhs") as mock_xhs:
              mock_xhs.sign_headers_post.return_value = mock_headers
              result = get_xhs_sign(self.API, data="raw_data", a1="a1_val")
          assert result["xs"] == "xs_val"
          mock_xhs.sign_headers_post.assert_called_once()

      def test_empty_data_and_a1(self):
          """Works with empty data and empty a1."""
          mock_headers = {"x-s": "xs_val", "x-t": 1712345680, "x-s-common": "common_val"}
          with patch("platforms.xiaohongshu.signer._xhs") as mock_xhs:
              mock_xhs.sign_headers_post.return_value = mock_headers
              result = get_xhs_sign(self.API)
          assert result["xs"] == "xs_val"
  ```

- [ ] **Run tests to verify they fail**

  ```bash
  uv run pytest tests/test_xhs_signer.py -v
  # Expected: FAIL (4 failed — get_xhs_sign still old implementation, _xhs doesn't exist)
  ```

- [ ] **Write new signer.py implementation**

  **File:** `platforms/xiaohongshu/signer.py` (full rewrite)

  ```python
  """小红书 API 签名模块 — pure Python via xhshow"""

  from __future__ import annotations

  # pyright: basic
  import logging
  from typing import Any

  from xhshow import Xhshow
  from xhshow.crypto import crypto_processor  # API verified via spike: build_payload_array

  logger = logging.getLogger(__name__)

  _xhs = Xhshow()


  def _build_get_content_string(api: str, data: str | dict[str, Any]) -> str:
      """Build content string for GET request signing.

      This replicates the MediaCrawler workaround for the a3_hash bug in
      xhshow.sign_headers_get(). Pattern sourced from MediaCrawler's
      playwright_sign.py — verified via spike.
      """
      if isinstance(data, str):
          return f"{api}?{data}" if data else api
      if data:
          import urllib.parse
          return f"{api}?{urllib.parse.urlencode(data, doseq=True)}"
      return api


  def get_xhs_sign(
      api: str,
      data: str | dict[str, Any] = "",
      a1: str = "",
      method: str = "POST",
  ) -> dict[str, str]:
      """Generate XHS API signature headers via xhshow (pure Python).

      Args:
          api: API path (e.g. '/api/sns/web/v1/login/qrcode/create')
          data: Request data (dict or JSON string for POST, query string for GET)
          a1: a1 cookie value
          method: HTTP method, 'GET' or 'POST'

      Returns:
          Dict with keys: xs, xt, xs_common

      Raises:
          RuntimeError: If signing fails
      """
      cookie_str = f"a1={a1}" if a1 else ""
      payload = data if isinstance(data, str) else data

      try:
          if method == "POST":
              headers = _xhs.sign_headers_post(uri=api, cookies=cookie_str, payload=payload)
          else:
              # GET workaround: xhshow.sign_headers_get() has a3_hash bug
              # Use crypto_processor.build_payload_array directly (MediaCrawler pattern)
              # Verified via xhshow/crypto/crypto_processor.py:build_payload_array(...)
              content_str = _build_get_content_string(api, data)
              payload_array = crypto_processor.build_payload_array(content_str, a1)
              headers = _xhs.sign_headers_get_from_array(payload_array)
      except Exception as exc:
          raise RuntimeError(f"XHS signing failed: {exc}") from exc

      return {
          "xs": headers.get("x-s", ""),
          "xt": str(headers.get("x-t", "")),
          "xs_common": headers.get("x-s-common", ""),
      }
  ```

  Note: The exact method names `sign_headers_get_from_array` and `crypto_processor.build_payload_array` must be verified during the spike. If xhshow exposes them under different names, adapt the implementation.

- [ ] **Run tests to verify they pass**

  ```bash
  uv run pytest tests/test_xhs_signer.py -v
  # Expected: PASS (4 passed)
  ```

### Task P1-4: Delete unused sign wrapper JS file

- [ ] **Delete `xhs_sign_wrapper.js`**

  ```bash
  rm /home/zyw10/proj/trawler/platforms/xiaohongshu/xhs_sign_wrapper.js
  ```

### Task P1-5: Phase verification

- [ ] **Run full verification suite**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright .
  uv run pytest -x
  ```

  Expected: All pass. Tests that reference the old `signer.py` subprocess behavior (like `test_xhs_authenticator.py`) should still work since the `get_xhs_sign` function signature is unchanged.

---

## Phase P2: XhsClient + Cookie Utils + Exception Types

**Files:**
- Create: `shared/cookie_utils.py` — 4 functions: `parse_cookie_str`, `parse_set_cookie_headers` (SimpleCookie + regex fallback), `build_cookie_str`, `extract_cookie_value`
- Create: `platforms/xiaohongshu/client.py` — `XhsClient` class with `__init__(self, cookie: str | dict[str, str], *, session: aiohttp.ClientSession | None = None)`
- Extend: `shared/exceptions.py` — add `DataFetchError`, `IpBlockError`, `CaptchaError`, `RetryableError`
- Create: `tests/test_cookie_utils.py` — new test file
- Create: `tests/test_xhs_client.py` — new test file
- Unchanged: All callers (monitor.py, comments.py, downloader.py, search.py, auth.py still use old patterns)

### Task P2-1: SPIKE — probe xiaohongshu.com/explore with real cookie to determine Set-Cookie behavior

- [ ] **Spike: Probe XHS homepage to check Set-Cookie behavior**

  ```bash
  # Run with a real cookie to confirm whether Set-Cookie headers are returned:
  uv run python -c "
  import asyncio, aiohttp, os
  from shared.config import load_config
  config = load_config()
  cookie = config.xiaohongshu.auth.cookie or os.environ.get('XHS_COOKIE', '')
  if not cookie:
      print('SKIP: no real cookie available')
      exit(0)
  async def probe():
      async with aiohttp.ClientSession() as s:
          async with s.get('https://www.xiaohongshu.com/explore',
                           headers={'User-Agent': 'Mozilla/5.0 ...', 'Cookie': cookie}) as r:
              print(f'Status: {r.status}')
              sc = r.headers.getall('Set-Cookie', [])
              print(f'Set-Cookie count: {len(sc)}')
              for h in sc:
                  print(f'  {h[:120]}')
              if not sc:
                  print('NO Set-Cookie headers — refresh_cookies will fall back to timestamp-only extension')
  asyncio.run(probe())
  "
  ```

  **Spike outcome (record as comment in client.py):** Document whether Set-Cookie headers are returned. If yes, `XhsClient.refresh_cookies()` parses them via `parse_set_cookie_headers`. If no, `refresh_cookies()` only bumps the `expires_at` timestamp (fallback path).

### Task P2-2: Extend shared/exceptions.py with 4 new exception types

- [ ] **Write failing tests first**

  Add to `tests/test_xhs_client.py`:

  ```python
  """Tests for XhsClient — exception hierarchy."""

  from __future__ import annotations

  from shared.exceptions import CaptchaError, DataFetchError, IpBlockError, RetryableError, TrawlerError


  class TestErrorHierarchy:
      def test_data_fetch_is_trawler_error(self):
          assert issubclass(DataFetchError, TrawlerError)

      def test_ip_block_is_trawler_error(self):
          assert issubclass(IpBlockError, TrawlerError)

      def test_captcha_is_trawler_error(self):
          assert issubclass(CaptchaError, TrawlerError)

      def test_retryable_is_trawler_error(self):
          assert issubclass(RetryableError, TrawlerError)
  ```

- [ ] **Run test to verify it fails**

  ```bash
  uv run pytest tests/test_xhs_client.py::TestErrorHierarchy -v
  # Expected: FAIL — DataFetchError, IpBlockError etc not defined yet
  ```

- [ ] **Add exception classes to shared/exceptions.py**

  **File:** `shared/exceptions.py` — add after `NotFoundError` (before the `async_retry` section):

  ```python
  class DataFetchError(TrawlerError):
      """200 OK 但 data.success == false，或 4xx（非特殊状态码）"""


  class IpBlockError(TrawlerError):
      """IP 被小红书风控拦截（HTTP 200 + code 300012）"""


  class CaptchaError(TrawlerError):
      """触发了验证码（HTTP 461 / 471）"""


  class RetryableError(TrawlerError):
      """可重试的临时错误（HTTP 403 / 429 / 5xx / 网络异常）"""
  ```

  All four inherit `TrawlerError` directly (not `HttpError`).

- [ ] **Run tests to verify they pass**

  ```bash
  uv run pytest tests/test_xhs_client.py::TestErrorHierarchy -v
  # Expected: PASS
  ```

### Task P2-3: Create shared/cookie_utils.py (4 functions)

- [ ] **Write failing tests first**

  **File:** `tests/test_cookie_utils.py`

  ```python
  """Tests for shared.cookie_utils."""

  from __future__ import annotations

  from shared.cookie_utils import build_cookie_str, extract_cookie_value, parse_cookie_str, parse_set_cookie_headers


  class TestParseCookieStr:
      def test_basic(self):
          result = parse_cookie_str("a1=hello; web_session=abc123")
          assert result == {"a1": "hello", "web_session": "abc123"}

      def test_empty_string(self):
          assert parse_cookie_str("") == {}

      def test_trailing_semicolon(self):
          assert parse_cookie_str("a1=v1;") == {"a1": "v1"}

      def test_whitespace_handling(self):
          assert parse_cookie_str("  a1=hello  ;  web_session=abc  ") == {"a1": "hello", "web_session": "abc"}

      def test_accepts_dict_identity(self):
          assert parse_cookie_str({"a1": "v1"}) == {"a1": "v1"}  # pass-through


  class TestParseSetCookieHeaders:
      def test_single(self):
          result = parse_set_cookie_headers(["web_session=abc123; Path=/; HttpOnly"])
          assert result["web_session"] == "abc123"

      def test_multiple(self):
          headers = [
              "web_session=abc123; Path=/; HttpOnly",
              "gid=gjid456; Path=/; Domain=.xiaohongshu.com",
          ]
          result = parse_set_cookie_headers(headers)
          assert result["web_session"] == "abc123"
          assert result["gid"] == "gjid456"

      def test_empty_list(self):
          assert parse_set_cookie_headers([]) == {}

      def test_malformed_header_falls_back_to_regex(self):
          # XHS sometimes sends malformed cookies that SimpleCookie silently drops
          result = parse_set_cookie_headers(["a1= value with spaces; Path=/", "good=ok; Path=/"])
          assert result.get("a1") == "value with spaces"
          assert result.get("good") == "ok"


  class TestBuildCookieStr:
      def test_basic(self):
          result = build_cookie_str({"a1": "hello", "web_session": "abc"})
          assert result == "a1=hello; web_session=abc"

      def test_empty_dict(self):
          assert build_cookie_str({}) == ""

      def test_with_str_input(self):
          result = build_cookie_str("a1=hello; web_session=abc")
          assert result == "a1=hello; web_session=abc"


  class TestExtractCookieValue:
      def test_from_string(self):
          assert extract_cookie_value("a1=v1; web_session=abc", "a1") == "v1"

      def test_from_dict(self):
          assert extract_cookie_value({"a1": "v1", "web_session": "abc"}, "a1") == "v1"

      def test_missing_returns_empty(self):
          assert extract_cookie_value("a1=v1", "web_session") == ""

      def test_empty_input(self):
          assert extract_cookie_value("", "a1") == ""
          assert extract_cookie_value({}, "a1") == ""
  ```

- [ ] **Run tests to verify they fail**

  ```bash
  uv run pytest tests/test_cookie_utils.py -v
  # Expected: FAIL — shared.cookie_utils module not found
  ```

- [ ] **Implement shared/cookie_utils.py**

  **File:** `shared/cookie_utils.py`

  ```python
  """Cookie utility functions — parse, build, extract, and manage cookie values.

  Uses stdlib http.cookies.SimpleCookie for robust Set-Cookie header parsing,
  with a regex fallback for malformed entries that SimpleCookie silently drops.
  """

  from __future__ import annotations

  import re
  from http.cookies import SimpleCookie


  def parse_cookie_str(cookie_str: str | dict[str, str]) -> dict[str, str]:
      """Parse a Cookie header string into a dict.

      Args:
          cookie_str: Raw Cookie header value (e.g. "a1=v1; web_session=abc")
                      or an existing dict (returned as-is).

      Returns:
          Dict mapping cookie names to values.
      """
      if isinstance(cookie_str, dict):
          return dict(cookie_str)
      if not cookie_str:
          return {}
      result: dict[str, str] = {}
      for part in cookie_str.split(";"):
          part = part.strip()
          if "=" in part:
              k, v = part.split("=", 1)
              result[k.strip()] = v.strip()
      return result


  def parse_set_cookie_headers(headers: list[str]) -> dict[str, str]:
      """Parse Set-Cookie response headers into a dict.

      Uses http.cookies.SimpleCookie first, then falls back to a manual
      regex parser for malformed entries that SimpleCookie silently drops
      (XHS sometimes sends cookies with spaces around '=' or other edge cases).

      Args:
          headers: List of raw Set-Cookie header values.

      Returns:
          Dict mapping cookie names to values (only name=value, not attrs).
      """
      result: dict[str, str] = {}

      # Phase 1: stdlib SimpleCookie
      for header in headers:
          try:
              cookie = SimpleCookie(header)
              for key, morsel in cookie.items():
                  result[key] = morsel.value
          except Exception:
              pass

      # Phase 2: regex fallback for entries SimpleCookie missed
      # Matches patterns like "name=value" or "name= value with spaces"
      for header in headers:
          if "=" not in header:
              continue
          name, _, rest = header.partition("=")
          name = name.strip()
          if name and name not in result:
              # Extract value up to first ';' or end of string
              value_match = re.match(r'[^;]*', rest.strip())
              if value_match:
                  result[name] = value_match.group(0).strip()

      return result


  def build_cookie_str(cookies: str | dict[str, str]) -> str:
      """Build a Cookie header string from a dict or pass through a string.

      Args:
          cookies: Dict mapping cookie names to values, or an existing string.

      Returns:
          Cookie header string (e.g. "a1=v1; web_session=abc").
      """
      if isinstance(cookies, str):
          return cookies
      return "; ".join(f"{k}={v}" for k, v in cookies.items())


  def extract_cookie_value(cookie_str_or_dict: str | dict[str, str], name: str) -> str:
      """Return the value of a single cookie by name.

      Consolidates the duplicated ``_extract_a1()`` helpers previously
      scattered across monitor.py and search.py.

      Args:
          cookie_str_or_dict: Cookie string ("k1=v1; k2=v2") or dict.
          name: Cookie name to extract (e.g. "a1").

      Returns:
          The cookie value, or empty string if not found.
      """
      cookies = parse_cookie_str(cookie_str_or_dict)
      return cookies.get(name, "")
  ```

- [ ] **Run tests to verify they pass**

  ```bash
  uv run pytest tests/test_cookie_utils.py -v
  # Expected: PASS
  ```

### Task P2-4: Create XhsClient class

- [ ] **Write failing tests first**

  **File:** `tests/test_xhs_client.py` (full content)

  ```python
  """Tests for XhsClient."""

  from __future__ import annotations

  from unittest.mock import AsyncMock, MagicMock, patch

  import pytest

  from platforms.xiaohongshu.client import XhsClient
  from shared.exceptions import CaptchaError, DataFetchError, IpBlockError, RetryableError


  def _mock_response(status: int = 200, json_data: dict | None = None) -> MagicMock:
      """Create an async context manager mock for aiohttp responses."""
      resp = MagicMock()
      resp.status = status
      resp.__aenter__ = AsyncMock(return_value=resp)
      resp.__aexit__ = AsyncMock(return_value=None)
      if json_data is not None:
          resp.json = AsyncMock(return_value=json_data)
      resp.headers = {}
      return resp


  class TestErrorHierarchy:
      """Exception hierarchy tests (from P2-2)."""

      def test_data_fetch_is_trawler_error(self):
          assert issubclass(DataFetchError, __import__("shared.exceptions", fromlist=["TrawlerError"]).TrawlerError)

      def test_ip_block_is_trawler_error(self):
          assert issubclass(IpBlockError, __import__("shared.exceptions", fromlist=["TrawlerError"]).TrawlerError)

      def test_captcha_is_trawler_error(self):
          assert issubclass(CaptchaError, __import__("shared.exceptions", fromlist=["TrawlerError"]).TrawlerError)

      def test_retryable_is_trawler_error(self):
          assert issubclass(RetryableError, __import__("shared.exceptions", fromlist=["TrawlerError"]).TrawlerError)


  class TestInit:
      def test_creates_session_with_cookie_str(self):
          client = XhsClient(cookie="a1=test")
          assert "a1=test" in client._cookie_str
          assert client._session is not None

      def test_creates_session_with_cookie_dict(self):
          client = XhsClient(cookie={"a1": "test", "web_session": "abc"})
          assert "a1=test" in client._cookie_str
          assert "web_session=abc" in client._cookie_str

      def test_accepts_injected_session(self):
          mock_session = MagicMock()
          client = XhsClient(cookie="a1=test", session=mock_session)
          assert client._session is mock_session

      def test_empty_cookie(self):
          client = XhsClient(cookie="")
          assert client._cookie_str == ""


  class TestRequest:
      API = "/api/sns/web/v1/user_posted"

      @pytest.mark.asyncio
      async def test_post_success(self):
          mock_resp = _mock_response(status=200, json_data={"success": True, "data": {"notes": ["n1"]}})
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              result = await client._request("POST", self.API, json={"k": "v"})

          assert result == {"notes": ["n1"]}

      @pytest.mark.asyncio
      async def test_get_success(self):
          mock_resp = _mock_response(status=200, json_data={"success": True, "data": {"items": []}})
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              result = await client._request("GET", self.API, params={"num": "20"})

          assert result == {"items": []}

      @pytest.mark.asyncio
      async def test_unsuccessful_raises_data_fetch_error(self):
          mock_resp = _mock_response(status=200, json_data={"success": False, "msg": "rate limited"})
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(DataFetchError, match="rate limited"):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_ip_block_300012(self):
          mock_resp = _mock_response(status=200, json_data={"success": False, "code": 300012, "msg": "风险操作"})
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(IpBlockError):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_captcha_461(self):
          mock_resp = _mock_response(status=461)
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(CaptchaError):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_retryable_429(self):
          mock_resp = _mock_response(status=429)
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(RetryableError):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_retryable_5xx(self):
          mock_resp = _mock_response(status=503)
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(RetryableError):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_client_error_retryable(self):
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(side_effect=RuntimeError("Connection refused"))
          client._session.post = AsyncMock(side_effect=RuntimeError("Connection refused"))

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(RetryableError):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_403_raises_retryable(self):
          mock_resp = _mock_response(status=403)
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(RetryableError):
                  await client._request("GET", self.API)

      @pytest.mark.asyncio
      async def test_4xx_other_raises_data_fetch_error(self):
          mock_resp = _mock_response(status=400)
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(return_value=mock_resp)
          client._session.post = AsyncMock(return_value=mock_resp)

          with patch("platforms.xiaohongshu.client.get_xhs_sign") as mock_sign:
              mock_sign.return_value = {"xs": "x", "xt": "1", "xs_common": "c"}
              with pytest.raises(DataFetchError):
                  await client._request("GET", self.API)


  class TestApiMethods:
      @pytest.mark.asyncio
      async def test_get_user_notes(self):
          client = XhsClient(cookie="a1=test")
          client._request = AsyncMock(return_value={"notes": [{"note_id": "123"}]})
          result = await client.get_user_notes("user_1")
          assert result == [{"note_id": "123"}]
          client._request.assert_called_once_with(
              "GET", "/api/sns/web/v1/user_posted",
              params={"num": 20, "cursor": "", "user_id": "user_1", "image_formats": "jpg,webp,avif", "xsec_token": "", "xsec_source": "pc_feed"},
          )

      @pytest.mark.asyncio
      async def test_get_note_detail(self):
          client = XhsClient(cookie="a1=test")
          client._request = AsyncMock(return_value={"items": [{}]})
          result = await client.get_note_detail("note_1", "xsec_abc")
          assert result is not None

      @pytest.mark.asyncio
      async def test_get_comments(self):
          client = XhsClient(cookie="a1=test")
          client._request = AsyncMock(return_value={"comments": [], "has_more": False, "cursor": ""})
          result = await client.get_comments("note_1")
          assert result == {"comments": [], "has_more": False, "cursor": ""}

      @pytest.mark.asyncio
      async def test_search_notes(self):
          client = XhsClient(cookie="a1=test")
          client._request = AsyncMock(return_value={})
          result = await client.search_notes("keyword")
          assert result is not None

      @pytest.mark.asyncio
      async def test_probe_true(self):
          client = XhsClient(cookie="a1=test")
          client._request = AsyncMock(return_value={"nickname": "test_user"})
          assert await client.probe() is True

      @pytest.mark.asyncio
      async def test_probe_false_on_error(self):
          client = XhsClient(cookie="a1=test")
          client._request = AsyncMock(side_effect=Exception("fail"))
          assert await client.probe() is False

      @pytest.mark.asyncio
      async def test_close_owned_session(self):
          client = XhsClient(cookie="a1=test")
          client._session.close = AsyncMock()
          await client.close()
          client._session.close.assert_called_once()

      @pytest.mark.asyncio
      async def test_close_does_not_close_injected_session(self):
          mock_session = MagicMock()
          mock_session.close = AsyncMock()
          client = XhsClient(cookie="a1=test", session=mock_session)
          await client.close()
          mock_session.close.assert_not_called()


  class TestRefreshCookies:
      @pytest.mark.asyncio
      async def test_returns_new_cookies(self):
          client = XhsClient(cookie="a1=test")
          mock_resp = _mock_response(status=200, json_data={})
          mock_resp.headers = {}
          client._session.get = AsyncMock(return_value=mock_resp)

          with patch("shared.cookie_utils.parse_set_cookie_headers", return_value={"a1": "new_a1"}):
              result = await client.refresh_cookies()
          assert result == {"a1": "new_a1"}

      @pytest.mark.asyncio
      async def test_returns_none_when_no_set_cookie(self):
          client = XhsClient(cookie="a1=test")
          mock_resp = _mock_response(status=200, json_data={})
          mock_resp.headers = {}
          client._session.get = AsyncMock(return_value=mock_resp)

          with patch("shared.cookie_utils.parse_set_cookie_headers", return_value={}):
              result = await client.refresh_cookies()
          assert result is None

      @pytest.mark.asyncio
      async def test_returns_none_on_http_error(self):
          client = XhsClient(cookie="a1=test")
          client._session.get = AsyncMock(side_effect=Exception("err"))
          result = await client.refresh_cookies()
          assert result is None
  ```

- [ ] **Run tests to verify they fail**

  ```bash
  uv run pytest tests/test_xhs_client.py -v
  # Expected: FAIL — XhsClient not defined
  ```

- [ ] **Implement XhsClient**

  **File:** `platforms/xiaohongshu/client.py`

  ```python
  """XhsClient — single HTTP entry point for all Xiaohongshu API calls.

  Owns one long-lived aiohttp.ClientSession (or accepts one via DI).
  Signs every request via signer.get_xhs_sign(). Translates server errors
  to typed exceptions (IpBlockError, CaptchaError, RetryableError, DataFetchError).
  """

  from __future__ import annotations

  # pyright: basic
  import logging
  from typing import Any

  import aiohttp

  from platforms.xiaohongshu.signer import get_xhs_sign
  from shared.constants import XHS_REQUEST_TIMEOUT
  from shared.cookie_utils import build_cookie_str, extract_cookie_value, parse_cookie_str, parse_set_cookie_headers
  from shared.exceptions import CaptchaError, DataFetchError, IpBlockError, RetryableError

  logger = logging.getLogger(__name__)

  # NOTE: These constants are defined HERE (not imported from auth.py) because P4-2
  # rewrites auth.py to use XhsClient for all HTTP and drops them. The current
  # auth.py defines them (and uses them in _fetch_sec_cookies / validate_tokens /
  # get_request_headers), but those code paths are deleted in P3-5/P4-2. Importing
  # them from auth.py would create a circular dependency (auth.py -> client.py ->
  # auth.py) after P4-2 (which adds `from platforms.xiaohongshu.client import XhsClient`).
  # Defining them locally keeps client.py standalone and avoids the import break.
  DEFAULT_USER_AGENT = (
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
  )
  XHS_API_BASE = "https://edith.xiaohongshu.com"
  XHS_HOME_URL = "https://www.xiaohongshu.com"


  class XhsClient:
      """Single HTTP client for all XHS API interactions.

      Args:
          cookie: Cookie as string ("k1=v1; k2=v2") or dict ({"k1": "v1"}).
          session: Optional aiohttp.ClientSession for dependency injection.
                   If provided, the client does NOT own/close this session.
                   If None, the client creates and owns its own session.
      """

      def __init__(
          self,
          cookie: str | dict[str, str],
          *,
          session: aiohttp.ClientSession | None = None,
      ) -> None:
          self._cookie_str = build_cookie_str(parse_cookie_str(cookie))
          self._owns_session = session is None
          self._session = session or aiohttp.ClientSession(trust_env=False)

      async def close(self) -> None:
          """Close the owned session. Does NOT close an injected session."""
          if self._owns_session and self._session and not self._session.closed:
              await self._session.close()

      # ── Internal request method ──────────────────────────────────

      async def _request(
          self,
          method: str,
          api: str,
          *,
          params: dict[str, Any] | None = None,
          json: dict[str, Any] | str | None = None,
      ) -> dict[str, Any]:
          """Sign, send, parse, raise on error.

          Args:
              method: HTTP method ("GET" or "POST")
              api: API path (e.g. "/api/sns/web/v1/user_posted")
              params: Query parameters (for GET requests)
              json: JSON body (for POST requests)

          Returns:
              Parsed JSON response data (from response["data"]).

          Raises:
              IpBlockError: IP blocked (code 300012)
              CaptchaError: Captcha required (HTTP 461/471)
              RetryableError: Temporary failure (HTTP 403/429/5xx/network)
              DataFetchError: Other API errors
          """
          a1 = extract_cookie_value(self._cookie_str, "a1")
          data_for_sign: str | dict[str, Any]

          if method == "GET" and params:
              from urllib.parse import urlencode
              query = urlencode(params, doseq=True, safe=",")
              full_api = f"{api}?{query}"
              # NOTE: sign the BASE api path (no query), pass query separately as `data`.
              # signer internally reconstructs `f"{api}?{data}"` for GET content string.
              # If we passed full_api here, the query would be duplicated (`/path?k=v?k=v`).
              sign_api = api
              data_for_sign = query
          elif method == "GET" and not params:
              full_api = api
              sign_api = api
              data_for_sign = ""
          else:
              full_api = api
              sign_api = api
              data_for_sign = json if json is not None else ""

          sign = get_xhs_sign(sign_api, data=data_for_sign, a1=a1, method=method)
          headers: dict[str, str] = {
              "User-Agent": DEFAULT_USER_AGENT,
              "Origin": XHS_HOME_URL,
              "Referer": f"{XHS_HOME_URL}/",
              "x-s": sign["xs"],
              "x-t": sign["xt"],
              "x-s-common": sign["xs_common"],
              "Cookie": self._cookie_str,
          }
          if method == "POST":
              headers["Content-Type"] = "application/json;charset=UTF-8"

          try:
              if method == "GET":
                  async with self._session.get(
                      XHS_API_BASE + full_api,
                      headers=headers,
                      timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
                  ) as resp:
                      return await self._handle_response(resp)
              else:
                  async with self._session.post(
                      XHS_API_BASE + full_api,
                      headers=headers,
                      json=json if isinstance(json, dict) else None,
                      data=json if isinstance(json, str) else None,
                      timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
                  ) as resp:
                      return await self._handle_response(resp)
          except (IpBlockError, CaptchaError, DataFetchError, RetryableError):
              raise
          except Exception as exc:
              raise RetryableError(str(exc)) from exc

      async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
          """Parse and validate the HTTP response."""
          status = resp.status

          if status in (461, 471):
              raise CaptchaError(f"Captcha required (HTTP {status})")
          if status in (403, 429):
              raise RetryableError(f"Rate limited or forbidden (HTTP {status})")
          if status >= 500:
              raise RetryableError(f"Server error (HTTP {status})")
          if status >= 400:
              raise DataFetchError(f"HTTP {status}")

          data = await resp.json(content_type=None)

          if not data.get("success", True):
              code = data.get("code", 0)
              msg = data.get("msg", "unknown")
              if code == 300012:
                  raise IpBlockError(msg)
              raise DataFetchError(msg)

          return data.get("data", {})

      # ── Content APIs ────────────────────────────────────────────

      async def get_user_notes(self, user_id: str, cursor: str = "", num: int = 20) -> list[dict[str, Any]]:
          """GET /api/sns/web/v1/user_posted"""
          data = await self._request(
              "GET", "/api/sns/web/v1/user_posted",
              params={
                  "num": str(num), "cursor": cursor, "user_id": user_id,
                  "image_formats": "jpg,webp,avif", "xsec_token": "", "xsec_source": "pc_feed",
              },
          )
          return data.get("notes", [])

      async def get_note_detail(self, note_id: str, xsec_token: str) -> dict[str, Any] | None:
          """GET /api/sns/web/v1/feed"""
          data = await self._request(
              "GET", "/api/sns/web/v1/feed",
              params={
                  "source_note_id": note_id, "image_scenes": ["CRD_WM_WEBP"],
                  "xsec_source": "pc_share", "xsec_token": xsec_token,
              },
          )
          items = data.get("items", [])
          if items and isinstance(items, list):
              return items[0].get("note_card", items[0])
          return None

      async def get_comments(self, note_id: str, cursor: str = "") -> dict[str, Any]:
          """GET /api/sns/web/v2/comment/page"""
          return await self._request(
              "GET", "/api/sns/web/v2/comment/page",
              params={"note_id": note_id, "cursor": cursor, "top_comment_id": "", "image_scenes": ""},
          )

      async def search_notes(self, keyword: str, cursor: str = "") -> dict[str, Any]:
          """GET /api/sns/web/v1/search/notes"""
          return await self._request(
              "GET", "/api/sns/web/v1/search/notes",
              params={"keyword": keyword, "cursor": "page", "page": cursor, "search_id": "", "sort": "general", "note_type": 0},
          )

      # ── Auth APIs ──────────────────────────────────────────────

      async def get_user_info(self) -> dict[str, Any]:
          """GET /api/sns/web/v2/user/me — used by probe() and QR login verification."""
          return await self._request("GET", "/api/sns/web/v2/user/me")

      async def create_qrcode(self, init_cookies: dict[str, str]) -> dict[str, Any]:
          """POST /api/sns/web/v1/login/qrcode/create"""
          old_cookie = self._cookie_str
          self._cookie_str = build_cookie_str(init_cookies)
          try:
              return await self._request("POST", "/api/sns/web/v1/login/qrcode/create", json={})
          finally:
              self._cookie_str = old_cookie

      async def check_qrcode_status(self, qr_id: str, code: str) -> dict[str, Any]:
          """GET /api/sns/web/v1/login/qrcode/status"""
          return await self._request(
              "GET", "/api/sns/web/v1/login/qrcode/status",
              params={"qr_id": qr_id, "code": code},
          )

      async def fetch_sec_cookies(self, init_cookies: dict[str, str]) -> dict[str, str]:
          """Fetch sec_poison_id and gid for initial cookies.

          Called before QR generation. Returns dict that may contain
          sec_poison_id and/or gid (best-effort).
          """
          result: dict[str, str] = {}
          old_cookie = self._cookie_str
          self._cookie_str = build_cookie_str(init_cookies)
          try:
              # POST /api/sec/v1/scripting
              try:
                  data = {"callFrom": "web", "callback": "", "type": "ds", "appId": "xhs-pc-web"}
                  script_resp = await self._request("POST", "/api/sec/v1/scripting", json=data)
                  sec_id = script_resp.get("secPoisonId", "")
                  if sec_id:
                      result["sec_poison_id"] = sec_id
              except Exception:
                  pass
              # POST /api/sec/v1/shield/webprofile
              try:
                  data2 = {"platform": "Windows", "sdkVersion": "4.3.5", "svn": "2", "profileData": ""}
                  await self._request("POST", "/api/sec/v1/shield/webprofile", json=data2)
                  # gid is typically returned as Set-Cookie; refresh_cookies captures it
              except Exception:
                  pass
          finally:
              self._cookie_str = old_cookie
          return result

      # ── Lifecycle ───────────────────────────────────────────────

      async def probe(self) -> bool:
          """True iff cookie still accepted by server."""
          try:
              data = await self.get_user_info()
              return bool(data.get("nickname"))
          except Exception:
              return False

      async def refresh_cookies(self) -> dict[str, str] | None:
          """GET /explore, parse Set-Cookie headers, return merged dict (or None).

          Set-Cookie behavior confirmed via probe spike (P2-1). If the server
          returns Set-Cookie headers, they are parsed via parse_set_cookie_headers.
          If not (spike outcome), this method returns None and the caller
          only bumps expires_at.
          """
          try:
              headers = {
                  "User-Agent": DEFAULT_USER_AGENT,
                  "Cookie": self._cookie_str,
              }
              async with self._session.get(
                  XHS_HOME_URL + "/explore",
                  headers=headers,
                  timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
              ) as resp:
                  if resp.status != 200:
                      return None
                  set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                  if not set_cookie_headers:
                      return None
                  return parse_set_cookie_headers(set_cookie_headers)
          except Exception:
              logger.debug("cookie refresh failed", exc_info=True)
              return None
  ```

- [ ] **Run tests to verify they pass**

  ```bash
  uv run pytest tests/test_xhs_client.py -v
  # Expected: PASS
  ```

### Task P2-5: Phase verification

- [ ] **Run full verification suite**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright .
  uv run pytest -x
  ```

  Expected: All pass. Existing callers still use old patterns (not yet migrated), but no imports are broken since `client.py` and `cookie_utils.py` are new.

---

## Phase P3: Migrate Callers to XhsClient

**Files:**
- Modify: `platforms/xiaohongshu/monitor.py` — use `XhsClient` instead of inline aiohttp + get_xhs_sign; delete `_fetch_notes_fallback()` entirely; delete `_extract_a1()` (now in shared/cookie_utils.py)
- Modify: `platforms/xiaohongshu/comments.py` — use `XhsClient`
- Modify: `platforms/xiaohongshu/downloader.py` — use `XhsClient` for feed API (keep aiohttp for raw CDN download)
- Modify: `platforms/xiaohongshu/search.py` — use `XhsClient`; delete `_extract_a1()` (now in shared/cookie_utils.py)
- Unchanged: `platforms/xiaohongshu/auth.py` — dead signing functions (`get_signed_params`, `_local_sign`, `_try_vendor_sign`, `get_request_headers`) become unreachable after callers migrate, but `auth.py` is NOT modified in P3. The full `auth.py` rewrite happens in P4-2, which subsumes any cleanup. See P3-5 for rationale.
- Update: `platforms/xiaohongshu/handlers.py` — imports may need updating
- Update: `tests/test_xhs_search.py` — mock `XhsClient._request` instead of `aiohttp.ClientSession`

### Task P3-1: Migrate monitor.py to XhsClient

- [ ] **Update `_fetch_notes_via_api` to use XhsClient, delete `_fetch_notes_fallback`**

  **File:** `platforms/xiaohongshu/monitor.py` — replace `_fetch_notes_via_api` body, delete `_fetch_notes_fallback` entirely:

  - Delete the entire `_fetch_notes_fallback` function (lines 191-247) — it makes unsigned requests that no longer work.
  - Delete `_extract_a1` function (lines 31-38) — now in `shared.cookie_utils.extract_cookie_value`.
  - Replace `_fetch_notes_via_api` to use `XhsClient`:

  ```python
  async def _fetch_notes_via_api(
      user_id: str,
      cookie: str,
      cursor: str = "",
      num: int = DEFAULT_PAGE_SIZE,
  ) -> list[dict[str, Any]]:
      """通过小红书 API 获取用户笔记列表 (via XhsClient).

      Args:
          user_id: 小红书用户 ID
          cookie: Cookie 字符串
          cursor: 分页游标
          num: 每页数量

      Returns:
          笔记数据列表
      """
      from platforms.xiaohongshu.client import XhsClient

      client = XhsClient(cookie=cookie)
      try:
          return await client.get_user_notes(user_id, cursor=cursor, num=num)
      except Exception as e:
          logger.warning(f"小红书笔记列表 API 请求异常: {e}")
          return []
      finally:
          await client.close()
  ```

  - Remove old imports no longer used: `urlencode`, `get_xhs_sign`, `aiohttp` (check if any other code uses them; if `fetch_user_notes` only calls `_fetch_notes_via_api`, remove them).
  - In `fetch_user_notes` (line 250), remove the `_fetch_notes_fallback` call path — only try `_fetch_notes_via_api` now.

- [ ] **Verify with pyright and run tests**

  ```bash
  uv run ruff check .
  uv run pyright .
  uv run pytest -x
  ```

### Task P3-2: Migrate comments.py to XhsClient

- [ ] **Rewrite `fetch_xhs_comment_highlights` to use XhsClient**

  **File:** `platforms/xiaohongshu/comments.py`

  - Remove imports: `aiohttp`, `XHS_BASE_URL`, `get_request_headers`, `get_signed_params`
  - Remove `COMMENT_API` constant
  - Replace the function body with `XhsClient` usage (same pattern as monitor.py: create XhsClient, call method, close):

  ```python
  async def fetch_xhs_comment_highlights(
      note_id: str,
      config: Config,
      *,
      author_user_id: str = "",
      max_count: int = MAX_HIGHLIGHT_COMMENTS,
  ) -> list[CommentHighlight]:
      """获取小红书笔记的评论亮点（热门评论）..."""
      from platforms.xiaohongshu.client import XhsClient

      cookie = get_xhs_cookie(config)
      if not cookie:
          logger.debug(f"[评论] 缺少 Cookie，跳过评论抓取: {note_id}")
          return []

      client = XhsClient(cookie=cookie)
      try:
          data = await client.get_comments(note_id)
      except Exception as e:
          logger.debug(f"[评论] 请求失败: {e}, note_id: {note_id}")
          return []
      finally:
          await client.close()

      comments_raw = data.get("comments", [])
      if not isinstance(comments_raw, list):
          return []

      all_comments: list[CommentHighlight] = []
      for raw in comments_raw:
          comment = _parse_comment(raw, author_user_id)
          if comment is None or comment.is_author:
              continue
          all_comments.append(comment)

      # Pagination — try second page if needed
      has_more = data.get("has_more", False)
      cursor = data.get("cursor", "")
      if has_more and cursor and len(all_comments) < max_count:
          try:
              data2 = await client.get_comments(note_id, cursor=cursor)
              for raw in data2.get("comments", []):
                  comment = _parse_comment(raw, author_user_id)
                  if comment is None or comment.is_author:
                      continue
                  all_comments.append(comment)
          except Exception:
              pass

      all_comments.sort(key=lambda c: c.like_count, reverse=True)
      result = all_comments[:max_count]
      logger.info(f"[评论] 获取到 {len(result)} 条热门评论, note_id: {note_id}")
      return result
  ```

- [ ] **Verify with pyright and run tests**

  ```bash
  uv run ruff check .
  uv run pyright .
  uv run pytest -x
  ```

### Task P3-3: Migrate downloader.py to XhsClient (feed API only)

- [ ] **Rewrite `_fetch_note_detail` to use XhsClient**

  **File:** `platforms/xiaohongshu/downloader.py`

  - Remove imports: `get_request_headers`, `get_signed_params` from `auth` imports (keep `get_xhs_cookie`, `XHS_BASE_URL` if still used)
  - Replace `_fetch_note_detail` body:

  ```python
  async def _fetch_note_detail(note: NoteInfo, cookie: str) -> Optional[dict[str, Any]]:
      """直接请求笔记详情 API (via XhsClient)."""
      from platforms.xiaohongshu.client import XhsClient

      client = XhsClient(cookie=cookie)
      try:
          return await client.get_note_detail(note.note_id, note.xsec_token)
      except Exception as e:
          logger.debug(f"获取笔记详情失败: {e}")
          return None
      finally:
          await client.close()
  ```

  - Keep `aiohttp` import — `_download_file` still uses its own `aiohttp.ClientSession` for raw CDN binary downloads (this is intentional).
  - Remove `NOTE_FEED_API` constant if no longer used.
  - Remove `urljoin` import if only used in `_fetch_note_detail` (check `_try_xhs_downloader_lib` and `_try_direct_download` — they may also use it).

- [ ] **Verify with pyright and run tests**

  ```bash
  uv run ruff check .
  uv run pyright .
  uv run pytest -x
  ```

### Task P3-4: Migrate search.py to XhsClient

- [ ] **Rewrite `search_xhs_user_by_name` to use XhsClient**

  **File:** `platforms/xiaohongshu/search.py`

  - Remove imports: `aiohttp`, `DEFAULT_USER_AGENT`, `XHS_API_BASE`, `XHS_HOME_URL`, `get_xhs_sign`
  - Remove `_extract_a1` function (now in `shared.cookie_utils.extract_cookie_value`)
  - Keep `_generate_search_id`, `_generate_search_request_id`, `_int_to_base36` helpers
  - Keep `SEARCH_USER_API` constant (still used in `_request` call)

  ```python
  async def search_xhs_user_by_name(
      cookie: str,
      query: str,
      page: int = 1,
  ) -> list[dict[str, Any]]:
      """通过昵称搜索小红书用户 (via XhsClient)."""
      from platforms.xiaohongshu.client import XhsClient
      from shared.cookie_utils import extract_cookie_value

      a1 = extract_cookie_value(cookie, "a1")
      if not a1:
          logger.warning("小红书搜索缺少 a1 cookie")
          return []

      data = {
          "search_user_request": {
              "keyword": query,
              "search_id": _generate_search_id(),
              "page": page,
              "page_size": 15,
              "biz_type": "web_search_user",
              "request_id": _generate_search_request_id(),
          }
      }
      data_json = json.dumps(data, separators=(",", ":"), ensure_ascii=False)

      client = XhsClient(cookie=cookie)
      try:
          data_dict = await client._request("POST", "/api/sns/web/v1/search/usersearch", json=data)
          users = data_dict.get("users", [])
          return users if isinstance(users, list) else []
      except Exception:
          logger.exception("小红书搜索请求异常")
          return []
      finally:
          await client.close()
  ```

- [ ] **Update `tests/test_xhs_search.py` — mock XhsClient instead of aiohttp.ClientSession**

  The existing `TestSearchXhsUserByName` class mocks `platforms.xiaohongshu.search.aiohttp.ClientSession` directly. After the migration, search.py uses `XhsClient._request`. The tests must mock `XhsClient._request` instead.

  **Files changed:**
  - **Test class:** `TestSearchXhsUserByName` (4 test methods)
  - **Current mock pattern:** `patch("platforms.xiaohongshu.search.aiohttp.ClientSession", mock_cls)`
  - **New mock pattern:** `patch("platforms.xiaohongshu.client.XhsClient._request", ...)` — use AsyncMock returning the expected JSON data structure

  Specific test method changes:

  | Test method | Current assertion | New mock target |
  |---|---|---|
  | `test_returns_matching_users` | `mock_cls` wraps `aiohttp.ClientSession` | `patch("platforms.xiaohongshu.client.XhsClient._request", AsyncMock(return_value={"users": [...]}))` |
  | `test_returns_empty_on_no_match` | same pattern | `XhsClient._request` returns `{"users": []}` |
  | `test_returns_empty_on_api_error` | mocks 500 response | `XhsClient._request` raises `RetryableError` (or just `Exception`) |
  | `test_returns_empty_on_success_false` | mocks `success: False` | `XhsClient._request` raises `DataFetchError("rate limited")` |

  Note: `XhsClient` wraps search failures as exceptions; `search_xhs_user_by_name` catches all `Exception` and returns `[]`. The tests should verify this exception catching works.

  Remove now-unused imports from test file: `MagicMock` (if only used for `mock_cls`).

  Also note: the old mock patches `platforms.xiaohongshu.signer.get_xhs_sign` — this is no longer needed after search.py uses XhsClient (which internally calls get_xhs_sign). The test can drop that patch.

- [ ] **Verify with pyright and run tests**

  ```bash
  uv run ruff check .
  uv run pytest tests/test_xhs_search.py -v
  ```

### Task P3-5: (Deferred to P4) auth.py cleanup is redundant

> **No work in this task.** Originally this task deleted the dead signing functions
> (`_try_vendor_sign`, `_local_sign`, `get_signed_params`, `get_request_headers`)
> and cleaned up unused imports from `auth.py`. **That work is fully subsumed by
> P4-2**, which rewrites `auth.py` in its entirety. Touching `auth.py` here would
> be wasted effort (P4-2 overwrites the file) and risks merge churn.
>
> After P3-1 through P3-4 migrate all callers (monitor/comments/downloader/search)
> to `XhsClient`, the dead signing functions become unreachable but harmless —
> they don't break any import (no caller references them) and don't fail any test.
> The constants `DEFAULT_USER_AGENT` / `XHS_API_BASE` / `XHS_HOME_URL` are still
> defined in `auth.py` and imported by other modules via `from platforms.xiaohongshu.auth import ...`;
> **but note**: `client.py` (P2-4) defines its OWN copies locally and does NOT
> import from auth.py, so deleting these constants in P4-2 will not break client.py.
>
> Skip directly to P3-6.

### Task P3-6: Phase verification

- [ ] **Run full verification suite**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright .
  uv run pytest -x
  ```

  Expected: All pass. `XhsAuthenticator` still uses `_vendor_*` helpers — those are deleted in P4.

---

## Phase P4: Auth QR Rewrite (Pure Python via XhsClient)

**Files:**
- Rewrite: `platforms/xiaohongshu/auth.py` — pure-Python QR login via XhsClient; delete all `_vendor_*` helpers
- Rewrite: `tests/test_xhs_authenticator.py` — mock XhsClient instead of vendor helpers
- Add: `XhsClient.fetch_sec_cookies` already exists (created in P2)

### Task P4-1: Rewrite auth tests first

- [ ] **Rewrite `tests/test_xhs_authenticator.py`**

  **File:** `tests/test_xhs_authenticator.py` (full rewrite)

  Key changes from current:
  - Remove all vendor mocking (`_vendor_init_qr`, `_vendor_check_status`)
  - Mock `XhsClient` methods instead: `create_qrcode`, `check_qrcode_status`, `refresh_cookies`, `probe`, `fetch_sec_cookies`, `get_user_info`
  - Test classes:
    - `TestGenerateQrCode` — mocks `XhsClient.fetch_sec_cookies` + `create_qrcode`; asserts `QRCodeResult.qr_key`, `.qr_url`, `.expires_in`
    - `TestPollQrStatus` — tests status code mapping (1=waiting, 2=scanned, 3=success, 4=expired)
    - `TestGetTokens` — asserts `PlatformTokens` with correct cookies and expiry
    - `TestRefreshTokens` — mocks `XhsClient.refresh_cookies`; asserts cookies are updated or preserved; asserts `expires_at` is bumped OR cookies differ
    - `TestValidateTokens` — mocks `XhsClient.probe()`; asserts expiry check + probe result
    - `TestIsAuthenticator` — subclass/supports checks (unchanged)

  For `TestRefreshTokens`, the AC is relaxed: `cookies` may differ OR `expires_at` is bumped — either is acceptable. The test should assert `result.expires_at >= tokens.expires_at` or `result.cookies != tokens.cookies`.

- [ ] **Run tests to verify they fail**

  ```bash
  uv run pytest tests/test_xhs_authenticator.py -v
  # Expected: FAIL — XhsAuthenticator still uses old vendor-based implementation
  ```

### Task P4-2: Rewrite auth.py

- [ ] **Rewrite `platforms/xiaohongshu/auth.py`**

  Delete everything from `_vendor_setup` through `_vendor_poll_login` (lines 297-392). Delete `_VENDOR_DIR`, `_VENDOR_LOCK`. Delete `threading` import. Remove `XHS_QR_CREATE_API`, `XHS_QR_CHECK_API`, `XHS_QR_STATUS_API`, `XHS_SEC_SCRIPT_API`, `XHS_SEC_GID_API`, `XHS_AS_BASE` constants.

  Rewrite `XhsAuthenticator`:

  ```python
  """小红书认证模块 — 纯 Python QR 登录 + Cookie Keepalive"""

  from __future__ import annotations

  # pyright: basic
  import asyncio
  import binascii
  import hashlib
  import logging
  import os
  import random
  import time
  from collections.abc import Callable
  from typing import Any

  from rich.console import Console

  from platforms.xiaohongshu.client import XhsClient
  from shared.auth.base import (
      AuthStatus,
      BaseAuthenticator,
      PlatformTokens,
      QRCodeResult,
      QRExpiredError,
      QRStatus,
  )
  from shared.auth.qr_display import display_qr_in_terminal
  from shared.config import Config
  from shared.cookie_utils import build_cookie_str

  logger = logging.getLogger("trawler.xiaohongshu.auth")
  console = Console()

  XHS_HOME_URL = "https://www.xiaohongshu.com"
  _A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"


  def get_xhs_cookie(config: Config) -> str:
      """从配置或环境变量获取小红书 Cookie。"""
      cookie = config.xiaohongshu.auth.cookie
      if cookie:
          return cookie.strip()
      cookie = os.environ.get("XHS_COOKIE", "")
      if cookie:
          return cookie.strip()
      logger.warning("未配置小红书 Cookie，API 请求可能失败")
      console.print("[yellow]⚠ 未配置小红书 Cookie，请在 config/cookies.toml 或环境变量 XHS_COOKIE 中设置[/yellow]")
      return ""


  def generate_a1() -> str:
      """Generate a random a1 cookie value."""
      ts_hex = hex(int(time.time() * 1000))[2:]
      random_str = "".join(random.choices(_A1_CHARSET, k=30))
      a_part = ts_hex + random_str + "5" + "0" + "000"
      crc = binascii.crc32(a_part.encode()) & 0xFFFFFFFF
      return (a_part + str(crc))[:52]


  def generate_web_id(a1: str) -> str:
      """Generate webId from a1 (MD5 hash)."""
      return hashlib.md5(a1.encode()).hexdigest()


  class XhsAuthenticator(BaseAuthenticator):
      """小红书 QR 扫码登录 + Keepalive 保活续期 (纯 Python, 无 vendor 依赖)"""

      def __init__(self) -> None:
          self._client: XhsClient | None = None
          self._init_cookies: dict[str, str] = {}
          self._login_cookies: dict[str, str] = {}

      def _ensure_client(self, cookie: str = "") -> XhsClient:
          if self._client is None or cookie:
              self._client = XhsClient(cookie=cookie)
          return self._client

      async def generate_qr_code(self) -> QRCodeResult:
          a1 = generate_a1()
          init_cookies: dict[str, str] = {"a1": a1, "web_id": generate_web_id(a1)}
          client = self._ensure_client()
          sec = await client.fetch_sec_cookies(init_cookies)
          init_cookies.update(sec)
          qr_data = await client.create_qrcode(init_cookies)
          self._init_cookies = init_cookies
          return QRCodeResult(
              qr_url=qr_data["qr_url"],
              qr_key=qr_data["qr_id"],
              expires_in=180,
          )

      async def poll_qr_status(self, qr_key: str) -> AuthStatus:
          client = self._ensure_client()
          try:
              result = await client.check_qrcode_status(qr_key, self._init_cookies.get("code", ""))
          except Exception as e:
              return AuthStatus(success=False, status=QRStatus.WAITING, message=f"轮询失败: {e}")

          status_code = result.get("status", 1)
          if status_code == 3:
              # Login cookies are captured from Set-Cookie in the response
              return AuthStatus(success=True, status=QRStatus.SUCCESS, message="登录成功")
          elif status_code == 2:
              return AuthStatus(success=False, status=QRStatus.SCANNED, message="已扫描，请确认")
          elif status_code == 4:
              return AuthStatus(success=False, status=QRStatus.EXPIRED, message="二维码已过期")
          else:
              return AuthStatus(success=False, status=QRStatus.WAITING, message="等待扫描")

      async def get_tokens(self, qr_key: str) -> PlatformTokens:
          now = time.time()
          cookies = dict(self._init_cookies)
          cookies.update(self._login_cookies)
          return PlatformTokens(
              platform="xhs",
              cookies={k: v for k, v in cookies.items() if v},
              obtained_at=now,
              expires_at=now + 7 * 86400,
          )

      async def qr_login(
          self,
          on_status: Callable[[AuthStatus], None] | None = None,
      ) -> PlatformTokens:
          qr = await self.generate_qr_code()
          display_qr_in_terminal(qr.qr_url)

          deadline = time.monotonic() + qr.expires_in
          while time.monotonic() < deadline:
              status = await self.poll_qr_status(qr.qr_key)
              if on_status is not None:
                  on_status(status)
              if status.status == QRStatus.SUCCESS:
                  try:
                      client = self._ensure_client()
                      await client.get_user_info()
                  except Exception:
                      logger.warning("QR 登录后验证失败，但 cookies 可能仍有效")
                  return await self.get_tokens(qr.qr_key)
              if status.status == QRStatus.EXPIRED:
                  raise QRExpiredError("二维码已过期")
              await asyncio.sleep(2)

          raise QRExpiredError("二维码轮询超时")

      async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
          client = self._ensure_client(build_cookie_str(tokens.cookies))
          new_cookies = await client.refresh_cookies()
          cookies = dict(tokens.cookies)
          if new_cookies:
              cookies.update(new_cookies)
          now = time.time()
          return PlatformTokens(
              platform="xhs",
              cookies=cookies,
              obtained_at=now,
              expires_at=now + 7 * 86400,
          )

      async def validate_tokens(self, tokens: PlatformTokens) -> bool:
          if tokens.expires_at < time.time():
              return False
          client = self._ensure_client(build_cookie_str(tokens.cookies))
          return await client.probe()

      def supports_refresh(self) -> bool:
          return True


  def build_tokens_from_config(config: Config) -> PlatformTokens | None:
      """Build PlatformTokens from config.xiaohongshu.auth. Returns None if not configured."""
      from shared.cookie_utils import parse_cookie_str

      auth = config.xiaohongshu.auth
      if not auth.cookie or auth.expires_at <= 0:
          return None
      cookie_dict = parse_cookie_str(auth.cookie)
      if not cookie_dict:
          return None
      return PlatformTokens(
          platform="xhs",
          cookies=cookie_dict,
          obtained_at=time.time(),
          expires_at=auth.expires_at,
      )
  ```

  Key changes from current auth.py:
  - Remove all `_vendor_*` functions and helpers (lines 297-392)
  - Remove `DEFAULT_USER_AGENT`, `XHS_API_BASE`, `XHS_BASE_URL` constants — these now live ONLY in `platforms/xiaohongshu/client.py` (see P2-4 decision). The rewritten authenticator delegates all HTTP to `XhsClient`, so auth.py no longer references them. Do NOT re-import from client.py (would create a redundant dependency); if any future code needs them, import from `platforms.xiaohongshu.client`.
  - Keep `XHS_HOME_URL` ONLY IF the rewritten authenticator still references it; the P4-2 sketch above does NOT use it, so it can be dropped entirely. (client.py already defines its own `XHS_HOME_URL`.)
  - Remove `XHS_QR_*`, `XHS_SEC_*`, `XHS_AS_BASE` constants
  - Remove `get_request_headers`, `get_signed_params`, `_local_sign`, `_try_vendor_sign`
  - Remove `_fetch_sec_cookies` standalone function (now `XhsClient.fetch_sec_cookies`)
  - Remove `threading`, `aiohttp`, `sys`, `Path` imports
  - Rewrite `XhsAuthenticator` to use `XhsClient` for all HTTP
  - `build_tokens_from_config` now uses `parse_cookie_str` from shared.cookie_utils

- [ ] **Run tests to verify they pass**

  ```bash
  uv run pytest tests/test_xhs_authenticator.py -v
  # Expected: PASS
  ```

### Task P4-3: Phase verification

- [ ] **Run full verification suite**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright .
  uv run pytest -x
  ```

  Expected: All pass. Vendor dependencies eliminated from auth flow.

---

## Phase P5: Cleanup

**Files:**
- Delete: `vendor/spider_xhs/` directory (44 MB — plain directory, NOT a git submodule)
- Delete: `shared/http.py` (dead httpx code)
- Modify: `README.md` — remove references to `shared/http.py` in architecture diagram
- Modify: `pyproject.toml` — remove `httpx` dependency (only used by dead `shared/http.py`)
- Verify: No remaining references to `spider_xhs` or old HTTP patterns in code

### Task P5-1: Delete vendor/spider_xhs directory

- [ ] **Delete the directory via rm -rf (NOT git submodule deinit)**

  ```bash
  rm -rf /home/zyw10/proj/trawler/vendor/spider_xhs
  ```

  Note: This is a plain directory, NOT a git submodule. Do NOT use `git submodule deinit`.

- [ ] **Verify deletion**

  ```bash
  test -d /home/zyw10/proj/trawler/vendor/spider_xhs && echo "STILL EXISTS" || echo "DELETED OK"
  ```

### Task P5-2: Delete shared/http.py (dead httpx wrapper)

- [ ] **Verify no remaining imports of shared.http**

  ```bash
  grep -rn "shared\.http\|from shared.http\|import shared.http" /home/zyw10/proj/trawler/platforms/ /home/zyw10/proj/trawler/core/ /home/zyw10/proj/trawler/shared/ /home/zyw10/proj/trawler/run_check.py
  # Expected: no matches
  ```

- [ ] **Delete the file**

  ```bash
  rm /home/zyw10/proj/trawler/shared/http.py
  ```

### Task P5-3: Remove httpx dependency from pyproject.toml

- [ ] **Remove httpx from [project.dependencies]**

  ```bash
  # Edit pyproject.toml to remove the "httpx>=0.27" line
  ```

  ```toml
  dependencies = [
      "bilibili-api-python>=17.0",
      "click>=8.0",
      "rich>=13.0",
      "terminal-qrcode>=1.1",
      "tomlkit>=0.13",
      "aiohttp>=3.9",
      "xhshow>=0.1.9",
      "faster-whisper>=1.1",
  ]
  ```

- [ ] **Reinstall to verify**

  ```bash
  uv pip install -e ".[dev]"
  uv run python -c "import httpx" 2>&1 || echo "httpx not available (expected)"
  ```

### Task P5-4: Update README and docs

- [ ] **Update README.md — remove shared/http.py reference**

  Search for `shared/http.py` in README.md (currently in the architecture diagram section). Delete that line.

- [ ] **Check for other stale references**

  ```bash
  grep -rn "spider_xhs\|vendor/" /home/zyw10/proj/trawler/README.md /home/zyw10/proj/trawler/README.zh.md 2>/dev/null || echo "No stale references"
  ```

  If any exist, remove them. (Spec docs under `docs/superpowers/specs/` are allowed to reference old state.)

### Task P5-5: Verify no remaining stale references in code

- [ ] **Grep for spider_xhs in source code**

  ```bash
  grep -rn "spider_xhs" /home/zyw10/proj/trawler/platforms/ /home/zyw10/proj/trawler/shared/ /home/zyw10/proj/trawler/core/ /home/zyw10/proj/trawler/tests/ /home/zyw10/proj/trawler/run_check.py 2>/dev/null || echo "No spider_xhs references found"
  # Expected: no matches
  ```

### Task P5-6: Phase verification + acceptance criteria

- [ ] **Run full verification suite**

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright .
  uv run pytest -x
  ```

- [ ] **Verify spec acceptance criteria**

  ```bash
  # AC1: vendor/spider_xhs no longer exists
  test -d vendor/spider_xhs/ && echo "FAIL" || echo "PASS"

  # AC2: xhs_sign_wrapper.js is deleted
  test -f platforms/xiaohongshu/xhs_sign_wrapper.js && echo "FAIL" || echo "PASS"

  # AC3: shared/http.py is deleted
  test -f shared/http.py && echo "FAIL" || echo "PASS"

  # AC4: import without Node.js
  uv run python -c "from platforms.xiaohongshu.auth import XhsAuthenticator; from platforms.xiaohongshu.signer import get_xhs_sign; print('OK')"

  # AC5: get_xhs_sign returns non-empty dict (mocked)
  uv run python -c "from platforms.xiaohongshu.signer import get_xhs_sign; print(get_xhs_sign('/api/sns/web/v2/user/me', a1='test_a1', method='GET'))"

  # AC6: All XHS HTTP goes through XhsClient (not aiohttp directly)
  rg 'aiohttp\.ClientSession' platforms/xiaohongshu/monitor.py platforms/xiaohongshu/comments.py platforms/xiaohongshu/search.py || echo "PASS — no direct aiohttp in migrated files"

  # AC7: pytest passes
  uv run pytest -x

  # AC8: ruff and pyright are clean (new files carry # pyright: basic)
  uv run ruff check . && uv run pyright .
  ```

- [ ] **Final status** — All acceptance criteria met.

---

## Acceptance Checklist (from spec section 10)

- [ ] `vendor/spider_xhs/` no longer exists (`test -d vendor/spider_xhs/` is false)
- [ ] `platforms/xiaohongshu/xhs_sign_wrapper.js` is deleted (P1)
- [ ] `shared/http.py` is deleted (P5)
- [ ] `uv run python -c "from platforms.xiaohongshu.auth import XhsAuthenticator; from platforms.xiaohongshu.signer import get_xhs_sign; print('OK')"` succeeds without Node.js installed
- [ ] `uv run python -c "from platforms.xiaohongshu.signer import get_xhs_sign; print(get_xhs_sign('/api/sns/web/v2/user/me', a1='test_a1', method='GET'))"` returns a dict with non-empty `xs`, `xt`, `xs_common` keys (mocked or real)
- [ ] All XHS HTTP in `monitor.py` / `comments.py` / `downloader.py` / `search.py` goes through `XhsClient` — verified by `rg "aiohttp\.ClientSession" platforms/xiaohongshu/{monitor,comments,search}.py` returning no hits (downloader.py keeps aiohttp for raw CDN downloads only)
- [ ] `uv run pytest -x` passes
- [ ] `uv run ruff check .` and `uv run pyright .` are clean (new code carries `# pyright: basic` consistent with existing platform files)
- [ ] `XhsAuthenticator.refresh_tokens()` returns a `PlatformTokens` whose `cookies` dict may differ from input **OR** whose `expires_at` is bumped — either is acceptable

