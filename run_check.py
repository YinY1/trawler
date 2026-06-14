"""Trawler CLI 入口 - Click Group with login/token/check subcommands."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from core.pipeline import run_check_once
from shared.auth import QRExpiredError, get_authenticator, update_auth_section
from shared.auth.base import PlatformTokens
from shared.config import Config, load_config

console = Console()


def setup_logging(verbose: bool = False, log_dir: str = "data") -> None:
    """配置日志：控制台 + 文件轮转（幂等，重复调用不叠加 handler）。"""
    root = logging.getLogger()
    if root.handlers:
        return  # 已配置，幂等跳过

    log_level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    root.setLevel(log_level)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(console_handler)

    # 文件 handler（轮转：5MB × 3 个备份）
    log_path = Path(log_dir) / "trawler.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_handler)


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
        ac_val = authenticator.ac_time_value
        if platform == "bili" and ac_val:
            auth_dict["ac_time_value"] = ac_val
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
    default=None,
    help="续期的平台",
)
@click.option(
    "--all",
    "refresh_all",
    is_flag=True,
    default=False,
    help="续期所有已配置平台",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="强制刷新（跳过续期检查，直接调用刷新接口）",
)
def token_refresh(platform: str | None, refresh_all: bool, force: bool) -> None:
    """手动续期 token"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    )

    config = load_config("config.toml")

    if refresh_all:
        targets = [p for p in ["bili", "xhs", "weibo"] if _is_platform_configured(p, config, force)]
    elif platform:
        targets = [platform]
    else:
        console.print("[red]✗ 请指定 --platform 或 --all[/]")
        sys.exit(1)

    any_failure = False
    for plat in targets:
        if not _refresh_single_platform(plat, config, force):
            any_failure = True

    if any_failure:
        sys.exit(1)


def _is_platform_configured(platform: str, config: Config, force: bool = False) -> bool:
    """Check if a platform has auth credentials configured.

    When force=True, skips the expiry check (still requires credentials to exist).
    """
    if platform == "bili":
        if not (config.bilibili.auth.sessdata and config.bilibili.auth.bili_jct):
            return False
        if force:
            return True
        return config.bilibili.auth.expires_at > time.time()
    elif platform == "weibo":
        if not config.weibo.auth.cookie:
            return False
        if force:
            return True
        return config.weibo.auth.expires_at > time.time()
    elif platform == "xhs":
        if not config.xiaohongshu.auth.cookie:
            return False
        if force:
            return True
        return config.xiaohongshu.auth.expires_at > time.time()
    return False


def _refresh_single_platform(platform: str, config: Config, force: bool = False) -> bool:
    """Refresh tokens for a single platform. Returns True on success, False on failure.

    This function does not call sys.exit(), allowing callers like --all to continue
    processing remaining platforms even if one fails.
    """
    if platform == "bili":
        auth = config.bilibili.auth
        if not force and (auth.expires_at <= 0 or auth.expires_at < time.time()):
            console.print("[red]✗[/] Token 已过期或未配置，请先执行 trawler login --platform bili")
            return False
        try:
            authenticator = get_authenticator(platform)
            bili_auth = config.bilibili.auth
            current_tokens = PlatformTokens(
                platform=platform,
                cookies={
                    "sessdata": bili_auth.sessdata,
                    "bili_jct": bili_auth.bili_jct,
                    "buvid3": bili_auth.buvid3 or "",
                    "dedeuserid": bili_auth.dedeuserid or "",
                },
                obtained_at=time.time(),
                expires_at=bili_auth.expires_at,
            )
            tokens = asyncio.run(authenticator.refresh_tokens(current_tokens))
            auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
            ac_val = authenticator.ac_time_value
            if ac_val:
                auth_dict["ac_time_value"] = ac_val
            update_auth_section(platform, auth_dict)
            console.print(f"[green]✓[/] {platform} Token 续期成功")
            return True
        except Exception as exc:
            console.print(f"[red]✗[/] 续期失败: {exc}")
            return False

    elif platform == "weibo":
        auth = config.weibo.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            console.print("[red]✗[/] 未配置微博 Cookie 或已过期，请先执行 trawler login --platform weibo")
            return False
        try:
            from platforms.weibo.auth import WeiboAuthenticator

            authenticator = WeiboAuthenticator()
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
            update_auth_section("weibo", auth_dict)
            console.print("[green]✓[/] weibo Token 续期成功")
            return True
        except Exception as exc:
            console.print(f"[red]✗[/] 续期失败: {exc}")
            return False

    elif platform == "xhs":
        auth = config.xiaohongshu.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            console.print("[red]✗[/] 未配置小红书 Cookie 或已过期，请先执行 trawler login --platform xhs")
            return False
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
            update_auth_section("xhs", auth_dict)
            console.print("[green]✓[/] xhs Token 续期成功")
            return True
        except Exception as exc:
            console.print(f"[red]✗[/] 续期失败: {exc}")
            return False

    else:
        console.print(f"[red]✗ 未知平台: {platform}[/]")
        return False


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
@click.option(
    "--from-phase",
    default=None,
    type=click.Choice(["discovered", "downloaded", "transcribed", "summarized"], case_sensitive=False),
    help="从指定阶段开始处理（不指定则自动断点续传）",
)
def check(platform: str, config_path: str, verbose: bool, from_phase: str | None) -> None:
    """检查各平台新内容"""
    try:
        config = load_config(config_path)
    except Exception as exc:
        console.print(f"[red]✗ 配置加载失败: {exc}[/]")
        sys.exit(1)

    setup_logging(verbose=verbose, log_dir=config.general.data_dir)
    if verbose:
        console.print("[dim]调试模式已启用[/]")
    try:
        asyncio.run(run_check_once(config, platform, config_path, from_phase=from_phase))
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
