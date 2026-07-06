# tests/test_cli_fetch.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from shared.config import Config


def test_fetch_command_invokes_run_fetch_and_process(tmp_path):
    """CLI ``trawler fetch --ids ...`` 调用引擎 ``run_fetch_and_process``。"""
    from run_check import cli

    # 用真实最小 Config（避免 AsyncMock 的 .general.data_dir 返回 Mock 让
    # setup_logging / MessageStore 调用炸 —— P1-3 修复）
    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    runner = CliRunner()
    with patch("run_check.load_config", new=mock_load), \
         patch("run_check.MessageStore"), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        result = runner.invoke(cli, ["fetch", "--ids", "bili:BV1xx,xhs:note1"])

    assert result.exit_code == 0, f"output: {result.output}"
    # run_fetch_and_process 被调用一次，msg_ids 拆分正确
    mock_run.assert_awaited_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["msg_ids"] == ["bili:BV1xx", "xhs:note1"]


def test_fetch_command_invalid_prefix_exits_nonzero(tmp_path):
    """无效前缀 → sys.exit(1)，不调引擎。"""
    from run_check import cli

    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    runner = CliRunner()
    with patch("run_check.load_config", new=mock_load), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        result = runner.invoke(cli, ["fetch", "--ids", "unknown:xx"])

    assert result.exit_code != 0
    assert "无效的 msg_id" in result.output
    mock_run.assert_not_awaited()


def test_fetch_command_skip_push_flag(tmp_path):
    """``--skip-push`` 透传到引擎。"""
    from run_check import cli

    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    runner = CliRunner()
    with patch("run_check.load_config", new=mock_load), \
         patch("run_check.MessageStore"), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        runner.invoke(cli, ["fetch", "--ids", "bili:BV1", "--skip-push"])

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["skip_push"] is True
