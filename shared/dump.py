"""响应 dump 工具:环境变量 TRAWLER_DUMP=1 开启,默认关闭零开销。

调用点写:
    if DUMP_ENABLED:
        dump_response("weibo_poll", data)

环境变量:
    TRAWLER_DUMP=1                 开启 dump(默认关闭)
    TRAWLER_DUMP_DIR=/path         输出目录(默认 /tmp)
    TRAWLER_DUMP_TARGETS=a,b,c     只 dump 指定 tag(默认全部)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DUMP_ENABLED: bool = bool(os.environ.get("TRAWLER_DUMP", ""))
DUMP_DIR: Path = Path(os.environ.get("TRAWLER_DUMP_DIR", "/tmp"))
DUMP_TARGETS: frozenset[str] = frozenset(
    filter(None, os.environ.get("TRAWLER_DUMP_TARGETS", "").split(","))
)


def dump_response(tag: str, data: Any) -> None:
    """把响应 data 以 jsonl 格式追加到 {DUMP_DIR}/{tag}_dump.jsonl。

    失败静默吞掉,dump 永远不能影响主流程。默认关闭时直接 return。
    """
    if not DUMP_ENABLED:
        return
    if DUMP_TARGETS and tag not in DUMP_TARGETS:
        return
    try:
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path = DUMP_DIR / f"{tag}_dump.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False) + "\n")
    except Exception:
        pass  # dump 失败不影响主流程
