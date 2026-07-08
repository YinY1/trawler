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

    def test_create_without_scope_warns_no_permissions(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """不带 --scope → 无任何权限（#108 破坏性变更），输出 red warning。"""
        result = runner.invoke(cli, ["create", "bot"])
        assert result.exit_code == 0
        # warning 文本含「无任何权限」（#108 后空 scopes = 无权）
        assert "无任何权限" in result.output

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
    def test_list_shows_no_permissions_for_empty_scopes(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        from api.auth import create_token

        create_token("admin-bot")  # 空 scope
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "admin-bot" in result.output
        # #108: 空 scopes 显示「无权限」而非「无限制」
        assert "无权限" in result.output

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




# ── adopt 子命令 + create 空 scopes warning（issue #108）──────────────


class TestAdoptCommand:
    """``trawler token adopt --platform --id --owner`` 一键给孤儿 sub 补 owner。"""

    def test_adopt_success(
        self,
        runner: CliRunner,
        auth_path: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """成功给 bili/100 补 owner。"""
        # 先准备 auth.toml 含 owner-bot token
        from api.auth import create_token

        create_token("owner-bot")

        # 准备 subscriptions.toml 含一条无 owner 的 bili sub
        subs_path = tmp_path / "subscriptions.toml"
        subs_path.write_text(
            '[[bilibili.subscriptions]]\n'
            'uid = 100\n'
            'name = "UP100"\n',
            encoding="utf-8",
        )
        # adopt 命令调 set_subscription_owner 时不传 path，默认查 cwd/config/...
        # monkeypatch：让默认 path 指向 tmp 文件
        from core import subscription_cli as cli_mod

        async def patched_set_owner(
            platform: str,
            identifier: int | str,
            owner_token: str,
            path: str = str(subs_path),
        ) -> tuple[bool, str]:
            return await cli_mod._orig_set_owner(  # type: ignore[attr-defined]
                platform=platform,
                identifier=identifier,
                owner_token=owner_token,
                path=str(subs_path),
            )

        # 保存原函数引用（避免 monkeypatch 循环）
        if not hasattr(cli_mod, "_orig_set_owner"):
            cli_mod._orig_set_owner = cli_mod.set_subscription_owner  # type: ignore[attr-defined]
        monkeypatch.setattr(cli_mod, "set_subscription_owner", patched_set_owner)
        # adopt 命令通过 `from core.subscription_cli import set_subscription_owner`
        # 在函数体内 import，patch cli_mod 的属性即可命中。

        result = runner.invoke(
            cli,
            [
                "adopt",
                "--platform", "bili",
                "--id", "100",
                "--owner", "owner-bot",
            ],
        )
        assert result.exit_code == 0
        assert "已设置 owner" in result.output
        # 落盘验证
        content = subs_path.read_text(encoding="utf-8")
        assert 'owner_token = "owner-bot"' in content

    def test_adopt_unknown_token_fails(
        self,
        runner: CliRunner,
        auth_path: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """adopt 不存在的 token → 退出码非 0。"""
        subs_path = tmp_path / "subscriptions.toml"
        subs_path.write_text(
            '[[bilibili.subscriptions]]\n'
            'uid = 100\n'
            'name = "UP100"\n',
            encoding="utf-8",
        )
        from core import subscription_cli as cli_mod

        async def patched_set_owner(
            platform: str,
            identifier: int | str,
            owner_token: str,
            path: str = str(subs_path),
        ) -> tuple[bool, str]:
            return await cli_mod._orig_set_owner(  # type: ignore[attr-defined]
                platform=platform,
                identifier=identifier,
                owner_token=owner_token,
                path=str(subs_path),
            )

        if not hasattr(cli_mod, "_orig_set_owner"):
            cli_mod._orig_set_owner = cli_mod.set_subscription_owner  # type: ignore[attr-defined]
        monkeypatch.setattr(cli_mod, "set_subscription_owner", patched_set_owner)

        result = runner.invoke(
            cli,
            [
                "adopt",
                "--platform", "bili",
                "--id", "100",
                "--owner", "ghost-bot",
            ],
        )
        assert result.exit_code != 0
        assert "未知 token" in result.output or "✗" in result.output


class TestCreateNoScopesWarning:
    """#108 后 create 空 scopes = 无权，CLI 必须明示。"""

    def test_create_no_scopes_shows_red_warning(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """create 不传 --scope → 输出含 red warning 提示无权。"""
        result = runner.invoke(cli, ["create", "empty-bot"])
        assert result.exit_code == 0
        # 输出含「无任何权限」提示
        assert "无任何权限" in result.output


class TestResourceFlagsRemoved:
    """#108 删除 --resource-platform / --resource-sub flag。"""

    def test_create_rejects_resource_platform_flag(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """``--resource-platform`` 已删除 → Click 报未知 option。"""
        result = runner.invoke(
            cli,
            ["create", "x", "--resource-platform", "bili"],
        )
        assert result.exit_code != 0
        # Click 报错信息含 "no such option"
        assert "no such option" in result.output.lower() or "resource-platform" in result.output.lower()

    def test_create_rejects_resource_sub_flag(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """``--resource-sub`` 已删除。"""
        result = runner.invoke(
            cli,
            ["create", "x", "--resource-sub", "bili:100"],
        )
        assert result.exit_code != 0

    def test_list_no_resource_rules_column(
        self, runner: CliRunner, auth_path: Path
    ) -> None:
        """``list`` 命令不再显示 Resource Rules 列。"""
        from api.auth import create_token

        create_token("bot1")
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        # 不含 "Resource Rules" 列标题
        assert "Resource Rules" not in result.output
