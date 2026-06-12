"""yt-dlp 下载封装 - 支持视频/音频下载，处理各类失败情况"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from rich.console import Console

from shared.config import Config
from shared.constants import DOWNLOAD_TIMEOUT
from shared.protocols import DownloadResult

logger = logging.getLogger(__name__)
console = Console()


# ── 永久性失败关键词 ─────────────────────────────────────

_ACCESS_LIMITED_PATTERNS = [
    r"付费",
    r"会员",
    r"VIP",
    r"copyright",
    r"not available",
    r"removed",
    r"private",
    r"地理.*限制",
    r"region.*lock",
    r"免责声明.*无法",
]

_NOT_FOUND_PATTERNS = [
    r"404",
    r"not found",
    r"不存在",
    r"已删除",
]


def _classify_error(error_msg: str) -> tuple[bool, str]:
    """分类下载错误，判断是否为永久性访问限制。

    Args:
        error_msg: 错误消息

    Returns:
        (is_access_limited, note) 元组
    """
    for pattern in _ACCESS_LIMITED_PATTERNS:
        if re.search(pattern, error_msg, re.IGNORECASE):
            return True, f"访问受限 (匹配: {pattern})"
    for pattern in _NOT_FOUND_PATTERNS:
        if re.search(pattern, error_msg, re.IGNORECASE):
            return True, "视频不存在或已删除"
    return False, ""


async def download_video(
    bvid: str,
    config: Config,
    *,
    title: str = "",
) -> DownloadResult:
    """使用 yt-dlp 下载 B 站视频。

    优先下载音频（匹配 config.download.format），保存到 config.download.dir。
    支持传入 Cookie 文件用于需要登录的视频。

    Args:
        bvid: 视频 BV 号
        config: 全局配置
        title: 视频标题（用于日志，可选）

    Returns:
        DownloadResult 实例

    Raises:
        无 - 所有异常均被捕获并体现在 DownloadResult 中
    """
    url = f"https://www.bilibili.com/video/{bvid}"
    download_dir = Path(config.download.dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    display_name = title or bvid
    logger.info(f"开始下载: {display_name} ({bvid})")

    # 构建 yt-dlp 参数
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "-f",
        config.download.format,
        "-o",
        str(download_dir / "%(title).100s.%(ext)s"),
        "--print-after-finalize",
        "filepath:%(filepath)s",
    ]

    # 限制下载速度（可选）
    cmd.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=DOWNLOAD_TIMEOUT,  # 10 分钟超时
        )
    except asyncio.TimeoutError:
        logger.error(f"下载超时: {display_name} ({bvid})")
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error=f"下载超时 ({DOWNLOAD_TIMEOUT}s)",
        )
    except Exception as e:
        logger.error(f"下载进程异常: {display_name} ({bvid}): {e}")
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error=str(e),
        )

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        error_msg = stderr_text or stdout_text or f"exit code {proc.returncode}"
        is_limited, note = _classify_error(error_msg)
        logger.error(f"下载失败: {display_name} ({bvid}): {error_msg[:200]}")

        if is_limited:
            logger.info(f"标记为永久性访问限制: {note}")

        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error=error_msg[:500],
            access_limited=is_limited,
            access_note=note,
        )

    # 解析输出获取文件路径
    filepath: Optional[Path] = None
    for line in stdout_text.splitlines():
        if line.startswith("filepath:"):
            fp = line[len("filepath:") :].strip()
            if fp:
                filepath = Path(fp)
                break

    # 如果 yt-dlp 没有输出 filepath，尝试在下载目录查找
    if filepath is None or not filepath.exists():
        # 查找最近修改的文件
        try:
            files = sorted(
                download_dir.iterdir(),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            for f in files:
                if f.is_file() and not f.name.startswith("."):
                    filepath = f
                    break
        except OSError:
            pass

    if filepath and filepath.exists():
        logger.info(f"下载完成: {display_name} -> {filepath.name}")
    else:
        logger.warning(f"下载可能成功但未找到文件: {display_name} ({bvid})")

    return DownloadResult(
        success=True,
        source_id=bvid,
        title=display_name,
        filepath=filepath,
    )
