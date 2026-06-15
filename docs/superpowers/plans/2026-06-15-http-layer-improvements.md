# HTTP Layer Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve Trawler's HTTP layer: pure Python signing, platform client abstraction (eliminate scattered sessions), retry/degradation, runtime token validation, TLS fingerprint, proxy rotation, real cookie keepalive.

**Motivation:** Learned from MediaCrawler — all improvements are pure HTTP, no browser automation needed.

**Execution Order:**
1. Task 1: Replace XHS signer with pure Python
2. Task 2: XhsClient — 收拢小红书所有 HTTP 调用
3. Task 3: WeiboClient — 收拢微博所有 HTTP 调用
4. Task 4: 跨平台共享工具 (media download, cookie utils)
5. Task 5: Add retry + error code degradation (into clients)
6. Task 6: Add runtime pong() token validation (into clients)
7. Task 7: Adopt curl_cffi for TLS fingerprint (swap client transport)
8. Task 8: Add proxy rotation support (mixin into clients)
9. Task 9: Fix XHS cookie keepalive
10. Task 10: Cleanup dead code
11. Task 11: Write/update tests

---

### Task 1: Replace XHS signer with pure Python

**File:** `platforms/xiaohongshu/signer.py`

Replace `subprocess` + Node.js call with `xhshow` pure Python library.

**Current (fragile):**
```python
proc = subprocess.run(
    ["node", str(_SIGN_WRAPPER), api, method],
    input=json.dumps(payload), capture_output=True, text=True, timeout=30,
)
```

**Target:**
```python
from xhshow import Xhshow

xhshow_client = Xhshow()
headers = xhshow_client.sign_headers_post(uri=api, cookies=cookie_str, payload=data)
```

Notes:
- `xhshow` is MIT licensed, installable via pip
- MediaCrawler already verified correctness, includes a monkey-patch for GET a3_hash bug
- GET requests need special handling (xhshow's sign_headers_post only handles POST)

- [ ] **Step 1: Add xhshow dependency**

Add to `pyproject.toml`:
```toml
dependencies = [
    ...
    "xhshow",
]
```

- [ ] **Step 2: Rewrite `get_xhs_sign()` to use pure Python**

**File:** `platforms/xiaohongshu/signer.py`

Preserve the exact same function signature `get_xhs_sign(api, data, a1, method)` so all callers work unchanged. Return same dict keys `xs`, `xt`, `xs_common`.

Remove:
- `_check_node()`
- `_SIGN_WRAPPER` / `_XHS_MAIN_JS` paths
- `subprocess` import
- `vendor/spider_xhs` dependency (document in README cleanup)

- [ ] **Step 3: Remove vendor dependency note from --help / error messages**

Update any error message that mentions "install Node.js" or "clone spider_xhs".

- [ ] **Step 4: Update tests**

**File:** `tests/test_xhs_signer.py`

- Update mock expectations to match pure Python return values
- Remove tests that check for Node.js error paths

- [ ] **Step 5: Verify**

```bash
pytest tests/test_xhs_signer.py -v
```

---

### Task 2: XhsClient — 收拢小红书所有 HTTP 调用

**Problem:** 小红书平台 4 个文件 (`monitor.py`, `comments.py`, `downloader.py`, `auth.py`) 各建各的 `aiohttp.ClientSession`，重复拼 header、调签名、错误处理。

**Solution:** 创建 `XhsClient` 类，小红书的所有 HTTP 请求走这一个类。

- [ ] **Step 1: Create `XhsClient` class**

**New file:** `platforms/xiaohongshu/client.py`

```python
class XhsClient:
    def __init__(self, cookie: str, proxy: str | None = None):
        self.cookie = cookie
        self._a1 = _extract_a1(cookie)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def _request(self, method: str, api: str, **kwargs) -> dict:
        """Central request: build signed headers, send, handle errors."""
        session = await self._ensure_session()
        sign = get_xhs_sign(api, a1=self._a1, method=method)
        headers = {
            "x-s": sign["xs"], "x-t": sign["xt"], "x-s-common": sign["xs_common"],
            "Cookie": self.cookie, "User-Agent": DEFAULT_USER_AGENT,
            "Origin": XHS_HOME_URL, "Referer": f"{XHS_HOME_URL}/",
        }
        headers.update(kwargs.pop("headers", {}))
        async with session.request(method, f"{XHS_API_BASE}{api}",
                                    headers=headers, **kwargs) as resp:
            if resp.status != 200:
                raise DataFetchError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
            if not data.get("success", False):
                raise DataFetchError(data.get("msg", "unknown"))
            return data.get("data", {})

    async def get_user_notes(self, user_id: str, cursor: str = "",
                             num: int = 20) -> list[dict]:
        """GET /api/sns/web/v1/user_posted — 用户笔记列表"""
        params = {"num": str(num), "cursor": cursor, "user_id": user_id, ...}
        return await self._request("GET", "/api/sns/web/v1/user_posted", params=params)

    async def get_comments(self, note_id: str, xsec_token: str,
                           cursor: str = "") -> dict:
        """GET /api/sns/web/v2/comment/page — 评论列表"""
        ...

    async def probe(self) -> bool:
        """GET /api/sns/web/v2/user/me — 验证 cookie"""
        ...

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
```

- [ ] **Step 2: Migrate monitor.py**

Replace `_fetch_notes_via_api()` and `_fetch_notes_fallback()` usage with `XhsClient.get_user_notes()`. Remove the two standalone functions and their associated imports (`aiohttp`, `urlencode`, etc.).

- [ ] **Step 3: Migrate comments.py**

Replace `fetch_xhs_comment_highlights()` inner logic with `XhsClient.get_comments()`. The outer function wrapper stays (it's called from handlers.py), just the HTTP part gets replaced.

- [ ] **Step 4: Migrate downloader.py**

Replace inline `aiohttp.ClientSession` in `_download_file()` and `_fetch_note_detail()` with `XhsClient` methods.

- [ ] **Step 5: Migrate auth.py**

Replace `XhsAuthenticator._ensure_session()` and standalone session usage with `XhsClient`.

- [ ] **Step 6: Remove redundant exports from auth.py**

After migration, functions like `get_signed_params()`, `get_request_headers()` are only used inside `XhsClient`. Remove public exports, or keep for backward compatibility.

- [ ] **Step 7: Verify**

```bash
pytest tests/ -v -k "xhs"
ruff check .
```

---

### Task 3: WeiboClient — 收拢微博所有 HTTP 调用

**Problem:** 微博 4 个文件 (`api.py`, `comments.py`, `downloader.py`, `auth.py`) 同样各建各的 session。`api.py` 本身 475 行混合了 API 调用、解析、时间处理，缺少结构。

**Solution:** 创建 `WeiboClient` 类，微博的所有 HTTP 请求走这一个类。

- [ ] **Step 1: Create `WeiboClient` class**

**New file:** `platforms/weibo/client.py`

```python
class WeiboClient:
    def __init__(self, cookie: str, proxy: str | None = None):
        self.cookie = cookie
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        """Central request: add cookie + UA, send, handle errors."""
        session = await self._ensure_session()
        headers = {
            "User-Agent": _DEFAULT_UA,
            "Referer": "https://weibo.com/",
            "Cookie": self.cookie,
        }
        headers.update(kwargs.pop("headers", {}))
        async with session.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status != 200:
                raise DataFetchError(f"HTTP {resp.status}")
            return await resp.json(content_type=None)
```

- [ ] **Step 2: Migrate api.py**

Move into `WeiboClient`:
- `fetch_user_posts()` → `WeiboClient.get_user_posts()`
- `fetch_user_posts_pc()` → `WeiboClient.get_user_posts_pc()`
- `fetch_user_posts_mobile()` → `WeiboClient.get_user_posts_mobile()`
- `_fetch_long_text()` → `WeiboClient.get_long_text()`

Keep parsing helpers (`_parse_weibo_post_json`, `_parse_chinese_time`, etc.) as static/private methods on the class, or extract to `parser.py` where they currently live.

- [ ] **Step 3: Migrate comments.py**

Replace `fetch_weibo_comment_highlights()` inner session with `WeiboClient`.

- [ ] **Step 4: Migrate downloader.py**

Replace inline session in `_download_file()` with `WeiboClient`.

- [ ] **Step 5: Migrate auth.py**

Replace inline `aiohttp.ClientSession` usage with `WeiboClient`.

- [ ] **Step 6: Verify**

```bash
pytest tests/ -v -k "weibo"
ruff check .
```

---

### Task 4: 跨平台共享工具

**Problem:** `_download_file()` 在小红书和微博各有一份完全相同实现。cookie 解析散落各处。这些不依赖平台逻辑的代码应该共享。

- [ ] **Step 1: Shared `_download_file()`**

**New file:** `shared/media_downloader.py`

```python
async def download_file(url: str, dest: Path,
                        timeout: int = 30, session: aiohttp.ClientSession | None = None) -> bool:
    """下载文件到指定路径（可复用 session，也可无 session 自动创建）。"""
    close_session = session is None
    if session is None:
        session = aiohttp.ClientSession(trust_env=False)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(await resp.read())
            return True
    except Exception:
        return False
    finally:
        if close_session:
            await session.close()
```

然后 `platforms/*/downloader.py` 里的 `_download_file()` 全部改为调这个共享函数。

- [ ] **Step 2: Shared cookie utils**

**New file:** `shared/cookie_utils.py`

```python
def parse_cookie_str(cookie_str: str) -> dict[str, str]:
    """解析分号分隔的 cookie 字符串为字典。"""

def parse_set_cookie_headers(headers: list[str]) -> dict[str, str]:
    """解析 Set-Cookie 响应头列表为字典（使用 http.cookies.SimpleCookie）。"""

def build_cookie_str(cookies: dict[str, str]) -> str:
    """构建分号分隔的 cookie 字符串。"""
```

`platforms/weibo/auth.py` 的 `_parse_weibo_cookies()` 移到此处，两边共用。

- [ ] **Step 3: Verify**

```bash
pytest tests/ -v
ruff check .
```

---

### Task 5: Add retry + error code degradation (into clients)

**Files:** `platforms/xiaohongshu/client.py`, `platforms/weibo/client.py`

Add retry logic to the client's `_request()` method (centralized, not scattered).

- [ ] **Step 1: Add tenacity dependency (if not already)**

Check `pyproject.toml` — add if missing.

- [ ] **Step 2: Add retry to client._request()**

```python
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

class XhsClient:
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((
            aiohttp.ClientError,
            OSError,
        )),
    )
    async def _request(self, method, api, ...):
        ...
```

Do NOT retry on:
- 4xx client errors (except 429)
- `NoteNotFoundError` (404-equivalent)
- Empty successful responses

- [ ] **Step 3: Add error code degradation**

```python
# Inside _request(), after response received
data = await resp.json()
if data.get("code") == 300012:       # IP blocked
    raise IPBlockError(...)
if resp.status in (471, 461):        # Captcha triggered
    raise CaptchaError(...)
if resp.status in (403, 429):        # Rate limited
    await self._rotate_proxy()
    raise RetryableError(...)
```

- [ ] **Step 4: Apply same pattern to WeiboClient**

- [ ] **Step 5: Verify**

```bash
pytest tests/ -v
ruff check .
```

---

### Task 6: Add runtime pong() token validation (into clients)

**Files:** `platforms/xiaohongshu/client.py`, `platforms/weibo/client.py`, `platforms/bilibili/auth.py`

Add a `probe()` method to each platform client that sends a minimal authenticated request to verify the cookie is still accepted server-side.

- [ ] **Step 1: Add probe() to XhsClient**

```python
async def probe(self) -> bool:
    """Call /api/sns/web/v2/user/me to verify cookie.
    Returns True if cookie is still valid."""
    api = "/api/sns/web/v2/user/me"
    sign = get_xhs_sign(api, a1=self._a1, method="GET")
    headers = {
        "x-s": sign["xs"], "x-t": sign["xt"], "x-s-common": sign["xs_common"],
        "Cookie": self.cookie, "User-Agent": ...,
    }
    async with self._session.get(f"{API_BASE}{api}", headers=headers) as resp:
        if resp.status != 200:
            return False
        data = await resp.json()
        return data.get("success", False) and bool(data.get("data", {}).get("nickname"))
```

- [ ] **Step 2: Add probe() to WeiboClient**

```python
async def probe(self) -> bool:
    """Access weibo.com homepage. 200 = valid."""
    async with self._session.get("https://weibo.com", headers=...) as resp:
        return resp.status == 200
```

- [ ] **Step 3: Add probe() to BilibiliAuthenticator**

Use `bilibili_api.Credential.check_valid()` (already available in `validate_tokens`).

- [ ] **Step 4: Integrate into scheduler**

In `shared/auth/scheduler.py`, replace the pure-timestamp check:

```python
# Before: only check expires_at timestamp
if tokens.expires_at < time.time():
    return RenewalResult(..., "expired")

# After: also check server-side via probe()
if tokens.expires_at < time.time():
    return RenewalResult(..., "expired")
if not await authenticator.probe(tokens):
    logger.warning("Token valid by timestamp but rejected by server")
    # trigger re-login or refresh
```

- [ ] **Step 5: Verify**

```bash
pytest tests/test_scheduler.py -v
```

---

### Task 7: Adopt curl_cffi for TLS fingerprint (swap client transport)

**Files:** `platforms/xiaohongshu/client.py`, `platforms/weibo/client.py`

Replace `aiohttp.ClientSession` with `curl_cffi.requests.AsyncSession(impersonate="chrome")` for better TLS fingerprint.

- [ ] **Step 1: Add curl_cffi dependency**

```toml
dependencies = [
    ...
    "curl-cffi",
]
```

- [ ] **Step 2: Swap XhsClient transport**

```python
from curl_cffi.requests import AsyncSession

class XhsClient:
    def __init__(self, cookie, proxy=None):
        self._session = AsyncSession(
            impersonate="chrome",
            proxy=proxy,
        )
```

Since Task 2-3 already centralized all HTTP into clients, this is a one-line swap per client in `__init__`, not 22 find-and-replaces.

- [ ] **Step 3: Swap WeiboClient transport**

Same one-line swap.

- [ ] **Step 4: Verify curl_cffi API compatibility**

Key differences to verify:
- `curl_cffi` uses `.get()`, `.post()` like `requests`, similar to `aiohttp`
- `resp.json()` works identically
- Timeout handling: `AsyncSession(timeout=30)` vs `aiohttp.ClientTimeout`
- Header casing: `curl_cffi` may normalize headers differently

- [ ] **Step 5: Verify**

```bash
pytest tests/ -v
ruff check .
pyright .
```

---

### Task 8: Add proxy rotation support (mixin into clients)

**New file:** `shared/proxy.py`

Add a `ProxyRefreshMixin` class + `ProxyPool`, inspired by MediaCrawler's pattern.

- [ ] **Step 1: Create proxy types + pool**

```python
# shared/proxy.py
@dataclass
class ProxyInfo:
    ip: str
    port: int
    user: str = ""
    password: str = ""
    expired_at: float = 0.0

    def is_expired(self, buffer: int = 30) -> bool:
        return time.time() + buffer >= self.expired_at


class ProxyPool:
    """Simple proxy pool with TTL management."""

    def __init__(self, provider: str = "static", pool_count: int = 2):
        ...

    async def get_proxy(self) -> ProxyInfo:
        """Get next available proxy, auto-rotate if current expired."""

    @property
    def current(self) -> ProxyInfo | None:
        """Currently used proxy."""
```

- [ ] **Step 2: Create ProxyRefreshMixin**

```python
class ProxyRefreshMixin:
    """Mixin that auto-checks proxy expiry before each request."""

    _proxy_pool: ProxyPool | None = None

    def init_pool(self, pool: ProxyPool | None) -> None:
        self._proxy_pool = pool

    async def _refresh_if_expired(self) -> None:
        if self._proxy_pool and self._proxy_pool.is_current_expired():
            await self._proxy_pool.get_proxy()
```

- [ ] **Step 3: Mix into XhsClient**

```python
class XhsClient(ProxyRefreshMixin):
    def __init__(self, cookie, proxy_pool=None):
        self.init_pool(proxy_pool)
        ...
```

Call `await self._refresh_if_expired()` at top of `_request()`.

- [ ] **Step 4: Mix into WeiboClient**

Same pattern.

- [ ] **Step 5: Add proxy config**

**File:** `shared/config.py`

```python
@dataclass
class ProxyConfig:
    enabled: bool = False
    provider: str = "static"  # static | kuaidaili | wandou
    static_url: str = ""
    pool_count: int = 2
```

- [ ] **Step 6: Write tests**

**File:** `tests/test_proxy.py`

- [ ] **Step 7: Verify**

```bash
pytest tests/test_proxy.py -v
```

---

### Task 9: Fix XHS cookie keepalive

**File:** `platforms/xiaohongshu/auth.py`

Current `refresh_tokens()` only extends `expires_at` timestamp — it never refreshes actual cookie values. Replace with real keepalive using `XhsClient.probe()` + capture Set-Cookie.

- [ ] **Step 1: Rewrite refresh_tokens for XHS**

```python
async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
    client = XhsClient(
        cookie="; ".join(f"{k}={v}" for k, v in tokens.cookies.items()),
    )
    # 1. Check if cookie still works
    if not await client.probe():
        # Cookie rejected by server — can't refresh, need re-login
        return tokens

    # 2. Access a login-gated page to trigger Set-Cookie
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    async with client._session.get(
        "https://www.xiaohongshu.com/explore",
        headers={"User-Agent": DEFAULT_USER_AGENT, "Cookie": cookie_str},
        allow_redirects=False,
    ) as resp:
        set_cookie = resp.headers.getall("Set-Cookie", [])

    if set_cookie:
        new_cookies = _parse_set_cookie(set_cookie)
        updated = dict(tokens.cookies)
        updated.update(new_cookies)
        now = time.time()
        return PlatformTokens(platform="xhs", cookies=updated,
                              obtained_at=now, expires_at=now + 7 * 86400)

    # No new cookie, just extend
    now = time.time()
    return PlatformTokens(platform="xhs", cookies=dict(tokens.cookies),
                          obtained_at=now, expires_at=now + 7 * 86400)
```

- [ ] **Step 2: Add _parse_set_cookie helper**

Use `http.cookies.SimpleCookie`, reused from `weibo/auth.py`'s `_parse_weibo_cookies`.

Move to `shared/cookie_utils.py` so both platforms can share.

- [ ] **Step 3: Update tests**

**File:** `tests/test_xhs_authenticator.py`

- [ ] **Step 4: Verify**

```bash
pytest tests/test_xhs_authenticator.py -v
```

---

### Task 10: Cleanup dead code

- [ ] **Step 1: Remove unused shared/http.py**

`shared/http.py` wraps `httpx` but no code uses it. After Task 5, everything uses `curl_cffi` (via client classes). Delete the file.

- [ ] **Step 2: Remove vendor/spider_xhs references**

After Task 1, the Node.js vendor is no longer needed. Remove:
- `vendor/spider_xhs/` directory reference in docs
- Node.js installation requirement from README
- `xhs_sign_wrapper.js` (if exists)

- [ ] **Step 3: Consolidate cookie parsing**

Move `_parse_weibo_cookies()` from `platforms/weibo/auth.py` and create `shared/cookie.py`:

```python
# shared/cookie.py
def parse_cookie_str(cookie_str: str) -> dict[str, str]:
    """Parse semicolon-delimited cookie string into dict."""

def parse_set_cookie_headers(headers: list[str]) -> dict[str, str]:
    """Parse Set-Cookie response headers into dict."""
```

Both XHS and Weibo can share these.

- [ ] **Step 4: Remove deprecated `_fetch_notes_fallback`**

After client abstraction, the fallback function in `monitor.py` becomes dead code — the client handles degradation internally. Remove it.

- [ ] **Step 5: Verify no regression**

```bash
pytest tests/ -v
ruff check .
ruff format --check .
pyright .
```

---

### Task 11: Write/update tests

- [ ] **Step 1: Update XHS signer tests**

**File:** `tests/test_xhs_signer.py`

- Replace Node.js subprocess mocks with xhshow mocks
- Verify format of returned signature headers

- [ ] **Step 2: Add client tests**

**New file:** `tests/test_xhs_client.py`

- Test `_request()` retry behavior with mocked failures
- Test `probe()` with mocked response
- Test cookie/header injection

- [ ] **Step 3: Add proxy tests**

**File:** `tests/test_proxy.py`

- Test proxy pool rotation
- Test expiry detection

- [ ] **Step 4: Add keepalive tests**

**File:** `tests/test_xhs_authenticator.py`

- Test that `refresh_tokens` captures Set-Cookie
- Test fallback when no new cookie returned

- [ ] **Step 5: Run full suite**

```bash
pytest -x
ruff check .
ruff format --check .
pyright .
```

---

### Verification

```bash
pytest -x
ruff check .
ruff format --check .
pyright .
```

Commit message: `feat: improve HTTP layer — pure Python signing, platform client, retry, probe, TLS fingerprint, proxy rotation, keepalive`
