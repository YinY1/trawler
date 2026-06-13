"""Trawler CLI 入口 - Click Group with login/token/check subcommands."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import click
from rich.console import Console
from rich.table import Table

from core.pipeline import run_check_once
from shared.auth import QRExpiredError, get_authenticator, update_auth_section
from shared.auth.base import PlatformTokens
from shared.config import load_config

console = Console()


@click.group()
def cli() -> None:
    """Trawler - 多平台创作者内容追更自动化工作流"""
    pass


@cli.command()
@click.option(
    "--platform",
    type=click.Choice(["bili", "xhs", "weibo"]),
    required=True,
    help="登录的平台",
)
def login(platform: str) -> None:
    """二维码扫码登录"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )

    try:
        authenticator = get_authenticator(platform)
        tokens = asyncio.run(authenticator.qr_login())
        # Weibo stores cookies as a single semicolon-delimited string
        if platform in ("weibo", "xhs"):
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
        else:
            auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
        # ac_time_value is stored separately (not in PlatformTokens) — only for bilibili
        if platform == "bili" and hasattr(authenticator, "_last_ac_time_value") and authenticator._last_ac_time_value:
            auth_dict["ac_time_value"] = authenticator._last_ac_time_value
        update_auth_section(platform, auth_dict)
        console.print(f"[green]✓ {platform} 登录成功，凭证已保存[/]")
    except QRExpiredError:
        console.print("[red]✗ 二维码已过期，请重试[/]")
        sys.exit(1)
    except Exception as exc:
        console.print(f"[red]✗ 登录失败: {exc}[/]")
        sys.exit(1)


@cli.group()
def token() -> None:
    """Token 管理命令"""
    pass


@token.command("status")
def token_status() -> None:
    """查看各平台 token 状态"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    config = load_config("config.toml")

    table = Table(title="Token 状态")
    table.add_column("平台", style="bold")
    table.add_column("状态")
    table.add_column("过期时间")

    now = time.time()
    platforms = [
        ("bilibili", config.bilibili.auth),
        ("xiaohongshu", config.xiaohongshu.auth),
        ("weibo", config.weibo.auth),
    ]

    for name, auth in platforms:
        if auth.expires_at <= 0:
            table.add_row(name, "[dim]未配置[/]", "-")
        elif auth.expires_at < now:
            table.add_row(name, "[red]已过期[/]", time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at)))
        else:
            remaining = auth.expires_at - now
            days = int(remaining // 86400)
            table.add_row(
                name,
                f"[green]有效[/] (剩余 {days} 天)",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(auth.expires_at)),
            )

    console.print(table)


@token.command("refresh")
@click.option(
    "--platform",
    type=click.Choice(["bili", "xhs", "weibo"]),
    required=True,
    help="续期的平台",
)
def token_refresh(platform: str) -> None:
    """手动续期 token"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )

    config = load_config("config.toml")

    if platform == "bili":
        auth = config.bilibili.auth
        if auth.expires_at <= 0 or auth.expires_at < time.time():
            console.print("[red]✗ Token 已过期或未配置，请先执行 trawler login --platform bili[/]")
            sys.exit(1)
        try:
            authenticator = get_authenticator(platform)
            bili_auth = config.bilibili.auth
            current_tokens = PlatformTokens(
                platform=platform,
                cookies={
                    "SESSDATA": bili_auth.sessdata,
                    "bili_jct": bili_auth.bili_jct,
                    "buvid3": bili_auth.buvid3,
                    "DedeUserID": bili_auth.dedeuserid,
                },
                obtained_at=time.time(),
                expires_at=bili_auth.expires_at,
            )
            tokens = asyncio.run(authenticator.refresh_tokens(current_tokens))
            auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
            # ac_time_value is stored separately (not in PlatformTokens)
            if hasattr(authenticator, "_last_ac_time_value") and authenticator._last_ac_time_value:
                auth_dict["ac_time_value"] = authenticator._last_ac_time_value
            update_auth_section(platform, auth_dict)
            console.print(f"[green]✓ {platform} Token 续期成功[/]")
        except Exception as exc:
            console.print(f"[red]✗ 续期失败: {exc}[/]")
            sys.exit(1)

    elif platform == "weibo":
        auth = config.weibo.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            console.print("[red]✗ 未配置微博 Cookie 或已过期，请先执行 trawler login --platform weibo[/]")
            sys.exit(1)
        try:
            from platforms.weibo.auth import WeiboAuthenticator

            authenticator = WeiboAuthenticator()
            # Parse single cookie string into individual keys
            cookie_dict: dict[str, str] = {}
            for part in auth.cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    cookie_dict[k] = v
            current_tokens = PlatformTokens(
                platform="weibo",
                cookies=cookie_dict,
                obtained_at=time.time(),
                expires_at=auth.expires_at,
            )
            tokens = asyncio.run(authenticator.refresh_tokens(current_tokens))
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
            update_auth_section(platform, auth_dict)
            console.print("[green]✓ weibo Token 续期成功[/]")
        except Exception as exc:
            console.print(f"[red]✗ 续期失败: {exc}[/]")
            sys.exit(1)

    elif platform == "xhs":
        auth = config.xiaohongshu.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            console.print("[red]✗[/] 未配置小红书 Cookie 或已过期，请先执行 trawler login --platform xhs")
            sys.exit(1)
        try:
            from platforms.xiaohongshu.auth import XhsAuthenticator

            authenticator = XhsAuthenticator()
            cookie_dict: dict[str, str] = {}
            for part in auth.cookie.split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    cookie_dict[k] = v
            current_tokens = PlatformTokens(
                platform="xhs",
                cookies=cookie_dict,
                obtained_at=time.time(),
                expires_at=auth.expires_at,
            )
            tokens = asyncio.run(authenticator.refresh_tokens(current_tokens))
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
            update_auth_section(platform, auth_dict)
            console.print("[green]✓[/] xhs Token 续期成功")
        except Exception as exc:
            console.print(f"[red]✗[/] 续期失败: {exc}")
            sys.exit(1)


@cli.command()
@click.option(
    "--platform",
    type=click.Choice(["all", "bili", "xhs", "weibo"]),
    default="all",
    help="检查的平台 (all/bili/xhs/weibo)",
)
@click.option(
    "--config",
    "config_path",
    default="config.toml",
    show_default=True,
    help="配置文件路径",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="启用详细日志输出",
)
def check(platform: str, config_path: str, verbose: bool) -> None:
    """检查各平台新内容"""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    if verbose:
        console.print("[dim]调试模式已启用[/]")
    try:
        config = load_config(config_path)
    except Exception as exc:
        console.print(f"[red]✗ 配置加载失败: {exc}[/]")
        sys.exit(1)
    try:
        asyncio.run(run_check_once(config, platform, config_path))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[red]✗ 运行出错: {exc}[/]")
        if verbose:
            console.print_exception()
        sys.exit(1)


if __name__ == "__main__":
    cli()
