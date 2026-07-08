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
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    help="限制 token scope（可多次指定，如 --scope messages:read --scope check:read）。"
    "不指定 = 全权限。合法 scope 见 ALL_SCOPES 常量。",
)
@click.option(
    "--resource-platform",
    "resource_platforms",
    multiple=True,
    help="限制 token 可访问平台（可多次：--resource-platform bili --resource-platform xhs）。"
    "合法值: bili, xhs, weibo。不指定 = 不限平台。",
)
@click.option(
    "--resource-sub",
    "resource_subs",
    multiple=True,
    help="限制 token 可访问订阅（复合 key <platform_short>:<id>，可多次）。"
    "如 --resource-sub bili:100 --resource-sub xhs:u456。不指定 = 不限订阅。",
)
def create(
    name: str,
    force: bool,
    scopes: tuple[str, ...],
    resource_platforms: tuple[str, ...],
    resource_subs: tuple[str, ...],
) -> None:
    """生成新 token，明文仅打印一次（存储为 SHA-256 hash，无法恢复）。

    ``--scope`` 可多次指定，限制 token 可访问的 API 范围（spec §4）。
    不传 ``--scope`` → 全权限 token（向后兼容老 bot，但建议生产环境显式收紧）。

    ``--resource-platform`` / ``--resource-sub`` 是行级过滤规则（issue #106）：
    限制 token 能看到哪些平台 / 订阅的消息。不指定 = 全权限（兼容老 token）。
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

    # ── 2. resource platform 白名单校验（行级规则，spec §9.1）─────
    valid_platforms_short = {"bili", "xhs", "weibo"}
    invalid_p = [p for p in resource_platforms if p not in valid_platforms_short]
    if invalid_p:
        console.print(
            f"[red]✗[/] 未知 platform: {', '.join(invalid_p)}",
            style="red",
        )
        console.print(
            f"[dim]合法: {', '.join(sorted(valid_platforms_short))}[/]"
        )
        sys.exit(1)

    # ── 3. resource sub 格式校验（必须是 <short>:<id> 且 short 合法）─
    invalid_s = [
        s
        for s in resource_subs
        if ":" not in s or s.split(":", 1)[0] not in valid_platforms_short
    ]
    if invalid_s:
        console.print(
            f"[red]✗[/] 非法 subscription_ref 格式: {', '.join(invalid_s)}",
            style="red",
        )
        console.print(
            "[dim]格式: <platform_short>:<id>，如 bili:100 / xhs:u456[/]"
        )
        sys.exit(1)

    # ── 4. 无意义组合 warning（不阻止创建，spec §11 风险表）────────
    # platforms=[bili] + subs=[xhs:u456] 无交集 → AND 后拒绝一切
    if resource_platforms and resource_subs:
        declared_platforms_in_subs = {s.split(":", 1)[0] for s in resource_subs}
        if not declared_platforms_in_subs.intersection(resource_platforms):
            console.print(
                "[yellow]⚠️[/] platforms 与 subscription_refs 无交集，"
                "token 将拒绝所有资源",
                style="yellow",
            )

    # ── 5. _token_exists / --force（同名覆盖检查）─────────────────
    if _token_exists(name) and not force:
        console.print(
            f"[red]✗[/] token '{name}' 已存在，加 --force 覆盖",
            style="red",
        )
        sys.exit(1)

    # ── 6. 落盘（带 resource_rules）──────────────────────────────
    from shared.config import ResourceRules

    scope_list = list(scopes)
    rules = ResourceRules(
        platforms=list(resource_platforms) if resource_platforms else None,
        subscription_refs=list(resource_subs) if resource_subs else None,
    )
    plain = create_token(name, scopes=scope_list, resource_rules=rules)
    console.print(f"[green]✓[/] 已创建 token '{name}'，明文（仅此一次）：")
    console.print(f"[yellow]{plain}[/]")
    console.print("[dim]存储为 SHA-256 hash，后续无法再查看明文。[/]")
    if scope_list:
        console.print(f"[cyan]📝[/] Scopes: {', '.join(scope_list)}")
    else:
        console.print(
            "[yellow]⚠️[/] 未指定 scope = [bold]无限制[/]（全权限）。"
            " 建议生产环境用 --scope 显式收紧。",
            style="yellow",
        )
    # 行级规则提示
    if rules.platforms is not None or rules.subscription_refs is not None:
        parts: list[str] = []
        if rules.platforms is not None:
            parts.append(f"platforms=[{', '.join(rules.platforms)}]")
        if rules.subscription_refs is not None:
            parts.append(f"subs=[{', '.join(rules.subscription_refs)}]")
        console.print(f"[cyan]🔍[/] Resource Rules: {' '.join(parts)}")


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
    table.add_column("Resource Rules")
    for t in cfg.api_tokens:
        created = datetime.fromtimestamp(t.created_at).strftime("%Y-%m-%d %H:%M")
        if t.scopes:
            scopes_str = ", ".join(t.scopes)
        else:
            scopes_str = "(无限制)"
        # 行级规则显示（issue #106）：默认全权限 → (unrestricted)
        rules = t.resource_rules
        if rules.platforms is None and rules.subscription_refs is None:
            rules_str = "(unrestricted)"
        else:
            parts: list[str] = []
            if rules.platforms is not None:
                parts.append(f"platforms=[{', '.join(rules.platforms)}]")
            if rules.subscription_refs is not None:
                parts.append(f"subs=[{', '.join(rules.subscription_refs)}]")
            rules_str = " ".join(parts)
        table.add_row(t.name, t.token_hash[:8], created, scopes_str, rules_str)
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
