#!/usr/bin/env python3
"""Web UI entry point — run with: uv run python run_web.py"""

from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    # Single-worker only: SSE queue and check task are in-memory per-process
    uvicorn.run("web.app:app", host="127.0.0.1", port=8080, reload=True)
