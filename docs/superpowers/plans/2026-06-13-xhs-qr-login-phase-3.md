# Phase 3: XHS QR Login — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement XhsAuthenticator(BaseAuthenticator) with QR code login, real API signing via vendor/spider_xhs, and keepalive token refresh.

**Architecture:** XhsAuthenticator wraps Spider_XHS login flow (init cookies → generate QR → poll status → get tokens). Signing via subprocess Node.js calling xhs_main_260411.js. Keepalive refresh visits XHS homepage. Token persisted as single cookie string in config.toml.

**Tech Stack:** Python 3.12+, aiohttp, Node.js 18+, crypto-js (npm), vendor/spider_xhs

**Execution Order:**
1. Task 1: Setup vendor dependencies (npm install)
2. Task 2: Create sign_wrapper.js (Node.js signing bridge)
3. Task 3: Create platforms/xiaohongshu/signer.py (Python signing wrapper)
4. Tasks 4-5: Write tests + implement XhsAuthenticator
5. Task 6: Wire up factory + CLI
6. Task 7: Clean up old signing code

---

### Task 1: Install vendor dependencies (npm install)

- [ ] **Step 1: Install npm packages in vendor/spider_xhs**

```bash
cd vendor/spider_xhs && npm install crypto-js
```

Expected: `node_modules/crypto-js/` directory created.

- [ ] **Step 2: Verify sign works**

```bash
cd vendor/spider_xhs && node -e "const { get_request_headers_params } = require('./static/xhs_main_260411.js'); const r = get_request_headers_params('/api/test', {k:'v'}, 'test_a1', 'POST'); console.log(r.xs.substring(0,4));"
```

Expected: Output starts with `XYS_`

- [ ] **Step 3: Commit vendor**

```bash
cd vendor/spider_xhs && git add package.json package-lock.json node_modules/ -f
```

Note: `vendor/` is typically .gitignored; use `-f` or update .gitignore to un-ignore `vendor/spider_xhs/node_modules/`.

---

### Task 2: Create `vendor/spider_xhs/sign_wrapper.js`

**File:** `vendor/spider_xhs/sign_wrapper.js`

A thin Node.js CLI bridge that reads JSON from stdin, calls `get_request_headers_params`, writes JSON to stdout.

- [ ] **Step 1: Write sign_wrapper.js**

```javascript
/**
 * XHS Sign Wrapper — Node.js bridge for subprocess-based signing.
 * Usage: node sign_wrapper.js <api> <method>
 * Reads data from stdin as JSON string, writes {xs, xt, xs_common} to stdout.
 */
const { get_request_headers_params } = require('./static/xhs_main_260411.js');

const api = process.argv[2];
const method = process.argv[3] || 'POST';
let stdinData = '';

process.stdin.setEncoding('utf-8');
process.stdin.on('data', (chunk) => { stdinData += chunk; });
process.stdin.on('end', () => {
    try {
        const input = JSON.parse(stdinData);
        const a1 = input.a1 || '';
        const data = input.data || '';
        const result = get_request_headers_params(api, data, a1, method);
        console.log(JSON.stringify({ xs: result.xs, xt: result.xt, xs_common: result.xs_common }));
    } catch (e) {
        console.error(JSON.stringify({ error: e.message }));
        process.exit(1);
    }
});
```

- [ ] **Step 2: Test sign_wrapper.js**

```bash
echo '{"a1":"test_a1","data":{"k":"v"}}' | node vendor/spider_xhs/sign_wrapper.js '/api/test' 'POST'
```

Expected: Output is `{"xs":"XYS_...", "xt":..., "xs_common":"..."}`

---

### Task 3: Create `platforms/xiaohongshu/signer.py`

**File:** `platforms/xiaohongshu/signer.py`

Python wrapper that calls sign_wrapper.js via subprocess.

- [ ] **Step 1: Write signer.py**

```python
"""小红书 API 签名模块 - subprocess + Node.js 调用 vendor/spider_xhs"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_VENDOR_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "spider_xhs"
_SIGN_WRAPPER = _VENDOR_DIR / "sign_wrapper.js"


def _check_node() -> bool:
    """Return True if Node.js is available."""
    return shutil.which("node") is not None


def get_xhs_sign(api: str, data: dict | str = "", a1: str = "", method: str = "POST") -> dict[str, str]:
    """Generate XHS API signature headers via vendor/spider_xhs.

    Args:
        api: API path (e.g. '/api/sns/web/v1/login/qrcode/create')
        data: Request data (dict for JSON body, str for query params)
        a1: a1 cookie value (may be empty for initial requests)
        method: HTTP method, 'GET' or 'POST'

    Returns:
        Dict with keys: xs, xt, xs_common

    Raises:
        RuntimeError: If Node.js is not installed or signing fails
    """
    if not _check_node():
        raise RuntimeError(
            "Node.js is required for XHS API signing. Install Node.js 18+ and try again."
        )

    if not _SIGN_WRAPPER.exists():
        raise RuntimeError(
            f"Sign wrapper not found at {_SIGN_WRAPPER}. "
            "Make sure vendor/spider_xhs is properly installed."
        )

    payload = {"a1": a1, "data": data}
    proc = subprocess.run(
        ["node", str(_SIGN_WRAPPER), api, method],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_VENDOR_DIR),
    )

    if proc.returncode != 0:
        try:
            err = json.loads(proc.stderr)
            msg = err.get("error", proc.stderr)
        except json.JSONDecodeError:
            msg = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise RuntimeError(f"XHS signing failed: {msg}")

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"Invalid sign output: {proc.stdout[:200]}")

    return {
        "xs": result.get("xs", ""),
        "xt": str(result.get("xt", "")),
        "xs_common": result.get("xs_common", ""),
    }
```

- [ ] **Step 2: Write tests for signer.py**

**File:** `tests/test_xhs_signer.py`

```python
"""Tests for platforms/xiaohongshu/signer.py — subprocess-based signing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from platforms.xiaohongshu.signer import _check_node, get_xhs_sign


class TestCheckNode:
    def test_finds_node(self):
        with patch("shutil.which", return_value="/usr/bin/node"):
            assert _check_node() is True

    def test_no_node(self):
        with patch("shutil.which", return_value=None):
            assert _check_node() is False


class TestGetXhsSign:
    def test_returns_sign_headers(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"xs": "XYS_abc123", "xt": 1700000000000, "xs_common": "common123"})
        mock_proc.stderr = ""

        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("platforms.xiaohongshu.signer._SIGN_WRAPPER.exists", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            result = get_xhs_sign("/api/test", {"k": "v"}, "test_a1", "POST")

        assert result["xs"] == "XYS_abc123"
        assert result["xt"] == "1700000000000"
        assert result["xs_common"] == "common123"

    def test_raises_when_no_node(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Node.js is required"):
                get_xhs_sign("/api/test")

    def test_raises_on_sign_failure(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = json.dumps({"error": "sign failed"})

        with (
            patch("shutil.which", return_value="/usr/bin/node"),
            patch("platforms.xiaohongshu.signer._SIGN_WRAPPER.exists", return_value=True),
            patch("subprocess.run", return_value=mock_proc),
        ):
            with pytest.raises(RuntimeError, match="sign failed"):
                get_xhs_sign("/api/test")
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_xhs_signer.py -v
```

---

### Task 4: Write tests for XhsAuthenticator

**File:** `tests/test_xhs_authenticator.py`

- [ ] **Step 1: Write test_xhs_authenticator.py**

```python
"""Tests for XhsAuthenticator — fully mocked, no real XHS API calls."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.xiaohongshu.auth import XhsAuthenticator
from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)


# ── Fixtures ──


def _sample_cookies() -> dict[str, str]:
    return {
        "a1": "test_a1_value",
        "web_session": "test_web_session",
        "webId": "test_web_id",
        "gid": "test_gid",
    }


def _cookie_str() -> str:
    return "; ".join(f"{k}={v}" for k, v in _sample_cookies().items())


# ── XhsAuthenticator.generate_qr_code ──


class TestGenerateQrCode:
    @pytest.mark.asyncio
    async def test_returns_qr_code_result(self):
        auth = XhsAuthenticator()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.cookies = MagicMock()
        mock_resp.cookies.items.return_value = []

        async def json_side() -> dict:
            return {"success": True, "data": {"qr_id": "qr_abc", "code": "code_123", "url": "https://qr.xhs.com/abc"}}

        mock_resp.json = AsyncMock(side_effect=json_side)

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            patch("platforms.xiaohongshu.auth.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
            patch("platforms.xiaohongshu.auth.generate_a1", return_value="test_a1"),
            patch("platforms.xiaohongshu.auth.generate_web_id", return_value="test_web_id"),
            patch("platforms.xiaohongshu.auth._fetch_sec_cookies", new_callable=AsyncMock, return_value={"sec_poison_id": "s1", "gid": "g1"}),
        ):
            result = await auth.generate_qr_code()

        assert isinstance(result, QRCodeResult)
        assert result.qr_key == "qr_abc"
        assert "code_123" in result.qr_url  # stores qr_id:code for poll

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self):
        auth = XhsAuthenticator()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.cookies = MagicMock()
        mock_resp.cookies.items.return_value = []

        async def json_side() -> dict:
            return {"success": False, "msg": "rate limited"}

        mock_resp.json = AsyncMock(side_effect=json_side)

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            patch("platforms.xiaohongshu.auth.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
            patch("platforms.xiaohongshu.auth.generate_a1", return_value="test_a1"),
            patch("platforms.xiaohongshu.auth.generate_web_id", return_value="test_web_id"),
            patch("platforms.xiaohongshu.auth._fetch_sec_cookies", new_callable=AsyncMock, return_value={"sec_poison_id": "s1", "gid": "g1"}),
        ):
            with pytest.raises(RuntimeError, match="rate limited"):
                await auth.generate_qr_code()


# ── XhsAuthenticator.poll_qr_status ──


class TestPollQrStatus:
    @pytest.mark.asyncio
    async def test_waiting(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.cookies = MagicMock()
        mock_resp.cookies.items.return_value = []

        async def json_side() -> dict:
            return {"data": {"codeStatus": 0}}

        mock_resp.json = AsyncMock(side_effect=json_side)

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            patch("platforms.xiaohongshu.auth.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.WAITING
        assert not status.success

    @pytest.mark.asyncio
    async def test_scanned(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.cookies = MagicMock()
        mock_resp.cookies.items.return_value = []

        async def json_side() -> dict:
            return {"data": {"codeStatus": 1}}

        mock_resp.json = AsyncMock(side_effect=json_side)

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            patch("platforms.xiaohongshu.auth.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SCANNED
        assert not status.success

    @pytest.mark.asyncio
    async def test_success(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        # First call: poll status returns codeStatus=2
        mock_resp1 = MagicMock()
        mock_resp1.status = 200
        mock_resp1.cookies = MagicMock()
        mock_resp1.cookies.items.return_value = []
        mock_resp1.json = AsyncMock(return_value={"data": {"codeStatus": 2}})

        # Second call: login/qrcode/status returns web_session
        mock_resp2 = MagicMock()
        mock_resp2.status = 200
        mock_resp2.cookies = MagicMock()
        mock_resp2.cookies.items.return_value = []
        mock_resp2.json = AsyncMock(return_value={"success": True, "data": {"login_info": {"session": "ws_session"}}})

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp1)
        mock_session.get = AsyncMock(return_value=mock_resp2)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            patch("platforms.xiaohongshu.auth.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.SUCCESS
        assert status.success
        assert "ws_session" in auth._init_cookies.get("web_session", "")

    @pytest.mark.asyncio
    async def test_expired(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.cookies = MagicMock()
        mock_resp.cookies.items.return_value = []

        async def json_side() -> dict:
            return {"data": {"codeStatus": 3}}

        mock_resp.json = AsyncMock(side_effect=json_side)

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        with (
            patch("shared.http.get_session", return_value=mock_session),
            patch("platforms.xiaohongshu.auth.get_xhs_sign", return_value={"xs": "x", "xt": "1", "xs_common": "c"}),
        ):
            status = await auth.poll_qr_status("qr_abc")

        assert status.status == QRStatus.EXPIRED
        assert not status.success


# ── XhsAuthenticator.get_tokens ──


class TestGetTokens:
    @pytest.mark.asyncio
    async def test_returns_platform_tokens(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1", "web_session": "ws", "webId": "wid", "gid": "gid1"}
        auth._qr_code = "code_123"

        mock_session = MagicMock()
        auth._session = mock_session

        tokens = await auth.get_tokens("qr_abc")

        assert tokens.platform == "xhs"
        assert tokens.cookies["a1"] == "test_a1"
        assert tokens.cookies["web_session"] == "ws"
        assert tokens.expires_at > time.time()

    @pytest.mark.asyncio
    async def test_raises_without_session(self):
        auth = XhsAuthenticator()
        auth._init_cookies = {"a1": "test_a1"}
        auth._qr_code = "code_123"

        mock_session = MagicMock()
        auth._session = mock_session

        with pytest.raises(RefreshFailedError, match="web_session"):
            await auth.get_tokens("qr_abc")


# ── XhsAuthenticator.refresh_tokens ──


class TestRefreshTokens:
    @pytest.mark.asyncio
    async def test_keepalive_updates_expiry(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=MagicMock(status=200))
        auth._session = mock_session

        result = await auth.refresh_tokens(tokens)
        assert result.expires_at > tokens.expires_at
        # Cookies preserved
        assert result.cookies["a1"] == "test_a1_value"

    @pytest.mark.asyncio
    async def test_keepalive_failure_returns_original(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_session = MagicMock()
        mock_session.get = AsyncMock(side_effect=Exception("network error"))
        auth._session = mock_session

        result = await auth.refresh_tokens(tokens)
        assert result is tokens


# ── XhsAuthenticator.validate_tokens ──


class TestValidateTokens:
    @pytest.mark.asyncio
    async def test_expired_returns_false(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies={"a1": "x"},
            obtained_at=time.time() - 86400,
            expires_at=time.time() - 10,
        )
        assert await auth.validate_tokens(tokens) is False

    @pytest.mark.asyncio
    async def test_valid_returns_true(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=MagicMock(status=200))
        auth._session = mock_session

        assert await auth.validate_tokens(tokens) is True

    @pytest.mark.asyncio
    async def test_redirect_returns_false(self):
        auth = XhsAuthenticator()
        tokens = PlatformTokens(
            platform="xhs",
            cookies=_sample_cookies(),
            obtained_at=time.time(),
            expires_at=time.time() + 86400,
        )

        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=MagicMock(status=302))
        auth._session = mock_session

        assert await auth.validate_tokens(tokens) is False


# ── Inheritance ──


class TestIsAuthenticator:
    def test_is_subclass(self):
        assert issubclass(XhsAuthenticator, BaseAuthenticator)

    def test_supports_qr_login(self):
        assert XhsAuthenticator().supports_qr_login() is True

    def test_supports_refresh(self):
        assert XhsAuthenticator().supports_refresh() is True
```

- [ ] **Step 2: Verify tests fail initially**

```bash
pytest tests/test_xhs_authenticator.py -v
```

Expected: All tests fail (XhsAuthenticator not yet implemented or methods missing)

---

### Task 5: Implement `platforms/xiaohongshu/auth.py` with XhsAuthenticator

**File:** `platforms/xiaohongshu/auth.py` — Rewrite to add XhsAuthenticator class while keeping backward-compat functions.

- [ ] **Step 1: Read current auth.py to understand structure**

Current file has: `get_xhs_cookie()`, `_try_vendor_sign()`, `_local_sign()`, `get_signed_params()`, `get_request_headers()`.

- [ ] **Step 2: Add XhsAuthenticator class to auth.py**

Add after the existing `get_request_headers()` function. Keep all existing code.

```python
# ═══════════════════════════════════════════════════════════
# XhsAuthenticator — QR 登录 + Keepalive 续期
# ═══════════════════════════════════════════════════════════

import asyncio
import binascii
import random
import time
import uuid
import json

import aiohttp

from shared.auth.base import (
    AuthStatus,
    BaseAuthenticator,
    PlatformTokens,
    QRCodeResult,
    QRStatus,
    RefreshFailedError,
)
from shared.constants import XHS_REQUEST_TIMEOUT
from shared.http import get_session

# XHS API base
XHS_API_BASE = "https://edith.xiaohongshu.com"
XHS_HOME_URL = "https://www.xiaohongshu.com"

# QR login API paths
XHS_QR_CREATE_API = "/api/sns/web/v1/login/qrcode/create"
XHS_QR_CHECK_API = "/api/qrcode/userinfo"
XHS_QR_STATUS_API = "/api/sns/web/v1/login/qrcode/status"
XHS_USER_ME_API = "/api/sns/web/v2/user/me"
XHS_SEC_SCRIPT_API = "/api/sec/v1/scripting"
XHS_SEC_GID_API = "/api/sec/v1/shield/webprofile"
XHS_AS_BASE = "https://as.xiaohongshu.com"

# a1 generation constants
_A1_CHARSET = "abcdefghijklmnopqrstuvwxyz1234567890"


def generate_a1() -> str:
    """Generate a random a1 cookie value (same algorithm as Spider_XHS)."""
    ts_hex = hex(int(time.time() * 1000))[2:]
    random_str = "".join(random.choices(_A1_CHARSET, k=30))
    a_part = ts_hex + random_str + "5" + "0" + "000"
    crc = binascii.crc32(a_part.encode()) & 0xFFFFFFFF
    return (a_part + str(crc))[:52]


def generate_web_id(a1: str) -> str:
    """Generate webId from a1 (MD5 hash)."""
    import hashlib
    return hashlib.md5(a1.encode()).hexdigest()


async def _fetch_sec_cookies(session, cookies: dict[str, str]) -> dict[str, str]:
    """Fetch sec_poison_id and gid for initial cookies.

    This is needed before QR code generation to establish a valid session.
    """
    from platforms.xiaohongshu.signer import get_xhs_sign

    result: dict[str, str] = {}

    # Step 1: Fetch sec_poison_id
    try:
        api = XHS_SEC_SCRIPT_API
        data = {"callFrom": "web", "callback": "", "type": "ds", "appId": "xhs-pc-web"}
        sign = get_xhs_sign(api, data, cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": _get_default_ua(),
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.post(
            XHS_AS_BASE + api,
            headers=headers,
            cookies=cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            res = await resp.json(content_type=None)
            sec_id = res.get("data", {}).get("secPoisonId")
            if sec_id:
                result["sec_poison_id"] = sec_id
    except Exception:
        pass

    # Step 2: Fetch gid
    try:
        api = XHS_SEC_GID_API
        data = {"platform": "Windows", "sdkVersion": "4.3.5", "svn": "2", "profileData": ""}
        sign = get_xhs_sign(api, data, cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": _get_default_ua(),
            "Content-Type": "application/json",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }
        async with session.post(
            XHS_AS_BASE + api,
            headers=headers,
            cookies=cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            for key, value in resp.cookies.items():
                cookies[key] = value.value if hasattr(value, "value") else str(value)
            if "gid" in cookies:
                result["gid"] = cookies["gid"]
    except Exception:
        pass

    return result


def _get_default_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    )


class XhsAuthenticator(BaseAuthenticator):
    """小红书 QR 扫码登录 + Keepalive 保活续期"""

    def __init__(self) -> None:
        self._session = None
        self._init_cookies: dict[str, str] = {}
        self._qr_code: str = ""  # store 'code' alongside 'qr_id'
        self._config_path: str = "config.toml"

    async def _ensure_session(self):
        if self._session is None:
            self._session = await get_session()

    # ── BaseAuthenticator 接口 ──

    async def generate_qr_code(self) -> QRCodeResult:
        from platforms.xiaohongshu.signer import get_xhs_sign

        await self._ensure_session()

        # Step 1: Generate initial cookies
        self._init_cookies = {
            "abRequestId": str(uuid.uuid4()),
            "ets": str(int(time.time() * 1000)),
            "webBuild": "6.7.4",
            "xsecappid": "xhs-pc-web",
            "loadts": str(int(time.time() * 1000) + random.randint(50, 200)),
            "a1": generate_a1(),
            "webId": generate_web_id(generate_a1()),
        }

        # Step 2: Fetch sec cookies (sec_poison_id, gid)
        sec_cookies = await _fetch_sec_cookies(self._session, self._init_cookies)
        self._init_cookies.update(sec_cookies)

        # Step 3: Generate QR code
        api = XHS_QR_CREATE_API
        data = {"qr_type": 1}
        sign = get_xhs_sign(api, data, self._init_cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": _get_default_ua(),
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }

        async with self._session.post(
            XHS_API_BASE + api,
            headers=headers,
            cookies=self._init_cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            # Collect cookies from response
            for key, morsel in resp.cookies.items():
                self._init_cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            res = await resp.json(content_type=None)

        if not res.get("success"):
            raise RuntimeError(f"生成二维码失败: {res.get('msg', '未知错误')}")

        qr_data: dict = res.get("data") or {}
        if not all(k in qr_data for k in ("qr_id", "code", "url")):
            raise RuntimeError("生成二维码失败: 响应缺少必要字段")

        self._qr_code = qr_data["code"]

        return QRCodeResult(
            qr_url=f"{qr_data['qr_id']}:{qr_data['code']}",  # store both for poll
            qr_key=qr_data["qr_id"],
            expires_in=180,
        )

    async def poll_qr_status(self, qr_key: str) -> AuthStatus:
        from platforms.xiaohongshu.signer import get_xhs_sign

        await self._ensure_session()

        api = XHS_QR_CHECK_API
        data = {"qrId": qr_key, "code": self._qr_code}
        sign = get_xhs_sign(api, data, self._init_cookies.get("a1", ""), "POST")
        headers = {
            "User-Agent": _get_default_ua(),
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }

        async with self._session.post(
            XHS_API_BASE + api,
            headers=headers,
            cookies=self._init_cookies,
            json=data,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            # Collect response cookies
            for key, morsel in resp.cookies.items():
                self._init_cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            res = await resp.json(content_type=None)

        status = (res.get("data") or {}).get("codeStatus")

        # Map XHS status codes to QRStatus
        status_map: dict[int, QRStatus] = {
            0: QRStatus.WAITING,
            1: QRStatus.SCANNED,
            2: QRStatus.SUCCESS,
            3: QRStatus.EXPIRED,
        }

        qr_status = status_map.get(status, QRStatus.WAITING)
        msg_map: dict[int, str] = {
            0: "等待扫码",
            1: "已扫码，等待确认",
            2: "登录成功",
            3: "二维码已过期",
        }

        # When status is 2 (success), also fetch the session token
        if qr_status == QRStatus.SUCCESS:
            # Fetch login info (web_session)
            await self._fetch_login_info(qr_key)

        return AuthStatus(
            success=qr_status == QRStatus.SUCCESS,
            status=qr_status,
            message=msg_map.get(status, f"未知状态: {status}"),
        )

    async def _fetch_login_info(self, qr_key: str) -> None:
        """After QR confirmation, fetch login info to get web_session."""
        api = XHS_QR_STATUS_API
        params = {"qr_id": qr_key, "code": self._qr_code}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_api = f"{api}?{query}"

        from platforms.xiaohongshu.signer import get_xhs_sign

        sign = get_xhs_sign(full_api, a1=self._init_cookies.get("a1", ""), method="GET")
        headers = {
            "User-Agent": _get_default_ua(),
            "Origin": XHS_HOME_URL,
            "Referer": f"{XHS_HOME_URL}/",
            "x-s": sign["xs"],
            "x-t": sign["xt"],
            "x-s-common": sign["xs_common"],
        }

        async with self._session.get(
            XHS_API_BASE + full_api,
            headers=headers,
            cookies=self._init_cookies,
            timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
        ) as resp:
            for key, morsel in resp.cookies.items():
                self._init_cookies[key] = morsel.value if hasattr(morsel, "value") else str(morsel)
            res = await resp.json(content_type=None)

        if res.get("success") and "login_info" in res.get("data", {}):
            self._init_cookies["web_session"] = res["data"]["login_info"].get("session", "")

    async def get_tokens(self, qr_key: str) -> PlatformTokens:
        """Return PlatformTokens from the cookies collected during QR login."""
        if "web_session" not in self._init_cookies:
            raise RefreshFailedError("未获取到 web_session，QR 登录可能未完成")

        now = time.time()
        # Pick the key cookies
        cookie_keys = ["a1", "web_session", "webId", "gid", "sec_poison_id"]
        cookies = {}
        for k in cookie_keys:
            if k in self._init_cookies:
                cookies[k] = self._init_cookies[k]

        return PlatformTokens(
            platform="xhs",
            cookies=cookies,
            obtained_at=now,
            expires_at=now + 7 * 86400,  # estimate 7 days
        )

    async def refresh_tokens(self, tokens: PlatformTokens) -> PlatformTokens:
        """Keepalive: visit XHS homepage to keep cookies active."""
        await self._ensure_session()
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        try:
            async with self._session.get(
                XHS_HOME_URL,
                headers={
                    "User-Agent": _get_default_ua(),
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    now = time.time()
                    return PlatformTokens(
                        platform="xhs",
                        cookies=dict(tokens.cookies),
                        obtained_at=now,
                        expires_at=now + 7 * 86400,
                    )
            return tokens
        except Exception:
            return tokens

    async def validate_tokens(self, tokens: PlatformTokens) -> bool:
        """Check if cookies are still valid by visiting XHS user API."""
        if tokens.expires_at < time.time():
            return False
        await self._ensure_session()
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        try:
            async with self._session.get(
                XHS_HOME_URL,
                headers={
                    "User-Agent": _get_default_ua(),
                    "Cookie": cookie_str,
                },
                timeout=aiohttp.ClientTimeout(total=XHS_REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def supports_refresh(self) -> bool:
        return True
```

Need to add `XHS_REQUEST_TIMEOUT = 15` to `shared/constants.py`.

- [ ] **Step 2: Add XHS_REQUEST_TIMEOUT to shared/constants.py**

```python
XHS_REQUEST_TIMEOUT = 15      # 小红书 API 请求超时
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_xhs_authenticator.py -v
```

Expected: All tests pass.

---

### Task 6: Wire up factory + CLI

- [ ] **Step 1: Update `shared/auth/__init__.py` — add xhs to get_authenticator**

```python
def get_authenticator(platform: str) -> BaseAuthenticator:
    """Factory: get platform authenticator instance."""
    if platform == "bili":
        from platforms.bilibili.auth import BilibiliAuthenticator
        return BilibiliAuthenticator()
    if platform == "weibo":
        from platforms.weibo.auth import WeiboAuthenticator
        return WeiboAuthenticator()
    if platform == "xhs":
        from platforms.xiaohongshu.auth import XhsAuthenticator
        return XhsAuthenticator()
    raise ValueError(f"Unsupported platform: {platform}")
```

- [ ] **Step 2: Update `run_check.py` — enable `trawler login --platform xhs`**

Remove the `if platform == "xhs": console.print(...); return` block (lines 41-43). Add xhs-specific cookie handling:

In the `login()` command, after the existing weibo cookie handling (line 51), add:

```python
elif platform == "xhs":
    cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
    auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
```

- [ ] **Step 3: Update `run_check.py` — enable `trawler token refresh --platform xhs`**

Replace the `elif platform == "xhs": console.print("coming soon"); return` block (lines 185-187) with actual refresh logic:

```python
elif platform == "xhs":
    auth = config.xiaohongshu.auth
    if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
        console.print("[red]✗ 未配置小红书 Cookie 或已过期，请先执行 trawler login --platform xhs[/]")
        sys.exit(1)
    try:
        from platforms.xiaohongshu.auth import XhsAuthenticator
        authenticator = XhsAuthenticator()
        cookie_dict: dict[str, str] = {}
        for part in auth.cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookie_dict[k] = v
        current_tokens = PlatformTokens(
            platform="xhs",
            cookies=cookie_dict,
            obtained_at=time.time(),
            expires_at=auth.expires_at,
        )
        tokens = asyncio.run(authenticator.refresh_tokens(current_tokens))
        cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
        auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
        update_auth_section(platform, auth_dict)
        console.print("[green]✓ xhs Token 续期成功[/]")
    except Exception as exc:
        console.print(f"[red]✗ 续期失败: {exc}[/]")
        sys.exit(1)
```

- [ ] **Step 4: Update CLI tests**

**File:** `tests/test_cli.py`

Update the `trawler login --platform xhs` test: instead of asserting "coming soon", assert it calls `get_authenticator("xhs")` and succeeds.

```python
def test_login_xhs_success(self):
    """trawler login --platform xhs should call XhsAuthenticator"""
    from click.testing import CliRunner
    from unittest.mock import AsyncMock, patch

    runner = CliRunner()
    mock_auth = MagicMock()
    mock_auth.qr_login = AsyncMock(return_value=PlatformTokens(
        platform="xhs",
        cookies={"a1": "x", "web_session": "y"},
        obtained_at=time.time(),
        expires_at=time.time() + 86400,
    ))

    with (
        patch("shared.auth.get_authenticator", return_value=mock_auth),
        patch("shared.auth.update_auth_section"),
    ):
        result = runner.invoke(cli, ["login", "--platform", "xhs"])
        assert result.exit_code == 0
        assert "登录成功" in result.output
```

- [ ] **Step 5: Run all tests**

```bash
pytest tests/test_cli.py -v
pytest tests/test_xhs_authenticator.py -v
pytest tests/test_auth_base.py -v
```

---

### Task 7: Clean up deprecated sign code in auth.py

The old `_local_sign()` and `_try_vendor_sign()` functions are no longer needed since `signer.py` handles signing. But they're still used by `get_signed_params()` which is called by `monitor.py` and `comments.py`.

**Leave the existing `get_signed_params()` and `get_request_headers()` in place** — they are used by the monitoring/download/comments flow. Only the new `XhsAuthenticator` uses the new `signer.py`.

However, update `get_signed_params()` to prefer the new `signer.py`:

```python
def get_signed_params(params: dict[str, Any], cookie: str) -> dict[str, str]:
    """为小红书 API 请求生成签名参数。
    
    优先使用 vendor/spider_xhs subprocess 签名，降级为 _local_sign。
    """
    # Try new signer module first (subprocess + Node.js)
    try:
        a1 = ""
        if cookie:
            for part in cookie.split(";"):
                if "=" in part and "a1" in part.split("=")[0].strip():
                    a1 = part.strip().split("=", 1)[1]
                    break
        from platforms.xiaohongshu.signer import get_xhs_sign
        result = get_xhs_sign("", "", a1)
        if result.get("xs"):
            return result
    except Exception:
        pass
    
    # Fallback: try vendor import
    signed = _try_vendor_sign(params, cookie)
    if signed:
        return signed
    
    # Final fallback: local sign
    return _local_sign(params, cookie)
```

---

### Verification

Run the full test suite and lint:

```bash
pytest -x
ruff check .
ruff format --check .
```

Commit message: `feat(xhs): add XhsAuthenticator with QR login and real API signing`