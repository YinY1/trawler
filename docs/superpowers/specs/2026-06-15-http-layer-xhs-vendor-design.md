# HTTP Layer & XHS Vendor Optimization — Design

**Date:** 2026-06-15
**Status:** Design (awaiting review)
**Scope:** Replace `vendor/spider_xhs` with pure-Python signing; consolidate all XHS HTTP behind a single client; remove dead HTTP code.
**Out of scope:** WeiboClient consolidation, curl_cffi TLS, proxy rotation, cross-platform shared utilities — these are deferred to separate specs.

---

## 1. Motivation

### Current pain points

1. **`vendor/spider_xhs/` is 44 MB** (including `node_modules/`) and drags the repo, CI, and IDE indexing.
2. **Two parallel vendor integration paths exist**:
   - `signer.py` invokes `static/xhs_main_260411.js` via Node.js subprocess — requires Node.js installed at runtime.
   - `auth.py` calls vendor's Python `apis.xhs_pc_login_apis.XHSLoginApi` — which itself is a thin RPC shim over the same JS files via `execjs`.
   - Both paths depend on Node.js. Spider_XHS's Python modules **cannot** be used standalone without the JS engine (verified via @librarian research).
3. **HTTP is fragmented**: `monitor.py`, `comments.py`, `downloader.py`, `auth.py`, `search.py` each open their own `aiohttp.ClientSession()` per call — no shared cookies, no shared retry, no shared error handling.
4. **`shared/http.py` is dead code** — wraps `httpx` but no caller uses it.
5. **Cookie keepalive is fake** — `XhsAuthenticator.refresh_tokens()` only bumps the `expires_at` timestamp; it never rotates the actual cookie values, so cookies silently die after ~7 days.

### Research-grounded decision

@librarian confirmed `xhshow` v0.1.9 (2026-02-17, MIT, 832★) is the mainstream pure-Python XHS signing library, already adopted by MediaCrawler (48K★) in production since Nov 2025. This makes Node.js-free signing viable.

**Known caveat (resolved by spike, 2026-06-15):** Earlier xhshow 0.1.x had an `a3_hash` GET-signing bug that MediaCrawler worked around via `crypto_processor.build_payload_array(...)`. **xhshow 0.2.0 has fixed this** — `sign_headers_get()` (and the unified `sign_headers(method, ...)`) work directly. No workaround is needed; the implementation calls xhshow's public API.

---

## 2. Goals & Non-Goals

### Goals

- G1: Eliminate Node.js and `vendor/spider_xhs/` entirely.
- G2: All XHS HTTP traffic flows through a single `XhsClient` class.
- G3: Cookie keepalive actually rotates cookie values via Set-Cookie capture.
- G4: Pure-Python QR login (no vendor, no Playwright, no Node.js).
- G5: No regression in functionality — every existing API call path has an equivalent in the new client.
- G6: Delete `shared/http.py` (dead code).

### Non-Goals (deferred)

- N1: Weibo HTTP consolidation (separate spec).
- N2: `curl_cffi` TLS fingerprint swap (orthogonal; revisit after this lands).
- N3: Proxy pool / rotation (YAGNI — no current need).
- N4: Cross-platform shared `_download_file()` / cookie utils (extract after both XhsClient and WeiboClient exist).
- N5: Generic retry-with-backoff library integration (per-client `try/except` is sufficient for current scale).

---

## 3. Architecture

### File-level changes

```
shared/
  http.py             → DELETE (G6 — dead code, no callers)
  cookie_utils.py     → NEW    (parse_cookie_str, parse_set_cookie_headers, build_cookie_str)
  exceptions.py       → EXTEND (add IpBlockError, CaptchaError, RetryableError)

platforms/xiaohongshu/
  signer.py           → REWRITE  (xhshow pure Python; same get_xhs_sign() signature)
  client.py           → NEW      (XhsClient — single HTTP entry point)
  auth.py             → REWRITE  (pure-Python QR login via XhsClient; remove _vendor_*)
#### `monitor.py` (migrated)

Replace both `_fetch_notes_via_api()` and `_fetch_notes_fallback()` with a single call to `XhsClient.get_user_notes()`. **Delete `_fetch_notes_fallback()` entirely** — it makes unsigned requests that no longer work (XHS rejects them) and `XhsClient`'s error translation is the single degradation path. Remove the duplicated `_extract_a1()` (now in `cookie_utils.extract_cookie_value`). Remove `aiohttp` import.

The outer `fetch_user_notes()` function signature stays unchanged — it's called from `handlers.py` — only its internal HTTP mechanism changes.
  comments.py         → MIGRATE  (call XhsClient)
  downloader.py       → MIGRATE  (call XhsClient for feed API; keep aiohttp for raw CDN download)
  search.py           → MIGRATE  (call XhsClient)
  handlers.py         → UPDATE   (imports only)
  parser.py           → UNCHANGED
  xhs_sign_wrapper.js → DELETE   (no longer needed)

vendor/spider_xhs/    → DELETE   (44 MB freed; plain directory removal via `rm -rf`)

pyproject.toml        → UPDATE   (add `xhshow>=0.1.9` to `[project.dependencies]`. Preserve the existing `[xhs]` optional extra — `downloader.py:64` still uses `from xhs import XhsClient` in `_try_xhs_downloader_lib`, which is independent of signing and out of scope to refactor here.)
```

### Component responsibilities

#### `XhsClient` (new — `platforms/xiaohongshu/client.py`)

Owns the entire HTTP conversation with XHS. Holds cookie + a1 + a single long-lived `aiohttp.ClientSession`. Exposes typed methods per API endpoint. Internally signs every request via `signer.get_xhs_sign()`.

**Cookie format**: accepts either a cookie string (`"k1=v1; k2=v2"`) or a dict (`{"k1": "v1"}`). Internally normalized to a dict for a1 extraction + a string for the `Cookie:` header. Use the `parse_cookie_str` / `build_cookie_str` helpers from `shared/cookie_utils.py`.

**Testing seam**: `__init__` accepts an optional `session: aiohttp.ClientSession | None = None` for dependency injection. If passed, the client does NOT own (close) the session; if `None`, the client creates and owns it.

**Typing target**: the XHS platform today carries `# pyright: basic` to escape the project's global `strict` mode. New `client.py` carries the same `# pyright: basic` pragma — consistent with the rest of the platform. (A future spec may tighten this; out of scope here.)

Public surface:

```python
class XhsClient:
    def __init__(
        self,
        cookie: str | dict[str, str],
        *,
        session: aiohttp.ClientSession | None = None,
    ): ...

    # ── Internal ──
    async def _request(self, method: str, api: str, *, params=None, json=None) -> dict:
        """Sign, send, parse, raise on error."""

    async def close(self) -> None: ...

    # ── Content APIs ──
    async def get_user_notes(self, user_id: str, cursor: str = "", num: int = 20) -> list[dict]:
        """GET /api/sns/web/v1/user_posted"""

    async def get_note_detail(self, note_id: str, xsec_token: str) -> dict:
        """GET /api/sns/web/v1/feed"""

    async def get_comments(self, note_id: str, cursor: str = "") -> dict:
        """GET /api/sns/web/v2/comment/page"""

    async def search_notes(self, keyword: str, cursor: str = "") -> dict:
        """GET /api/sns/web/v1/search/notes"""

    # ── Auth APIs ──
    async def get_user_info(self) -> dict:
        """GET /api/sns/web/v2/user/me — used by probe() and QR login verification"""

    async def create_qrcode(self, init_cookies: dict[str, str]) -> dict:
        """POST /api/sns/web/v1/login/qrcode/create — returns {qr_id, code, qr_url, ...}"""

    async def check_qrcode_status(self, qr_id: str, code: str) -> dict:
        """GET /api/sns/web/v1/login/qrcode/status — returns login state + set-cookie"""

    async def fetch_sec_cookies(self, init_cookies: dict[str, str]) -> dict[str, str]:
        """POST /api/sec/v1/scripting + /api/sec/v1/shield/webprofile — sec_poison_id, gid"""

    # ── Lifecycle ──
    async def probe(self) -> bool:
        """True iff cookie still accepted by server (calls get_user_info)."""

    async def refresh_cookies(self) -> dict[str, str] | None:
        """GET /explore with current cookie, parse Set-Cookie headers, return merged dict
        (or None if server sent no new cookies)."""
```

#### `signer.py` (rewritten)

Same public signature `get_xhs_sign(api, data, a1, method) -> {xs, xt, xs_common}` so all callers (initially XhsClient only) work unchanged.

**Spike result (2026-06-15, xhshow 0.2.0):** `sign_headers_get` works directly — the `a3_hash` bug that MediaCrawler worked around in 0.1.x has been **fixed upstream**. No `build_payload_array` workaround is needed. Use the unified `Xhshow.sign_headers(method, uri, cookies, params|payload)` entry point for both GET and POST.

**Spike result (header set):** xhshow returns the full set `{x-s, x-t, x-s-common, x-b3-traceid, x-xray-traceid, x-mns, xy-direction}` — modern XHS server validation requires all of them. To preserve backward compatibility with the existing 3-key contract, `get_xhs_sign()` returns only `{xs, xt, xs_common}` (mapped from hyphenated keys). New code paths should call `get_xhs_sign_full()` to get the complete set.

```python
from xhshow import Xhshow

_xhs = Xhshow()

def _sign(api: str, data: dict | str, a1: str, method: str) -> dict[str, str]:
    cookies = f"a1={a1}" if a1 else ""
    payload: dict | None = None
    params: dict | None = None
    if method == "GET":
        if isinstance(data, str) and data:
            from urllib.parse import parse_qs
            params = {k: v[0] for k, v in parse_qs(data).items()}
        elif isinstance(data, dict):
            params = data
    else:
        payload = data if isinstance(data, dict) else None
    return _xhs.sign_headers(method=method, uri=api, cookies=cookies, params=params, payload=payload)


def get_xhs_sign(api: str, data: dict | str = "", a1: str = "", method: str = "POST") -> dict[str, str]:
    headers = _sign(api, data, a1, method)
    return {
        "xs": headers["x-s"],
        "xt": headers["x-t"],
        "xs_common": headers["x-s-common"],
    }


def get_xhs_sign_full(api: str, data: dict | str = "", a1: str = "", method: str = "POST") -> dict[str, str]:
    """Like get_xhs_sign but returns the complete header set (x-s, x-t, x-s-common,
    x-b3-traceid, x-xray-traceid, x-mns, xy-direction). Preferred for new code.
    """
    return _sign(api, data, a1, method)
```

The public `get_xhs_sign` signature stays identical to preserve callers — that is the contract.

#### `auth.py` (rewritten QR login)

Replace all `_vendor_*` helpers with direct `XhsClient` calls:

```python
class XhsAuthenticator(BaseAuthenticator):
    async def generate_qr_code(self) -> QRCodeResult:
        init_cookies = {"a1": generate_a1(), "web_id": generate_web_id(generate_a1())}
        sec = await self._client.fetch_sec_cookies(init_cookies)
        init_cookies.update(sec)
        qr_data = await self._client.create_qrcode(init_cookies)
        self._init_cookies = init_cookies
        return QRCodeResult(qr_url=qr_data["qr_url"], qr_key=qr_data["qr_id"], expires_in=180)

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        result = await self._client.check_qrcode_status(qr_key, self._code)
        # Map XHS login status codes → QRStatus enum (SCANNED / SUCCESS / EXPIRED / WAITING)
        ...

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        new_cookies = await self._client.refresh_cookies()
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
```

`_vendor_setup`, `_vendor_call`, `_vendor_init_qr`, `_vendor_check_status`, `_vendor_poll_login`, `_VENDOR_DIR`, `_VENDOR_LOCK`, `_vendor_cookies` — all deleted.

#### `shared/cookie_utils.py` (new)

```python
def parse_cookie_str(cookie_str: str) -> dict[str, str]: ...
def parse_set_cookie_headers(headers: list[str]) -> dict[str, str]: ...
def build_cookie_str(cookies: dict[str, str]) -> str: ...
def extract_cookie_value(cookie_str_or_dict: str | dict[str, str], name: str) -> str:
    """Return the value of a single cookie (e.g. 'a1'). Empty string if missing.
    Consolidates the duplicated `_extract_a1()` helpers in current monitor.py / search.py."""
```

`parse_set_cookie_headers` uses stdlib `http.cookies.SimpleCookie` first, with a manual fallback parser (regex over the raw header list) to recover malformed entries that `SimpleCookie` silently drops (XHS sometimes sends values with spaces around `=` or duplicate keys). Consumed by `XhsClient.refresh_cookies()` and (later) WeiboClient.

#### `shared/exceptions.py` (extended)

Adds three exception classes (reusing existing `DataError` for HTTP-level data failures, no new `DataFetchError` needed — keeps hierarchy clean):

```python
class IpBlockError(TrawlerError): ...     # data.code == 300012
class CaptchaError(TrawlerError): ...     # HTTP 461 / 471
class RetryableError(TrawlerError): ...   # HTTP 403 / 429 / 5xx / aiohttp.ClientError
# DataError (existing) is raised for 200-OK-but-success=false and 4xx-non-special.
```

Raised inside `XhsClient._request()` based on response status / error code.

---

## 4. Data Flow

### Normal content fetch (e.g. monitor.py)

```
handlers.py
  → fetch_user_notes(user_id, config)
    → XhsClient(cookie).get_user_notes(user_id, cursor)
      → _request("GET", "/api/sns/web/v1/user_posted", params=...)
        → signer.get_xhs_sign(full_api, a1, "GET")  ← xhshow pure Python
        → aiohttp session.get(signed_url, headers)
        → parse JSON, raise on error code
      ← list[dict]
    ← _parse_note_from_api(...) → list[NoteInfo]
```

### QR login

```
XhsAuthenticator.qr_login()
  1. generate a1 + web_id locally
  2. client.fetch_sec_cookies(init) → sec_poison_id, gid
  3. client.create_qrcode(init) → {qr_id, code, qr_url}
  4. display_qr_in_terminal(qr_url)
  5. loop: client.check_qrcode_status(qr_id, code)
       → status == SUCCESS: collect login cookies, break
       → status == EXPIRED: raise QRExpiredError
       → else: sleep(2), retry (deadline = monotonic + 180s)
  6. client.get_user_info() — verify
  7. return PlatformTokens(cookies=..., expires_at=now + 7d)
```

### Cookie keepalive (scheduler-driven)

```
scheduler.tick()
  → authenticator.refresh_tokens(tokens)
    → XhsClient(cookie).refresh_cookies()
      → aiohttp.get(XHS_HOME_URL + "/explore", headers={Cookie})
      → resp.headers.getall("Set-Cookie")
      → parse via shared.cookie_utils.parse_set_cookie_headers
      ← merged dict (or None)
    → bump expires_at + update cookie values
```

---

## 5. Error Handling

`XhsClient._request()` centralizes HTTP-level error translation:

| Server response | Raised exception |
|---|---|
| HTTP 200 + `data.success == true` | return `data["data"]` |
| HTTP 200 + `data.success == false` | `DataError(data["msg"])` |
| HTTP 200 + `data.code == 300012` | `IpBlockError` |
| HTTP 461 / 471 | `CaptchaError` |
| HTTP 403 / 429 | `RetryableError` |
| HTTP 4xx (other) | `DataError(f"HTTP {status}")` |
| HTTP 5xx | `RetryableError` (caller may retry) |
| `aiohttp.ClientError` | `RetryableError` |

Callers decide retry policy. For now: no automatic retry (YAGNI — current scale doesn't need it; revisit if rate limits bite).

---

## 6. Testing Strategy

### Unit tests (mocked HTTP)

- `tests/test_xhs_signer.py` — REWRITE: drop Node.js subprocess mocks; add xhshow-mocked tests covering POST + GET (with workaround) signing; assert `{xs, xt, xs_common}` dict shape.
- `tests/test_xhs_client.py` — NEW: mock `aiohttp` responses, assert each method builds correct URL/headers/payload, translates errors correctly.
- `tests/test_xhs_authenticator.py` — REWRITE: drop vendor mocks; mock `XhsClient.create_qrcode` / `check_qrcode_status` / `refresh_cookies`; assert QR flow state transitions.
- `tests/test_cookie_utils.py` — NEW: parse_cookie_str / parse_set_cookie_headers / build_cookie_str round-trips.

### Integration tests (skipped by default)

- `tests/test_xhs_integration.py` — keep `@pytest.mark.integration` marker; needs real cookie. Manual run only.

### Verification gates

Each phase of the plan ends with:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright .
uv run pytest -x
```

---

## 7. Migration Phasing

The plan splits into 5 independently-committable phases. Each phase leaves the codebase in a working state.

| Phase | Scope | Verification |
|---|---|---|
| **P1: Signer rewrite** | `signer.py` → xhshow; update `test_xhs_signer.py`; add xhshow to `pyproject.toml`. Includes an **API spike** that reads xhshow source + MediaCrawler's `playwright_sign.py` to document the exact GET workaround calls before implementation. | `pytest tests/test_xhs_signer.py -v` |
| **P2: XhsClient + cookie utils + exceptions** | New `client.py`, `shared/cookie_utils.py`, extend `shared/exceptions.py` (add `IpBlockError`, `CaptchaError`, `RetryableError`; reuse existing `DataError`). Includes a **spike** that probes `https://www.xiaohongshu.com/explore` with a real cookie to confirm Set-Cookie headers are actually returned (if not, `refresh_cookies()` falls back to timestamp-only extension). No callers migrated yet. | `pytest tests/test_xhs_client.py tests/test_cookie_utils.py -v` |
| **P3: Migrate callers** | `monitor.py` (delete `_fetch_notes_fallback`), `comments.py`, `downloader.py`, `search.py` call `XhsClient`. Drop their inline sessions + `get_signed_params` usage. Update `tests/test_xhs_search.py` (currently mocks `aiohttp.ClientSession` directly — will break). | `pytest tests/ -v -k "xhs"` |
| **P4: Auth QR rewrite** | Rewrite `auth.py` to pure-Python QR via `XhsClient`. Delete all `_vendor_*` helpers. | `pytest tests/test_xhs_authenticator.py -v` |
| **P5: Cleanup** | Delete `vendor/spider_xhs/` (regular directory, **not** a submodule — `rm -rf`), `xhs_sign_wrapper.js`, `shared/http.py`. Remove stale README mentions. | Full suite + `ruff` + `pyright` |

---

## 8. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| xhshow GET workaround API differs from sketch | Medium | P1 includes a spike task: read xhshow source + MediaCrawler's `playwright_sign.py`, document the exact calls, THEN implement. Public `get_xhs_sign` signature unchanged regardless. |
| Pure-Python QR login hits CAPTCHA where vendor didn't | Low | Vendor used identical signing underneath; xhshow is the same algorithm. If captcha rates spike, add `curl_cffi` swap in a follow-up spec. |
| xhshow package goes unmaintained | Low | Pin version; vendor-lock risk is far smaller than current 44 MB Spider_XHS lock-in. |
| Set-Cookie format on XHS homepage changes | Low | `parse_set_cookie_headers` uses stdlib `http.cookies.SimpleCookie`, robust to format drift. |
| Removing `shared/http.py` breaks hidden caller | Very Low | Verified zero references via `grep`; `pyright` will catch any miss. |
| Scheduler `refresh_cookies()` runs while monitor mid-request | Low | Each caller has its own `XhsClient` instance; refreshed cookies propagate on next request construction (not in-flight). Window of ~1 request worth of stale cookies. A shared cookie store is YAGNI for current single-user scale; revisit if multi-task concurrency grows. |

---

## 9. Out-of-Scope Follow-ups

Tracked for future specs (not this one):

1. WeiboClient — same XhsClient-style consolidation for `platforms/weibo/`.
2. curl_cffi transport swap — once XhsClient + WeiboClient exist, swap `aiohttp` for `curl_cffi.requests.AsyncSession(impersonate="chrome")` in both `__init__`s.
3. Cross-platform `shared/media_downloader.py` — extract `_download_file()` once both platforms have clients.
4. Tenacity-based retry decorator — only if 429/5xx rates justify it.
5. Proxy pool + rotation — only if geo-restrictions or IP blocks become a real problem.

---

## 10. Acceptance Criteria

This spec is "done" when the implementation plan is fully executed and:

- [ ] `vendor/spider_xhs/` no longer exists (`test -d vendor/spider_xhs/` is false). Note: it is a plain directory, **not** a git submodule.
- [ ] `platforms/xiaohongshu/xhs_sign_wrapper.js` is deleted.
- [ ] `shared/http.py` is deleted.
- [ ] `uv run python -c "from platforms.xiaohongshu.auth import XhsAuthenticator; from platforms.xiaohongshu.signer import get_xhs_sign; print('OK')"` succeeds without Node.js installed.
- [ ] `uv run python -c "from platforms.xiaohongshu.signer import get_xhs_sign; print(get_xhs_sign('/api/sns/web/v2/user/me', a1='test_a1', method='GET'))"` returns a dict with non-empty `xs`, `xt`, `xs_common` keys (mocked or real).
- [ ] All XHS HTTP in `monitor.py` / `comments.py` / `downloader.py` / `search.py` goes through `XhsClient` — verified by `rg "aiohttp\.ClientSession" platforms/xiaohongshu/{monitor,comments,search}.py` returning no hits (downloader.py keeps aiohttp for raw CDN downloads only).
- [ ] `uv run pytest -x` passes.
- [ ] `uv run ruff check .` and `uv run pyright .` are clean (new code carries `# pyright: basic` consistent with the existing platform files).
- [ ] `XhsAuthenticator.refresh_tokens()` returns a `PlatformTokens` whose `cookies` dict may differ from input **OR** whose `expires_at` is bumped — either is acceptable (Set-Cookie headers are server-controlled; the spec acknowledges they may be absent).
