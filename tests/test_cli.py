"""Tests for CLI subcommands (login/token/check)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from run_check import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── 1. trawler --help ──────────────────────────────────────────


def test_root_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "login" in result.output
    assert "token" in result.output
    assert "check" in result.output
    assert "subscription" in result.output


# ── 2. trawler check --help ────────────────────────────────────


def test_check_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check", "--help"])
    assert result.exit_code == 0
    assert "--platform" in result.output
    assert "all" in result.output
    assert "bili" in result.output
    assert "xhs" in result.output
    assert "weibo" in result.output
    assert "config/config.toml" in result.output
    assert "--from-phase" in result.output


# ── 3. trawler check --platform bili (success) ────────────────


@patch("run_check.run_check_once", new_callable=AsyncMock)
@patch("run_check.load_config")
def test_check_bili_success(mock_load_config: MagicMock, mock_run: AsyncMock, runner: CliRunner) -> None:
    mock_load_config.return_value = MagicMock()
    result = runner.invoke(cli, ["check", "--platform", "bili"])
    assert result.exit_code == 0


# ── 4. trawler check with bad config ──────────────────────────


@patch("run_check.load_config", side_effect=Exception("bad config"))
def test_check_bad_config(mock_load_config: MagicMock, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["check", "--platform", "bili"])
    assert result.exit_code == 1
    assert "配置加载失败" in result.output


# ── 5. trawler login --help ───────────────────────────────────


def test_login_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["login", "--help"])
    assert result.exit_code == 0
    assert "--platform" in result.output
    assert "bili" in result.output
    assert "xhs" in result.output
    assert "weibo" in result.output


# ── 6. trawler login --platform bili (success) ────────────────


@patch("run_check.update_auth_section")
@patch("run_check.get_authenticator")
def test_login_bili_success(mock_get_auth: MagicMock, mock_update: MagicMock, runner: CliRunner) -> None:
    mock_authenticator = MagicMock()
    mock_tokens = MagicMock()
    mock_tokens.cookies = {"SESSDATA": "abc", "bili_jct": "def"}
    mock_tokens.expires_at = 1234567890.0
    mock_tokens.platform = "bili"
    mock_tokens.obtained_at = 1234567890.0
    mock_authenticator.qr_login = AsyncMock(return_value=mock_tokens)
    mock_authenticator._last_refresh_token = "rt123"
    mock_get_auth.return_value = mock_authenticator

    result = runner.invoke(cli, ["login", "--platform", "bili"])
    assert result.exit_code == 0
    assert "登录成功" in result.output
    mock_update.assert_called_once()


# ── ──


@patch("run_check.update_auth_section")
@patch("run_check.get_authenticator")
def test_login_xhs_success(mock_get_auth: MagicMock, mock_update: MagicMock, runner: CliRunner) -> None:
    mock_authenticator = MagicMock()
    mock_tokens = MagicMock()
    mock_tokens.cookies = {"a1": "abc123", "web_session": "xyz789"}
    mock_tokens.expires_at = 1234567890.0
    mock_tokens.platform = "xhs"
    mock_tokens.obtained_at = 1234567890.0
    mock_authenticator.qr_login = AsyncMock(return_value=mock_tokens)
    mock_get_auth.return_value = mock_authenticator

    result = runner.invoke(cli, ["login", "--platform", "xhs"])
    assert result.exit_code == 0
    assert "登录成功" in result.output
    mock_update.assert_called_once()


# ── 8. trawler token status ───────────────────────────────────


@patch("run_check.load_config")
def test_token_status(mock_load_config: MagicMock, runner: CliRunner) -> None:
    from shared.config import Config

    cfg = Config()
    # bilibili: valid (30 days from now)
    cfg.bilibili.auth.expires_at = time.time() + 30 * 86400
    # xiaohongshu: expired
    cfg.xiaohongshu.auth.expires_at = time.time() - 86400
    # weibo: not configured (0)
    cfg.weibo.auth.expires_at = 0.0

    mock_load_config.return_value = cfg

    result = runner.invoke(cli, ["token", "status"])
    assert result.exit_code == 0
    assert "Token 状态" in result.output
    assert "bilibili" in result.output
    assert "有效" in result.output
    assert "已过期" in result.output
    assert "未配置" in result.output


# ── 9. trawler token refresh --platform bili (success) ────────


@patch("run_check.update_auth_section")
@patch("run_check.get_authenticator")
@patch("run_check.load_config")
def test_token_refresh_bili_success(
    mock_load_config: MagicMock,
    mock_get_auth: MagicMock,
    mock_update: MagicMock,
    runner: CliRunner,
) -> None:
    from shared.config import Config

    cfg = Config()
    cfg.bilibili.auth.expires_at = time.time() + 30 * 86400  # valid
    mock_load_config.return_value = cfg

    mock_authenticator = MagicMock()
    mock_tokens = MagicMock()
    mock_tokens.cookies = {"SESSDATA": "new_abc"}
    mock_tokens.expires_at = time.time() + 180 * 86400
    mock_authenticator.refresh_tokens = AsyncMock(return_value=mock_tokens)
    mock_get_auth.return_value = mock_authenticator

    result = runner.invoke(cli, ["token", "refresh", "--platform", "bili"])
    assert result.exit_code == 0
    assert "续期成功" in result.output
    mock_update.assert_called_once()


# ── 10. trawler token refresh --platform bili (expired) ────────


@patch("run_check.load_config")
def test_token_refresh_bili_expired(mock_load_config: MagicMock, runner: CliRunner) -> None:
    from shared.config import Config

    cfg = Config()
    cfg.bilibili.auth.expires_at = time.time() - 86400  # expired
    mock_load_config.return_value = cfg

    result = runner.invoke(cli, ["token", "refresh", "--platform", "bili"])
    assert result.exit_code == 1
    assert "请先执行 trawler login" in result.output


# ── ──


@patch("run_check._refresh_single_platform")
@patch("run_check._is_platform_configured", return_value=True)
@patch("run_check.load_config")
def test_token_refresh_all(
    mock_load_config: MagicMock,
    mock_is_configured: MagicMock,
    mock_refresh: MagicMock,
    runner: CliRunner,
) -> None:
    mock_refresh.return_value = True
    mock_load_config.return_value = MagicMock()

    result = runner.invoke(cli, ["token", "refresh", "--all"])
    assert result.exit_code == 0
    assert mock_refresh.call_count == 3
    assert mock_refresh.call_args_list[0][0][0] == "bili"
    assert mock_refresh.call_args_list[1][0][0] == "xhs"
    assert mock_refresh.call_args_list[2][0][0] == "weibo"


# ── ──


@patch("run_check.load_config")
def test_token_refresh_no_target(mock_load_config: MagicMock, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["token", "refresh"])
    assert result.exit_code == 1
    assert "请指定" in result.output


# ── 11. trawler check --from-phase ─────────────────────────


@patch("run_check.run_check_once", new_callable=AsyncMock)
@patch("run_check.load_config")
def test_check_with_from_phase(mock_load_config: MagicMock, mock_run: AsyncMock, runner: CliRunner) -> None:
    mock_load_config.return_value = MagicMock()
    result = runner.invoke(cli, ["check", "--from-phase", "downloaded", "--platform", "bili"])
    # Should fail on config, not on option parsing
    assert result.exit_code != 2  # exit code 2 = invalid options


# ── 12. subscription CLI ──────────────────────────────────────────


@patch("run_check.add_subscription", return_value=(True, "已添加: 李大霄"))
def test_subscription_add(mock_add: MagicMock, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["subscription", "add", "--platform", "bili", "--id", "123", "--name", "李大霄"])
    assert result.exit_code == 0
    assert "已添加" in result.output
    mock_add.assert_called_once()


def test_subscription_add_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["subscription", "add", "--help"])
    assert result.exit_code == 0
    assert "--platform" in result.output
    assert "--id" in result.output
    assert "--search-name" in result.output


@patch("run_check.search_by_name")
@patch("run_check.add_subscription", return_value=(True, "已添加: 李大霄"))
def test_subscription_add_search_name(mock_add: MagicMock, mock_search: MagicMock, runner: CliRunner) -> None:
    mock_search.return_value = (True, "找到 1 个匹配", [{"uid": 2137589551, "name": "李大霄"}])
    result = runner.invoke(cli, ["subscription", "add", "--platform", "bili", "--search-name", "李大霄"])
    assert result.exit_code == 0
    assert "已添加" in result.output
    assert "2137589551" in result.output
    mock_add.assert_called_once_with("bili", 2137589551, "李大霄")


@patch("run_check.search_by_name")
def test_subscription_add_search_name_no_match(mock_search: MagicMock, runner: CliRunner) -> None:
    mock_search.return_value = (False, "未找到名为「Unknown」的用户", [])
    result = runner.invoke(cli, ["subscription", "add", "--platform", "bili", "--search-name", "Unknown"])
    assert result.exit_code == 1
    assert "未找到" in result.output


@patch("run_check.search_by_name")
def test_subscription_add_search_name_multiple(mock_search: MagicMock, runner: CliRunner) -> None:
    mock_search.return_value = (
        True,
        "找到 2 个匹配",
        [{"uid": 100, "name": "User1"}, {"uid": 200, "name": "User2"}],
    )
    result = runner.invoke(cli, ["subscription", "add", "--platform", "bili", "--search-name", "User"])
    assert result.exit_code == 1
    assert "多个匹配" in result.output
    assert "User1" in result.output
    assert "User2" in result.output


@patch("run_check.search_by_name")
@patch("run_check.add_subscription", return_value=(True, "已添加: 人民日报"))
def test_subscription_add_search_name_weibo(mock_add: MagicMock, mock_search: MagicMock, runner: CliRunner) -> None:
    mock_search.return_value = (
        True,
        "找到 1 个匹配",
        [{"user_id": "2803301701", "name": "人民日报"}],
    )
    result = runner.invoke(cli, ["subscription", "add", "--platform", "weibo", "--search-name", "人民日报"])
    assert result.exit_code == 0
    assert "已添加" in result.output


@patch("run_check.search_by_name")
@patch("run_check.add_subscription", return_value=(True, "已添加: 用户"))
def test_subscription_add_search_name_xhs(mock_add: MagicMock, mock_search: MagicMock, runner: CliRunner) -> None:
    mock_search.return_value = (
        True,
        "找到 1 个匹配",
        [{"user_id": "abc123", "name": "小红书用户"}],
    )
    result = runner.invoke(cli, ["subscription", "add", "--platform", "xhs", "--search-name", "用户"])
    assert result.exit_code == 0
    assert "已添加" in result.output


@patch("run_check.remove_subscription", return_value=(True, "已删除: 李大霄"))
def test_subscription_remove(mock_remove: MagicMock, runner: CliRunner) -> None:
    result = runner.invoke(cli, ["subscription", "remove", "--platform", "bili", "--id", "123"])
    assert result.exit_code == 0
    assert "已删除" in result.output
    mock_remove.assert_called_once()


@patch("run_check.list_subscriptions")
def test_subscription_list(mock_list: MagicMock, runner: CliRunner) -> None:
    mock_list.return_value = {
        "bilibili": [{"uid": 123, "name": "UP1"}],
        "xiaohongshu": [{"user_id": "abc", "name": "用户2"}],
    }
    result = runner.invoke(cli, ["subscription", "list"])
    assert result.exit_code == 0
    assert "UP1" in result.output
    assert "用户2" in result.output


@patch("run_check.list_subscriptions")
def test_subscription_list_filtered(mock_list: MagicMock, runner: CliRunner) -> None:
    mock_list.return_value = {
        "bilibili": [{"uid": 123, "name": "UP1"}],
    }
    result = runner.invoke(cli, ["subscription", "list", "--platform", "bili"])
    assert result.exit_code == 0
    assert "UP1" in result.output
    assert "B站" in result.output  # display name in Rich table
    mock_list.assert_called_with(platform="bili")


def test_subscription_help(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["subscription", "--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "remove" in result.output
    assert "list" in result.output
