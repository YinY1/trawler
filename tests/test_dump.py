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
    """写文件失败 → 静默吞,不抛异常。"""
    monkeypatch.setattr(dump_mod, "DUMP_ENABLED", True)
    # DUMP_DIR 指向不存在的不可写路径
    monkeypatch.setattr(dump_mod, "DUMP_DIR", Path("/nonexistent/path/that/cannot/be/created"))
    # 不应抛异常
    dump_response("test_tag", {"k": "v"})
