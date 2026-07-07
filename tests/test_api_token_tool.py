"""Tests for api.token_tool (T5) — API token 管理 CLI。

覆盖：
- ``create``：明文打印一次、--force 覆盖语义、重复无 --force 失败
- ``list``：name + hash 前 8 位 + created_at，不泄露完整 hash
- ``revoke``：成功删除、不存在则失败
- 端到端：CLI 生成的 token 能通过 ``require_token`` 校验

用 ``click.testing.CliRunner`` 隔离 stdout/exit_code。通过 monkeypatch
``web.auth.AUTH_TOML_PATH`` / ``api.auth.AUTH_TOML_PATH`` 隔离到 tmp_path，
不触碰真实 ``data/auth.toml``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from api.token_tool import cli
from web.auth import set_password

PASSWORD = "test12345"


@pytest.fixture
def auth_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp 隔离 AUTH_TOML_PATH + set_password 完成 setup。"""
    p = tmp_path / "auth.toml"
    monkeypatch.setattr("web.auth.AUTH_TOML_PATH", p)
    monkeypatch.setattr("api.auth.AUTH_TOML_PATH", p)
    set_password(PASSWORD)
    return p


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── create ────────────────────────────────────────────────────────


class TestCreate:
    def test_create_token_prints_plaintext_once(self, runner: CliRunner, auth_path: Path) -> None:
        from api.auth import _hash_token
        from web.auth import load_auth_config

        result = runner.invoke(cli, ["create", "mybot"])
        assert result.exit_code == 0
        out = result.output

        # 明文 token 出现在输出中（token_urlsafe(32) → ~43 chars）
        # 输出含 "仅此一次" 提示
        assert "仅此一次" in out

        # auth.toml 含一条 name=mybot 的 token
        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 1
        entry = cfg.api_tokens[0]
        assert entry.name == "mybot"

        # 明文能从输出里捞出来：找 _hash_token 反推太脆，改校验
        # 「输出的某段 token hash 后等于 entry.token_hash」
        # 输出格式固定为 [yellow]<plain>[/]，扫所有 token-like 串
        import re

        candidates = re.findall(r"[A-Za-z0-9_-]{30,}", out)
        matched = [_hash_token(c) == entry.token_hash for c in candidates]
        assert any(matched), "明文 token 未在输出中出现或 hash 不匹配"

    def test_create_token_duplicate_requires_force(self, runner: CliRunner, auth_path: Path) -> None:
        # 第一次 create
        r1 = runner.invoke(cli, ["create", "mybot"])
        assert r1.exit_code == 0

        # 第二次无 --force → 失败
        r2 = runner.invoke(cli, ["create", "mybot"])
        assert r2.exit_code != 0
        assert "已存在" in r2.output or "--force" in r2.output

        # 第三次带 --force → 成功覆盖
        r3 = runner.invoke(cli, ["create", "mybot", "--force"])
        assert r3.exit_code == 0

        from web.auth import load_auth_config

        cfg = load_auth_config()
        # 仍只有一条（覆盖，不重复）
        assert len([t for t in cfg.api_tokens if t.name == "mybot"]) == 1


# ── list ──────────────────────────────────────────────────────────


class TestList:
    def test_list_empty(self, runner: CliRunner, auth_path: Path) -> None:
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        # 空列表给出提示
        assert "无" in result.output or "empty" in result.output.lower()

    def test_list_tokens_shows_name_and_hash_prefix(self, runner: CliRunner, auth_path: Path) -> None:
        from api.auth import create_token

        create_token("bot-1")

        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        out = result.output

        assert "bot-1" in out

        from web.auth import load_auth_config

        cfg = load_auth_config()
        full_hash = cfg.api_tokens[0].token_hash
        hash_prefix = full_hash[:8]
        # 前 8 位出现
        assert hash_prefix in out
        # 完整 hash（64 字符）不出现在输出中（安全）
        assert full_hash not in out


# ── revoke ────────────────────────────────────────────────────────


class TestRevoke:
    def test_revoke_token_success(self, runner: CliRunner, auth_path: Path) -> None:
        from api.auth import create_token
        from web.auth import load_auth_config

        create_token("bot-1")

        result = runner.invoke(cli, ["revoke", "bot-1"])
        assert result.exit_code == 0
        assert "bot-1" in result.output

        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 0

    def test_revoke_nonexistent_fails(self, runner: CliRunner, auth_path: Path) -> None:
        result = runner.invoke(cli, ["revoke", "never-existed"])
        assert result.exit_code != 0
        assert "never-existed" in result.output


# ── 端到端：CLI 生成的 token 能通过 require_token ─────────────────


class TestEndToEnd:
    async def test_created_token_can_authenticate(self, runner: CliRunner, auth_path: Path) -> None:
        import re

        from api.auth import _hash_token, require_token
        from web.auth import load_auth_config

        result = runner.invoke(cli, ["create", "mybot"])
        assert result.exit_code == 0

        # 从输出捞明文
        candidates = re.findall(r"[A-Za-z0-9_-]{30,}", result.output)
        cfg = load_auth_config()
        full_hash = cfg.api_tokens[0].token_hash
        plain = next(c for c in candidates if _hash_token(c) == full_hash)

        # 构造假 request 调 require_token
        request = SimpleNamespace(headers={"authorization": f"Bearer {plain}"})
        name = await require_token(request)
        assert name == "mybot"


# ── create --scope ──────────────────────────────────────────────


class TestCreateWithScopes:
    def test_create_with_scopes_persists(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from web.auth import load_auth_config

        result = runner.invoke(
            cli,
            [
                "create", "notifier",
                "--scope", "messages:read",
                "--scope", "check:read",
            ],
        )
        assert result.exit_code == 0
        cfg = load_auth_config()
        assert cfg.api_tokens[0].scopes == ["messages:read", "check:read"]

    def test_create_without_scope_warns_unrestricted(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """不带 --scope → 全权限，输出警告（spec §5.3）。"""
        result = runner.invoke(cli, ["create", "bot"])
        assert result.exit_code == 0
        # 警告文本含「无限制」或「unrestricted」
        assert "无限制" in result.output or "unrestricted" in result.output.lower()

    def test_create_with_invalid_scope_fails(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """--scope xxx 不在白名单 → 退出码非 0，不落盘。"""
        result = runner.invoke(
            cli, ["create", "bot", "--scope", "messages:delete"]
        )
        assert result.exit_code != 0
        assert "scope" in result.output.lower() or "未知" in result.output

        from web.auth import load_auth_config
        cfg = load_auth_config()
        assert len(cfg.api_tokens) == 0  # 未落盘

    def test_create_with_tokens_manage_placeholder_ok(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """tokens:manage 占位常量也应通过白名单校验（虽然路由不消费）。"""
        result = runner.invoke(
            cli, ["create", "bot", "--scope", "tokens:manage"]
        )
        assert result.exit_code == 0


# ── list with scopes ────────────────────────────────────────────


class TestListWithScopes:
    def test_list_shows_unrestricted_for_empty_scopes(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from api.auth import create_token

        create_token("admin-bot")  # 空 scope
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "admin-bot" in result.output
        assert "无限制" in result.output or "unrestricted" in result.output.lower()

    def test_list_shows_scope_list(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from api.auth import create_token

        create_token(
            "notifier", scopes=["messages:read", "check:read"]
        )
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        out = result.output
        assert "notifier" in out
        assert "messages:read" in out
        assert "check:read" in out
