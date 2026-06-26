from __future__ import annotations

import json
from pathlib import Path

from shared import dump as dump_mod
from shared.dump import dump_response


def test_disabled_by_default(monkeypatch, tmp_path):
    """默认 DUMP_ENABLED=False → 不写文件。"""
    monkeypatch.setattr(dump_mod, "DUMP_ENABLED", False)
    monkeypatch.setattr(dump_mod, "DUMP_DIR", tmp_path)
    dump_response("test_tag", {"k": "v"})
    assert not (tmp_path / "test_tag_dump.jsonl").exists()


def test_enabled_writes_jsonl(monkeypatch, tmp_path):
    """开启 → 写 jsonl,格式含 ts + data。"""
    monkeypatch.setattr(dump_mod, "DUMP_ENABLED", True)
    monkeypatch.setattr(dump_mod, "DUMP_DIR", tmp_path)
    dump_response("test_tag", {"k": "v"})
    dump_response("test_tag", {"k2": "v2"})
    path = tmp_path / "test_tag_dump.jsonl"
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert "ts" in rec and rec["data"] == {"k": "v"}


def test_targets_filter(monkeypatch, tmp_path):
    """TRAWLER_DUMP_TARGETS 过滤,只 dump 指定 tag。"""
    monkeypatch.setattr(dump_mod, "DUMP_ENABLED", True)
    monkeypatch.setattr(dump_mod, "DUMP_DIR", tmp_path)
    monkeypatch.setattr(dump_mod, "DUMP_TARGETS", frozenset({"wanted"}))
    dump_response("wanted", {"a": 1})
    dump_response("filtered", {"b": 2})
    assert (tmp_path / "wanted_dump.jsonl").exists()
    assert not (tmp_path / "filtered_dump.jsonl").exists()


def test_dump_failure_silent(monkeypatch, tmp_path):
    """写文件失败 → 静默吞,不抛异常。

    旧版本用 ``DUMP_DIR=Path("/nonexistent/...")`` 期望触发 mkdir 失败,
    但在 root 下 mkdir 任意路径都成功,根本没走到 ``except Exception: pass``,
    属于假绿。这里改为 monkeypatch ``Path.open`` 抛 OSError,精确命中
    dump_response 内层的 ``with path.open(...)`` 写盘失败路径。
    """
    monkeypatch.setattr(dump_mod, "DUMP_ENABLED", True)
    monkeypatch.setattr(dump_mod, "DUMP_DIR", tmp_path)
    original_open = Path.open

    def raising_open(self: Path, *args: object, **kwargs: object) -> object:
        # 只对 tmp_path 下的目标文件抛错,避免误伤 pytest / 测试框架的
        # 其他 Path.open 调用(如写自己的临时配置文件)。
        if self.parent == tmp_path:
            raise OSError("simulated write failure")
        return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", raising_open)
    # 不应抛异常 — dump 失败必须静默
    dump_response("test_tag", {"k": "v"})
    # 验证文件确实没写成
    assert not (tmp_path / "test_tag_dump.jsonl").exists()
