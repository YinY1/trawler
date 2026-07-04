"""API token 管理 CLI（T5）。

Usage:
    python -m api.token_tool create <name> [--force]
    python -m api.token_tool list
    python -m api.token_tool revoke <name>

复用 :mod:`api.auth` 的 :func:`create_token` / :func:`revoke_token`（T1）和
:func:`web.auth.load_auth_config` 读写 ``data/auth.toml``。

设计决策（plan T5）：
- 用 Click（与 ``run_check.py`` 主 CLI 风格一致），而不是 plan 草稿里的 argparse。
  token 管理是 ``trawler`` 主 CLI 的自然延伸；``python -m api.token_tool`` 也能用 Click。
- ``create`` 输出明文 token 一次（仅此一次），存 SHA-256 hash 无法恢复。
- ``--force``：同名 token 已存在时要求显式覆盖；T1 的 :func:`create_token` 自身
  会静默覆盖同名，所以 CLI 层在调用前先检查是否存在，未带 ``--force`` 直接报错退出。
- ``list`` 只显示 hash 前 8 位（安全），不泄露完整 hash。
"""

from __future__ import annotations

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
    """API token 管理（生成 / 列出 / 撤销）。"""
    pass


@cli.command()
@click.argument("name")
@click.option("--force", is_flag=True, help="覆盖同名 token")
def create(name: str, force: bool) -> None:
    """生成新 token，明文仅打印一次（存储为 SHA-256 hash，无法恢复）。"""
    if _token_exists(name) and not force:
        console.print(
            f"[red]✗[/] token '{name}' 已存在，加 --force 覆盖",
            style="red",
        )
        sys.exit(1)

    plain = create_token(name)
    console.print(f"[green]✓[/] 已创建 token '{name}'，明文（仅此一次）：")
    console.print(f"[yellow]{plain}[/]")
    console.print("[dim]存储为 SHA-256 hash，后续无法再查看明文。[/]")


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
    for t in cfg.api_tokens:
        created = datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M")
        table.add_row(t.name, t.token_hash[:8], created)
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


if __name__ == "__main__":
    cli()
