"""API token 管理 CLI（T5 + issue #108 ownership）。

Usage（推荐，挂在 trawler 主 CLI 下）::

    trawler api-token create <name> [--force]
    trawler api-token list
    trawler api-token revoke <name>
    trawler api-token adopt --platform <p> --id <id> --owner <token_name>

向后兼容仍可直接以模块方式调用::

    python -m api.token_tool create <name> [--force]
    python -m api.token_tool list
    python -m api.token_tool revoke <name>
    python -m api.token_tool adopt --platform <p> --id <id> --owner <token_name>

复用 :mod:`api.auth` 的 :func:`create_token` / :func:`revoke_token` 和
:func:`web.auth.load_auth_config` 读写 ``data/auth.toml``。

issue #108 改动：
- 删除 ``--resource-platform`` / ``--resource-sub`` flag（ResourceRules 废弃）
- 新增 ``adopt`` 子命令（给孤儿 sub 补 owner_token）
- ``create`` 不传 ``--scope`` 改 red warning（空 scopes = 无权）
- ``list`` 删除 Resource Rules 列
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

from api.auth import create_token, revoke_token
from web.auth import load_auth_config

console = Console()


def _token_exists(name: str) -> bool:
    """auth.toml 是否已存在同名 token。"""
    cfg = load_auth_config()
    return any(t.name == name for t in cfg.api_tokens)


@click.group()
def cli() -> None:
    """API token 管理（生成 / 列出 / 撤销 / adopt）。"""
    pass


@cli.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="覆盖同名 token")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="限制 token scope（可多次指定，如 --scope messages:read --scope check:read）。"
    "不指定 = 无任何权限（#108 破坏性变更）。"
    "合法 scope 见 ALL_SCOPES 常量。要创建 superuser 加 --scope tokens:manage。",
)
def create(
    name: str,
    force: bool,
    scopes: tuple[str, ...],
) -> None:
    """生成新 token，明文仅打印一次（存储为 SHA-256 hash，无法恢复）。

    ``--scope`` 可多次指定，限制 token 能访问的 API 范围。
    不传 ``--scope`` → 空 scopes = 无任何权限（#108 破坏性变更，spec §6.2）。
    要创建 superuser token 加 ``--scope tokens:manage``。
    """
    from api.auth import ALL_SCOPES

    # ── 1. scope 白名单校验（防拼写错误）──────────────────────────
    invalid = [s for s in scopes if s not in ALL_SCOPES]
    if invalid:
        console.print(
            f"[red]✗[/] 未知 scope: {', '.join(invalid)}",
            style="red",
        )
        console.print(f"[dim]合法 scope: {', '.join(ALL_SCOPES)}[/]")
        sys.exit(1)

    # ── 2. _token_exists / --force（同名覆盖检查）─────────────────
    if _token_exists(name) and not force:
        console.print(
            f"[red]✗[/] token '{name}' 已存在，加 --force 覆盖",
            style="red",
        )
        sys.exit(1)

    # ── 3. 落盘 ──────────────────────────────────────────────────
    scope_list = list(scopes)
    plain = create_token(name, scopes=scope_list)
    console.print(f"[green]✓[/] 已创建 token '{name}'，明文（仅此一次）：")
    console.print(f"[yellow]{plain}[/]")
    console.print("[dim]存储为 SHA-256 hash，后续无法再查看明文。[/]")
    if scope_list:
        console.print(f"[cyan]📝[/] Scopes: {', '.join(scope_list)}")
    else:
        console.print(
            "[red]⚠️[/] 未指定 scope = [bold]无任何权限[/]（#108 破坏性变更）。"
            " 要创建 superuser token 加 --scope tokens:manage；"
            " 要创建只读 token 加 --scope messages:read --scope subscriptions:read。",
            style="red",
        )


@cli.command("list")
def list_cmd() -> None:
    """列出所有 token（只显示 hash 前 8 位）。"""
    cfg = load_auth_config()
    if not cfg.api_tokens:
        console.print("[dim]无 API token。[/]")
        return

    table = Table(title="API Tokens")
    table.add_column("Name")
    table.add_column("Hash (前 8 位)")
    table.add_column("Created At")
    table.add_column("Scopes")
    for t in cfg.api_tokens:
        created = datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M")
        if t.scopes:
            scopes_str = ", ".join(t.scopes)
        else:
            scopes_str = "(无权限)"
        table.add_row(t.name, t.token_hash[:8], created, scopes_str)
    console.print(table)


@cli.command()
@click.argument("name")
def revoke(name: str) -> None:
    """按 name 删除 token。"""
    if revoke_token(name):
        console.print(f"[green]✓[/] 已撤销 token '{name}'")
    else:
        console.print(f"[red]✗[/] 未找到 token '{name}'", style="red")
        sys.exit(1)


@cli.command()
@click.option(
    "--platform",
    "platform",
    required=True,
    type=click.Choice(["bili", "xhs", "weibo"]),
    help="平台 short name (bili/xhs/weibo)",
)
@click.option(
    "--id",
    "identifier",
    required=True,
    help="订阅 id（bili=uid, xhs/weibo=user_id）",
)
@click.option(
    "--owner",
    "owner_token",
    required=True,
    help="要绑定为 owner 的 token name（必须在 auth.toml 已存在）",
)
def adopt(platform: str, identifier: str, owner_token: str) -> None:
    """给孤儿 sub 补 owner_token（issue #108）。

    CLI 本身 = 管理员 = superuser 等价，直接改 subscriptions.toml。
    如果 token 不存在（不在 auth.toml）→ 退出码非 0。
    如果 sub 不存在 → 退出码非 0。

    示例::

        trawler api-token adopt --platform bili --id 123456 --owner bili-admin-bot
    """
    from core.subscription_cli import set_subscription_owner

    ok, msg = asyncio.run(set_subscription_owner(
        platform=platform, identifier=identifier, owner_token=owner_token,
    ))
    if ok:
        console.print(f"[green]✓[/] {msg}")
    else:
        console.print(f"[red]✗[/] {msg}", style="red")
        sys.exit(1)


if __name__ == "__main__":
    cli()
