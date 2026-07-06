"""Trawler CLI 入口 - Click Group with login/token/check subcommands."""

from __future__ import annotations

# pyright: basic
import asyncio
import json
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from core.notifiers import send_to_subscription
from core.pipeline import run_check_once
from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
from shared.auth import QRExpiredError, get_authenticator, update_auth_section
from shared.auth.base import PlatformTokens
from shared.config import Config, load_config
from shared.constants import VERSION_DISPLAY
from shared.message_store import MessageStore
from shared.protocols import NotificationContent, Phase

console = Console()
logger = logging.getLogger(__name__)


def parse_since(value: str) -> int:
    """解析 ``--since`` 参数为 Unix 时间戳（手动检查模式专用，plan 2026-06-28）。

    支持两种格式：
    - 相对：``24h`` / ``7d`` / ``30m``（h=小时, d=天, m=分钟）
    - 绝对：``2026-06-01`` 或 ``2026-06-01T12:00:00``（本地时区，与 pubdate 存储一致）

    Raises:
        ValueError: 格式无法识别
    """
    # 相对格式：数字 + 单位（h/d/m）
    match = re.fullmatch(r"(\d+)([hmd])", value)
    if match:
        num, unit = int(match.group(1)), match.group(2)
        multiplier = {"h": 3600, "m": 60, "d": 86400}[unit]
        return int(time.time()) - num * multiplier
    # 绝对格式：纯日期 / 日期+时间
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(value, fmt)))
        except ValueError:
            continue
    raise ValueError(f"无法解析 --since 值: {value!r}（支持格式: 24h / 7d / 30m / 2026-06-01）")


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
    file_handler = RotatingFileHandler(str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    root.addHandler(file_handler)


@click.group()
@click.version_option(VERSION_DISPLAY, "-V", "--version", message="Trawler %(version)s")
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
        logger.info("🔑 %s 登录流程启动...", platform)

        from shared.auth.base import AuthStatus, QRStatus

        def _on_status(status: AuthStatus) -> None:
            if status.status == QRStatus.SCANNED:
                logger.info("🔑 %s 已扫码，等待确认", platform)
                console.print("  [green]✓[/] 已扫码，请在手机上确认")
            elif status.status == QRStatus.SUCCESS:
                logger.info("🔑 %s 扫码成功", platform)
                console.print("  [green]✓[/] 登录成功")

        logger.info("🔑 %s 等待扫码...", platform)
        console.print("  [dim]等待扫码中...（每 2 秒检测一次）[/]")
        tokens = asyncio.run(authenticator.qr_login(on_status=_on_status))
        # Weibo stores cookies as a single semicolon-delimited string
        if platform in ("weibo", "xhs"):
            cookie_str = "; ".join(f"{k}={v}" for k, v in tokens.cookies.items())
            auth_dict = {"cookie": cookie_str, "expires_at": tokens.expires_at}
        else:
            auth_dict = {**tokens.cookies, "expires_at": tokens.expires_at}
        # refresh_token is stored separately (not in PlatformTokens) — only for bilibili
        rt_val = authenticator.refresh_token
        if platform == "bili" and rt_val:
            auth_dict["refresh_token"] = rt_val
        asyncio.run(update_auth_section(platform, auth_dict))
        # Save debug tokens JSON for integration tests (avoid re-scan)
        debug_path = Path("tests") / f"{platform}_debug_tokens.json"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_data = {
            "platform": tokens.platform,
            "cookies": tokens.cookies,
            "obtained_at": tokens.obtained_at,
            "expires_at": tokens.expires_at,
        }
        debug_path.write_text(json.dumps(debug_data, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]🔑 Debug tokens saved to {debug_path}[/]")
        console.print(f"[green]✓ {platform} 登录成功，凭证已保存[/]")
        logger.info("🔑 %s 登录成功", platform)
    except QRExpiredError:
        logger.warning("🔑 %s 二维码已过期", platform)
        console.print("[red]✗ 二维码已过期，请重试[/]")
        sys.exit(1)
    except Exception as exc:
        logger.warning("🔑 %s 登录失败: %s", platform, exc)
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
    config = asyncio.run(load_config("config/config.toml"))

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

    config = asyncio.run(load_config("config/config.toml"))

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
            logger.warning("🔑 %s token 已过期或未配置", platform)
            console.print("[red]✗[/] Token 已过期或未配置，请先执行 trawler login --platform bili")
            return False
        try:
            logger.info("🔑 %s Token 续期开始...", platform)
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
            rt_val = authenticator.refresh_token
            if rt_val:
                auth_dict["refresh_token"] = rt_val
            asyncio.run(update_auth_section(platform, auth_dict))
            logger.info("🔑 %s Token 续期成功", platform)
            console.print(f"[green]✓[/] {platform} Token 续期成功")
            return True
        except Exception as exc:
            logger.warning("🔑 %s Token 续期失败: %s", platform, exc)
            console.print(f"[red]✗[/] 续期失败: {exc}")
            return False

    elif platform == "weibo":
        auth = config.weibo.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            logger.warning("🔑 %s token 已过期或未配置", platform)
            console.print("[red]✗[/] 未配置微博 Cookie 或已过期，请先执行 trawler login --platform weibo")
            return False
        try:
            logger.info("🔑 %s Token 续期开始...", platform)
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
            asyncio.run(update_auth_section("weibo", auth_dict))
            logger.info("🔑 %s Token 续期成功", platform)
            console.print("[green]✓[/] weibo Token 续期成功")
            return True
        except Exception as exc:
            logger.warning("🔑 %s Token 续期失败: %s", platform, exc)
            console.print(f"[red]✗[/] 续期失败: {exc}")
            return False

    elif platform == "xhs":
        auth = config.xiaohongshu.auth
        if not auth.cookie or auth.expires_at <= 0 or auth.expires_at < time.time():
            logger.warning("🔑 %s token 已过期或未配置", platform)
            console.print("[red]✗[/] 未配置小红书 Cookie 或已过期，请先执行 trawler login --platform xhs")
            return False
        try:
            logger.info("🔑 %s Token 续期开始...", platform)
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
            asyncio.run(update_auth_section("xhs", auth_dict))
            logger.info("🔑 %s Token 续期成功", platform)
            console.print("[green]✓[/] xhs Token 续期成功")
            return True
        except Exception as exc:
            logger.warning("🔑 %s Token 续期失败: %s", platform, exc)
            console.print(f"[red]✗[/] 续期失败: {exc}")
            return False

    else:
        logger.warning("🔑 未知平台: %s", platform)
        console.print(f"[red]✗ 未知平台: {platform}[/]")
        return False


@cli.group()
def subscription() -> None:
    """订阅管理命令"""
    pass


@subscription.command("add")
@click.option(
    "--platform",
    type=click.Choice(["bili", "xhs", "weibo"]),
    required=True,
    help="平台",
)
@click.option("--id", "identifier", default=None, help="订阅标识（B站 UID / 小红书 user_id / 微博 user_id）")
@click.option("--search-name", default=None, help="按名称搜索并添加（支持 bili/weibo/xhs，需已登录）")
@click.option("--name", default=None, help="订阅名称（与 --id 搭配使用）")
def sub_add(platform: str, identifier: str | None, search_name: str | None, name: str | None) -> None:
    """添加订阅"""
    if search_name:
        # ── 按名称搜索 ────────────────────────────────────────
        ok, msg, candidates = asyncio.run(search_by_name(platform, search_name))
        if not ok:
            console.print(f"[red]✗[/] {msg}")
            sys.exit(1)

        if len(candidates) == 1:
            c = candidates[0]
            cid = c.get("uid", c.get("user_id", ""))
            cname = c.get("name", search_name)
            ok2, msg2 = asyncio.run(add_subscription(platform, cid, cname))
            if ok2:
                console.print(f"[green]✓[/] {msg2} (ID: {cid})")
            else:
                console.print(f"[red]✗[/] {msg2}")
                sys.exit(1)
        else:
            console.print("[yellow]⚠️[/] 找到多个匹配用户:")
            for c in candidates:
                cid = c.get("uid", c.get("user_id", ""))
                cname = c.get("name", "?")
                console.print(f"  [dim]- {cname} (ID: {cid})[/]")
            console.print("请使用 [bold]--id[/] 指定正确的标识再添加")
            sys.exit(1)
    elif identifier:
        if not name:
            console.print("[red]✗ 使用 --id 时需要同时提供 --name[/]")
            sys.exit(1)
        ok, msg = asyncio.run(add_subscription(platform, identifier, name))
        if ok:
            console.print(f"[green]✓[/] {msg}")
        else:
            console.print(f"[red]✗[/] {msg}")
            sys.exit(1)
    else:
        console.print("[red]✗ 请提供 --id + --name 或 --search-name[/]")
        sys.exit(1)


@subscription.command("remove")
@click.option(
    "--platform",
    type=click.Choice(["bili", "xhs", "weibo"]),
    required=True,
    help="平台",
)
@click.option("--id", "identifier", required=True, help="订阅标识（B站 UID / 小红书 user_id / 微博 user_id）")
def sub_remove(platform: str, identifier: str) -> None:
    """删除订阅"""
    ok, msg = asyncio.run(remove_subscription(platform, identifier))
    if ok:
        console.print(f"[green]✓[/] {msg}")
    else:
        console.print(f"[red]✗[/] {msg}")
        sys.exit(1)


@subscription.command("list")
@click.option(
    "--platform",
    type=click.Choice(["bili", "xhs", "weibo"]),
    default=None,
    help="按平台筛选",
)
def sub_list(platform: str | None) -> None:
    """列出所有订阅"""
    subs = asyncio.run(list_subscriptions(platform=platform))

    if not subs:
        console.print("[dim]暂无订阅[/]")
        return

    table = Table(title="订阅列表")
    table.add_column("平台", style="bold")
    table.add_column("标识")
    table.add_column("名称")

    # Platform display names
    display_names = {
        "bilibili": "B站",
        "xiaohongshu": "小红书",
        "weibo": "微博",
    }

    for section, items in subs.items():
        if not items:
            continue
        for item in items:
            ident = item.get("uid") or item.get("user_id") or "-"
            table.add_row(display_names.get(section, section), str(ident), item.get("name", "-"))

    console.print(table)


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
    default="config/config.toml",
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
# ↓ 手动检查筛选选项（plan 2026-06-28-manual-content-check）
@click.option(
    "--since",
    default=None,
    help="时间起点筛选（触发手动模式）：相对(24h/7d/30m) 或绝对(2026-06-01)",
)
@click.option(
    "--title",
    default=None,
    help="标题模糊匹配，大小写不敏感 substring（触发手动模式）",
)
@click.option(
    "--author",
    default=None,
    help="作者模糊匹配，大小写不敏感 substring（触发手动模式）",
)
@click.option(
    "--reset-phase",
    "reset_phase",
    default="summarized",
    type=click.Choice(["discovered", "downloaded", "transcribed", "summarized"], case_sensitive=False),
    show_default=True,
    help="手动模式重跑起始阶段（仅手动模式生效）",
)
@click.option(
    "--skip-push/--no-skip-push",
    default=True,
    show_default=True,
    help="是否跳过推送通知（手动模式默认跳过，避免重复打扰订阅者）",
)
def check(
    platform: str,
    config_path: str,
    verbose: bool,
    from_phase: str | None,
    since: str | None,
    title: str | None,
    author: str | None,
    reset_phase: str,
    skip_push: bool,
) -> None:
    """检查各平台新内容。

    传 --since / --title / --author 任一即进入「手动检查模式」：
    按筛选条件查询已存在的消息，从 --reset-phase（默认 summarized）阶段重跑流水线。
    默认 --skip-push（不重新推送通知），加 --no-skip-push 才真正推送。
    不传筛选参数则走原 cron 全量扫描路径（run_check_once）。
    """
    try:
        config = asyncio.run(load_config(config_path))
    except Exception as exc:
        console.print(f"[red]✗ 配置加载失败: {exc}[/]")
        sys.exit(1)

    setup_logging(verbose=verbose, log_dir=config.general.data_dir)
    if verbose:
        console.print("[dim]调试模式已启用[/]")

    # 判断是否手动模式：传了任意筛选参数
    manual_mode = any([since, title, author])

    try:
        if manual_mode:
            asyncio.run(
                _run_manual_check(
                    config, platform, since, title, author, reset_phase, skip_push,
                )
            )
        else:
            asyncio.run(run_check_once(config, platform, config_path, from_phase=from_phase))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[red]✗ 运行出错: {exc}[/]")
        if verbose:
            console.print_exception()
        # Bug 4 fix: cron 失败时推送健康告警到所有配置的 endpoints，
        # 让运维在 cron 静默失败时也能收到通知
        try:
            health_alert = NotificationContent(
                platform="system",
                source_id="health",
                title="Trawler 检查失败",
                author="Trawler",
                summary=f"check 命令执行失败: {exc}",
                type="health_alert",
            )
            # 发到所有已配置且 enabled 的 endpoints（platform 传空字符串避免过滤）
            all_endpoint_names = [ep.name for ep in config.endpoints if ep.enabled]
            if all_endpoint_names:
                logger.warning("🚨 推送健康告警到 %d 个 endpoint", len(all_endpoint_names))
                # check 是同步 Click 命令，run_check_once 的事件循环已随异常退出，
                # 这里开一个新的 asyncio.run 推送告警
                asyncio.run(send_to_subscription(config, "system", all_endpoint_names, health_alert))
        except Exception as alert_exc:
            logger.error("推送健康告警失败: %s", alert_exc)
        sys.exit(1)


async def _run_manual_check(
    config: Config,
    platform: str,
    since: str | None,
    title: str | None,
    author: str | None,
    reset_phase: str,
    skip_push: bool,
) -> None:
    """手动模式：按筛选条件查询消息并重跑（plan 2026-06-28-manual-content-check）。

    ⚠️ VIDEO + reset_phase=summarized 实际会从 download 开始跑全流水线：
    ``PipelineEngine.process_message`` 的 Bug-3 修复会在 ``ctx.downloaded_filepath is None``
    时把 VIDEO 消息回退到 DISCOVERED。手动模式每次创建新 ctx，filepath 必为 None
    （跨进程不可恢复），所以 VIDEO 消息重跑 summarize 实际会重新下载。
    """
    # 延迟导入避免模块加载顺序问题
    from core.engine import PipelineEngine

    store = MessageStore(config.general.data_dir)
    # ⚠️ 不调 cleanup（D6：避免误删超 24h 的历史消息）

    since_ts = parse_since(since) if since else None
    platform_filter = None if platform == "all" else platform
    target_phase = Phase[reset_phase.upper()]

    matched = store.query_messages(
        since=since_ts,
        title=title,
        author=author,
        platform=platform_filter,
    )
    if not matched:
        console.print("[yellow]⚠️[/] 没有匹配的消息")
        return

    # 显示匹配结果
    table = Table(title=f"匹配 {len(matched)} 条消息")
    table.add_column("ID", style="dim")
    table.add_column("标题")
    table.add_column("平台")
    table.add_column("作者")
    table.add_column("当前阶段")
    for m in matched:
        table.add_row(m.msg_id, m.title[:30], m.platform, m.author, m.phase.name)
    console.print(table)

    console.print(f"[bold blue]▶[/] 从 {reset_phase.upper()} 阶段重跑，skip_push={skip_push}")

    msg_ids = [m.msg_id for m in matched]
    await PipelineEngine.run_specific_messages(
        msg_ids=msg_ids,
        from_phase=target_phase,
        skip_push=skip_push,
        config=config,
        store=store,
    )


# ═══════════════════════════════════════════════════════════
# 命令: fetch（按需消息处理，issue #101）
# ═══════════════════════════════════════════════════════════


@cli.command()
@click.option(
    "--ids",
    "ids",
    required=True,
    help="逗号分隔的消息 ID，如 bili:BV1xx,xhs:note1,weibo:123",
)
@click.option(
    "--skip-push",
    is_flag=True,
    default=False,
    help="跳过推送通知（默认推送）",
)
@click.option(
    "--config",
    "config_path",
    default="config/config.toml",
    show_default=True,
    help="配置文件路径",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="启用详细日志输出",
)
def fetch(
    ids: str,
    skip_push: bool,
    config_path: str,
    verbose: bool,
) -> None:
    """按指定消息 ID 抓取并处理（不依赖订阅）。

    - 对每个 ID：不存在则抓取入库 + 走完整流水线；已存在则直接处理
    - 默认推送给订阅者（--skip-push 跳过）
    - 突破 24h 时间窗限制，允许任意历史消息

    msg_id 必须带平台前缀（bili:/xhs:/weibo:），无前缀会被拒绝。
    """
    try:
        config = asyncio.run(load_config(config_path))
    except Exception as exc:
        console.print(f"[red]✗ 配置加载失败: {exc}[/]")
        sys.exit(1)

    setup_logging(verbose=verbose, log_dir=config.general.data_dir)
    if verbose:
        console.print("[dim]调试模式已启用[/]")

    # 拆分 + 前缀校验（快速失败）
    msg_ids = [m.strip() for m in ids.split(",") if m.strip()]
    valid_prefixes = {"bili:", "xhs:", "weibo:"}
    invalid = [m for m in msg_ids if not any(m.startswith(p) for p in valid_prefixes)]
    if invalid:
        console.print(
            f"[red]✗[/] 无效的 msg_id（需 bili:/xhs:/weibo: 前缀）: {invalid}"
        )
        sys.exit(1)

    console.print(f"[bold blue]▶[/] 按需抓取处理 {len(msg_ids)} 条消息")
    console.print(f"[dim]{' / '.join(msg_ids)}[/]")

    store = MessageStore(config.general.data_dir)

    # 延迟导入避免模块加载顺序问题（与 _run_manual_check 同款）
    from core.engine import PipelineEngine

    try:
        asyncio.run(
            PipelineEngine.run_fetch_and_process(
                msg_ids=msg_ids,
                skip_push=skip_push,
                config=config,
                store=store,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[red]✗ 运行出错: {exc}[/]")
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    console.print("[green]✓[/] 处理完成")


if __name__ == "__main__":
    cli()
