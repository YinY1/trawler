"""B站视频下载 - 使用 bilibili_api 绕过 yt-dlp 的 412 错误"""

from __future__ import annotations

# pyright: basic
import logging
import re
import tempfile
from pathlib import Path

from shared.config import Config
from shared.protocols import DownloadResult

logger = logging.getLogger(__name__)


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


def _write_bili_cookies(config: Config) -> Path | None:
    """将 B站 登录凭证写入临时 Netscape cookie 文件。"""
    auth = config.bilibili.auth
    if not auth.sessdata or not auth.bili_jct:
        return None

    # Netscape cookie format
    cookie_lines = [
        "# Netscape HTTP Cookie File",
        ".bilibili.com\tTRUE\t/\tTRUE\t1735689600\tsessdata\t" + auth.sessdata,
        ".bilibili.com\tTRUE\t/\tTRUE\t1735689600\tbili_jct\t" + auth.bili_jct,
    ]
    if auth.buvid3:
        cookie_lines.append(".bilibili.com\tTRUE\t/\tTRUE\t1735689600\tbuvid3\t" + auth.buvid3)
    if auth.dedeuserid:
        cookie_lines.append(".bilibili.com\tTRUE\t/\tTRUE\t1735689600\tdedeuserid\t" + auth.dedeuserid)

    fp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="bili_cookies_")
    fp.write("\n".join(cookie_lines) + "\n")
    fp.close()
    return Path(fp.name)


# ── B站 URL 工具 ──────────────────────────────────────────


def _is_bili_url(url: str) -> bool:
    """检测是否为 B站 链接"""
    return "bilibili.com" in url


def _extract_bvid(url: str) -> str | None:
    """从 URL 中提取 BVID"""
    m = re.search(r"(BV[\w]+)", url)
    return m.group(1) if m else None


# ── B站 API 下载 (绕过 yt-dlp 412 错误) ─────────────────────


async def _download_bili_video(
    bvid: str,
    config: Config,
    download_dir: Path,
    display_name: str,
) -> DownloadResult:
    """使用 bilibili_api 获取直链下载 B站 音频，绕过 yt-dlp 的 HTTP 412 错误。"""
    import aiohttp

    auth = config.bilibili.auth
    if not auth.sessdata or not auth.bili_jct:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error="B站未配置登录凭证",
            permanent=True,  # 配置错误不会因 retry 消失
        )

    from bilibili_api import Credential, video

    cred = Credential(
        sessdata=auth.sessdata,
        bili_jct=auth.bili_jct,
        buvid3=auth.buvid3 or "",
        dedeuserid=auth.dedeuserid or "",
    )

    v = video.Video(bvid=bvid, credential=cred)

    try:
        info = await v.get_info()
    except Exception as e:
        # bilibili_api 对 404/不存在/参数错误抛异常，但和网络异常无法区分；
        # 保守不标 permanent，让 retry 兜底（临时网络抖动比 BVID 不存在更常见）。
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error=f"获取视频信息失败: {e}",
        )

    pages = info.get("pages", [])
    if not pages:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error="无法获取视频页面信息",
            permanent=True,  # 视频数据结构异常，retry 无意义
        )

    cid = pages[0].get("cid")
    if not cid:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error="无法获取视频 CID",
            permanent=True,
        )

    try:
        urls = await v.get_download_url(cid=cid)
    except Exception as e:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error=f"获取下载地址失败: {e}",
        )

    dash = urls.get("dash", {})
    audios = dash.get("audio", [])
    if not audios:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error="无可用音频流",
            permanent=True,  # 视频可能没有音频流（如纯图片动态），结构问题
        )

    # 按配置选择音频流（按 bandwidth 排序）
    quality = (config.download.quality or "").lower()
    if quality in ("best", "bestaudio"):
        audios.sort(key=lambda a: a.get("bandwidth", 0) or 0, reverse=True)
    elif quality in ("worst", "worstaudio"):
        audios.sort(key=lambda a: a.get("bandwidth", 0) or 0)
    # 默认保持原序，取第一条

    audio_url = audios[0].get("baseUrl", "") or audios[0].get("url", "")
    if not audio_url:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error="音频 URL 为空",
            permanent=True,
        )

    # ── 下载音频 ──
    filepath = download_dir / f"{display_name[:80]}.m4a"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            " AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }

    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.get(
                audio_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                if resp.status != 200:
                    return DownloadResult(
                        success=False,
                        source_id=bvid,
                        title=display_name,
                        error=f"下载失败 HTTP {resp.status}",
                    )
                with open(filepath, "wb") as f:
                    while True:
                        chunk = await resp.content.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
    except Exception as e:
        return DownloadResult(
            success=False,
            source_id=bvid,
            title=display_name,
            error=str(e),
        )

    if filepath.exists():
        size_mb = filepath.stat().st_size / 1024 / 1024
        logger.info("⬇ 下载完成: %s -> %s (%.1f MB)", display_name, filepath.name, size_mb)
    else:
        logger.warning("⬇ 下载可能成功但未找到文件: %s (%s)", display_name, bvid)

    return DownloadResult(
        success=True,
        source_id=bvid,
        title=display_name,
        filepath=filepath,
    )


# ── 公开接口 ─────────────────────────────────────────────────


async def download_video(
    bvid: str,
    config: Config,
    *,
    title: str = "",
) -> DownloadResult:
    """下载 B 站视频音频。

    使用 bilibili_api 获取直接下载地址，绕过 yt-dlp 的 HTTP 412 错误。
    保存到 config.download.dir。

    Args:
        bvid: 视频 BV 号
        config: 全局配置
        title: 视频标题（用于日志，可选）

    Returns:
        DownloadResult 实例

    Raises:
        无 - 所有异常均被捕获并体现在 DownloadResult 中
    """
    download_dir = Path(config.download.dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    display_name = title or bvid
    logger.info("⬇ 开始下载: %s (%s)", display_name, bvid)

    return await _download_bili_video(bvid, config, download_dir, display_name)
