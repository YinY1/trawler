"""CLI tests for manual content check options (plan 2026-06-28).

Covers:
- parse_since: relative (24h/7d/30m) and absolute (2026-06-01) time parsing
- check command --title/--author/--since/--reset-phase/--skip-push/--no-skip-push
- empty-result warning
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from run_check import cli
from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase


@pytest.fixture
def populated_store(tmp_path: Path) -> MessageStore:
    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "Python Tutorial", "Alice")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "Java Guide", "Bob")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.mark_phase("bili:BV2", Phase.PUSHED)
    store.save()
    return store


# ── parse_since ─────────────────────────────────────────────────


def test_since_parser_relative_hours() -> None:
    """--since 24h 解析为 now - 24*3600。"""
    from run_check import parse_since

    now = int(time.time())
    result = parse_since("24h")
    assert abs(result - (now - 24 * 3600)) < 5  # 5 秒容差


def test_since_parser_relative_days() -> None:
    from run_check import parse_since

    now = int(time.time())
    result = parse_since("7d")
    assert abs(result - (now - 7 * 24 * 3600)) < 5


def test_since_parser_relative_minutes() -> None:
    from run_check import parse_since

    now = int(time.time())
    result = parse_since("30m")
    assert abs(result - (now - 30 * 60)) < 5


def test_since_parser_absolute_date() -> None:
    from run_check import parse_since

    result = parse_since("2026-06-01")
    # 解析为当天 00:00:00 本地时间的 Unix 时间戳
    expected = int(time.mktime(time.strptime("2026-06-01", "%Y-%m-%d")))
    assert result == expected


def test_since_parser_absolute_datetime() -> None:
    from run_check import parse_since

    result = parse_since("2026-06-01T12:00:00")
    expected = int(time.mktime(time.strptime("2026-06-01T12:00:00", "%Y-%m-%dT%H:%M:%S")))
    assert result == expected


def test_since_parser_invalid_raises() -> None:
    from run_check import parse_since

    with pytest.raises(ValueError):
        parse_since("invalid")


# ── check 命令分支 ───────────────────────────────────────────────


def test_check_with_title_filter(populated_store: MessageStore, tmp_path: Path) -> None:
    """check --title python 应只匹配 BV1。"""
    runner = CliRunner()
    fake_config = MagicMock()
    fake_config.general.data_dir = str(tmp_path)
    with patch("run_check.load_config", new=AsyncMock(return_value=fake_config)), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()) as mock_run, \
         patch("core.pipeline.run_check_once", new=AsyncMock()):
        result = runner.invoke(cli, [
            "check", "--title", "python",
            "--config", str(tmp_path / "config.toml"),
        ])
        if result.exception:
            import traceback

            traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
        assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"
        assert mock_run.called
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["msg_ids"] == ["bili:BV1"]


def test_check_skip_push_default(populated_store: MessageStore, tmp_path: Path) -> None:
    """不传 flag 时 skip_push 默认 True。"""
    runner = CliRunner()
    fake_config = MagicMock()
    fake_config.general.data_dir = str(tmp_path)
    with patch("run_check.load_config", new=AsyncMock(return_value=fake_config)), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()) as mock_run:
        result = runner.invoke(cli, ["check", "--title", "python", "--config", str(tmp_path / "config.toml")])
        assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"
        assert mock_run.call_args.kwargs["skip_push"] is True


def test_check_no_skip_push_flag(populated_store: MessageStore, tmp_path: Path) -> None:
    """--no-skip-push 应让 skip_push=False。"""
    runner = CliRunner()
    fake_config = MagicMock()
    fake_config.general.data_dir = str(tmp_path)
    with patch("run_check.load_config", new=AsyncMock(return_value=fake_config)), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()) as mock_run:
        result = runner.invoke(cli, [
            "check", "--title", "python", "--no-skip-push",
            "--config", str(tmp_path / "config.toml"),
        ])
        assert result.exit_code == 0, f"exit={result.exit_code}, output={result.output}"
        assert mock_run.call_args.kwargs["skip_push"] is False


def test_check_empty_result_prints_warning(populated_store: MessageStore, tmp_path: Path) -> None:
    """筛选无匹配时打印警告并正常退出。"""
    runner = CliRunner()
    fake_config = MagicMock()
    fake_config.general.data_dir = str(tmp_path)
    with patch("run_check.load_config", new=AsyncMock(return_value=fake_config)), \
         patch("run_check.MessageStore", return_value=populated_store), \
         patch("core.engine.PipelineEngine.run_specific_messages", new=AsyncMock()):
        result = runner.invoke(cli, [
            "check", "--title", "nonexistent",
            "--config", str(tmp_path / "config.toml"),
        ])
        assert result.exit_code == 0
        assert "没有匹配的消息" in result.output


# ── --version (issue #55) ────────────────────────────────────────


def test_cli_version_option_outputs_version_display() -> None:
    """trawler --version 输出 VERSION_DISPLAY 字符串。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    # VERSION_DISPLAY 形如 `0.1.0+dev (unknown)` 或 `0.1.0+a1b2c3d (...)`
    from shared.constants import VERSION_DISPLAY

    assert VERSION_DISPLAY in result.output
    assert "Trawler" in result.output


def test_cli_version_option_short_flag_v() -> None:
    """-V 短 flag 也应工作（Click version_option 默认 -V/--version）。"""
    runner = CliRunner()
    result = runner.invoke(cli, ["-V"])
    assert result.exit_code == 0
    from shared.constants import VERSION

    assert VERSION in result.output
