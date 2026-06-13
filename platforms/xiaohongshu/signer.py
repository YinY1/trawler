"""小红书 API 签名模块 - subprocess + Node.js 调用 vendor/spider_xhs"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_VENDOR_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "spider_xhs"
_SIGN_WRAPPER = Path(__file__).resolve().parent / "xhs_sign_wrapper.js"
_XHS_MAIN_JS = _VENDOR_DIR / "static" / "xhs_main_260411.js"


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
            "Make sure the project is properly installed."
        )

    if not _XHS_MAIN_JS.exists():
        raise RuntimeError(
            f"XHS signature JS not found at {_XHS_MAIN_JS}. "
            "Clone https://github.com/cv-cat/Spider_XHS into vendor/spider_xhs and run `npm install crypto-js` there."
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
