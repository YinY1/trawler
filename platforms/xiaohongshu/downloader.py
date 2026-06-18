"""小红书笔记下载模块 - 三层降级策略下载笔记内容"""

from __future__ import annotations

# pyright: basic
import logging
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import aiohttp

from platforms.xiaohongshu.auth import (
    get_xhs_cookie,
)
from platforms.xiaohongshu.client import XhsClient
from shared.config import Config
from shared.constants import XHS_DOWNLOAD_TIMEOUT
from shared.protocols import NoteInfo, XhsDownloadResult

logger = logging.getLogger("trawler.xiaohongshu.downloader")

# 图片下载基础 URL
IMAGE_CDN_BASE = "https://sns-img-bd.xhscdn.com/"


def _get_note_dir(config: Config, note_id: str) -> Path:
    """获取笔记下载目录。

    Args:
        config: 全局配置
        note_id: 笔记 ID

    Returns:
        笔记专用下载目录路径
    """
    base = Path(config.download.dir) / "xhs" / note_id
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── 第一层：XHS-Downloader Python 库 ──────────────────────


async def _try_xhs_downloader_lib(note: NoteInfo, config: Config) -> Optional[XhsDownloadResult]:
    """尝试使用 XHS-Downloader Python 库下载。

    Args:
        note: 笔记信息
        config: 全局配置

    Returns:
        下载结果或 None
    """
    try:
        from xhs import XhsClient  # type: ignore[import-untyped]

        cookie = get_xhs_cookie(config)
        client = XhsClient(cookie=cookie)

        note_detail = client.get_note_by_id(note.note_id)
        if not note_detail:
            return None

        note_dir = _get_note_dir(config, note.note_id)

        if note.note_type == "video":
            # 视频下载
            video_url = note_detail.get("video", {}).get("media", {}).get("stream", {})
            if isinstance(video_url, dict) and video_url:
                # 取最高质量
                for quality_key in ("h264", "h265", "av1"):
                    streams = video_url.get(quality_key, [])
                    for stream in streams:
                        url = stream.get("master_url", "") or stream.get("backup_urls", [""])[0]
                        if url:
                            video_path = note_dir / f"{note.note_id}.mp4"
                            await _download_file(url, video_path)
                            return XhsDownloadResult(
                                success=True,
                                source_id=note.note_id,
                                title=note.title,
                                filepath=video_path,
                            )

            return XhsDownloadResult(
                success=False,
                source_id=note.note_id,
                title=note.title,
                error="视频 URL 提取失败",
            )

        else:
            # 图文笔记
            content_text = note_detail.get("desc", "")
            image_list = note_detail.get("image_list", [])

            image_paths: list[Path] = []
            for idx, img in enumerate(image_list):
                img_url = img.get("url_default", "") or img.get("url", "")
                if not img_url:
                    continue
                if not img_url.startswith("http"):
                    img_url = urljoin(IMAGE_CDN_BASE, img_url)

                ext = ".jpg"
                if "png" in img_url:
                    ext = ".png"
                elif "webp" in img_url:
                    ext = ".webp"

                img_path = note_dir / f"{idx + 1}{ext}"
                await _download_file(img_url, img_path)
                image_paths.append(img_path)

            return XhsDownloadResult(
                success=True,
                source_id=note.note_id,
                title=note.title,
                image_paths=image_paths,
                content_text=content_text,
            )

    except ImportError:
        logger.debug("XHS-Downloader 库未安装，跳过第一层")
        return None
    except Exception as e:
        logger.debug(f"XHS-Downloader 库下载失败: {e}")
        return None


# ── 第二层：XHS-Downloader API Server ─────────────────────


async def _try_xhs_downloader_api(note: NoteInfo, config: Config) -> Optional[XhsDownloadResult]:
    """尝试通过 XHS-Downloader API Server 下载。

    需要在配置中指定 API Server 地址。

    Args:
        note: 笔记信息
        config: 全局配置

    Returns:
        下载结果或 None
    """
    # 从环境变量或配置获取 API 地址
    api_url = os.environ.get("XHS_DOWNLOADER_API", "")
    if not api_url:
        return None

    try:
        note_url = f"https://www.xiaohongshu.com/explore/{note.note_id}"

        async with aiohttp.ClientSession(trust_env=False) as session:
            # 提交下载任务
            payload = {"url": note_url}
            async with session.post(
                f"{api_url.rstrip('/')}/api/download",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    return None

                result = await resp.json(content_type=None)

        if not result.get("success", False):
            return None

        # API 通常返回下载后的文件路径
        data = result.get("data", {})
        file_path = data.get("filepath", "") or data.get("file_path", "")

        if not file_path:
            return None

        note_dir = _get_note_dir(config, note.note_id)

        if note.note_type == "video":
            src = Path(file_path)
            if src.exists():
                dest = note_dir / f"{note.note_id}{src.suffix or '.mp4'}"
                # 移动文件到目标目录
                import shutil

                shutil.move(str(src), str(dest))
                return XhsDownloadResult(
                    success=True,
                    source_id=note.note_id,
                    title=note.title,
                    filepath=dest,
                )

        return XhsDownloadResult(
            success=True,
            source_id=note.note_id,
            title=note.title,
            content_text=data.get("desc", ""),
        )

    except Exception as e:
        logger.debug(f"XHS-Downloader API 下载失败: {e}")
        return None


# ── 第三层：直接 HTTP 下载（兜底） ─────────────────────────


async def _fetch_note_detail(note: NoteInfo, cookie: str) -> Optional[dict[str, Any]]:
    """直接请求笔记详情 API (via XhsClient)。

    Args:
        note: 笔记信息
        cookie: Cookie

    Returns:
        笔记详情数据或 None
    """
    client = XhsClient(cookie=cookie)
    try:
        return await client.get_note_detail(note.note_id, note.xsec_token)
    except Exception as e:
        logger.debug(f"获取笔记详情失败: {e}")
        return None
    finally:
        await client.close()


async def _download_file(url: str, dest: Path) -> bool:
    """下载文件到指定路径。

    Args:
        url: 文件 URL
        dest: 目标路径

    Returns:
        是否成功
    """
    async with aiohttp.ClientSession(trust_env=False) as session:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)

            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=XHS_DOWNLOAD_TIMEOUT),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"下载文件失败，状态码: {resp.status}, URL: {url}")
                    return False

                content = await resp.read()

            dest.write_bytes(content)
            return True

        except Exception as e:
            logger.debug(f"下载文件异常: {e}, URL: {url}")
            return False


async def _try_direct_download(note: NoteInfo, config: Config) -> XhsDownloadResult:
    """直接 HTTP 下载笔记内容（兜底方案）。

    Args:
        note: 笔记信息
        config: 全局配置

    Returns:
        下载结果
    """
    cookie = get_xhs_cookie(config)
    note_dir = _get_note_dir(config, note.note_id)

    # 获取笔记详情
    detail = await _fetch_note_detail(note, cookie)

    content_text = ""
    if detail:
        content_text = detail.get("desc", "") or note.desc
    else:
        content_text = note.desc

    if note.note_type == "video":
        # 尝试提取视频 URL
        video_url = ""
        if detail:
            video_info = detail.get("video", {})
            media = video_info.get("media", {})
            stream = media.get("stream", {})

            if isinstance(stream, dict):
                for quality_key in ("h264", "h265", "av1"):
                    streams = stream.get(quality_key, [])
                    if isinstance(streams, list) and streams:
                        video_url = streams[0].get("master_url", "")
                        if video_url:
                            break

        if not video_url:
            return XhsDownloadResult(
                success=False,
                source_id=note.note_id,
                title=note.title,
                content_text=content_text,
                error="无法获取视频下载地址",
            )

        video_path = note_dir / f"{note.note_id}.mp4"
        ok = await _download_file(video_url, video_path)

        if ok:
            return XhsDownloadResult(
                success=True,
                source_id=note.note_id,
                title=note.title,
                filepath=video_path,
                content_text=content_text,
            )
        else:
            return XhsDownloadResult(
                success=False,
                source_id=note.note_id,
                title=note.title,
                content_text=content_text,
                error="视频文件下载失败",
            )

    else:
        # 图文笔记：提取正文 + 下载图片
        image_paths: list[Path] = []
        image_list: list[dict[str, Any]] = []

        if detail:
            image_list = detail.get("image_list", [])

        for idx, img in enumerate(image_list):
            img_url = img.get("url_default", "") or img.get("url", "") or img.get("info_list", [{}])[-1].get("url", "")
            if not img_url:
                continue

            # 确保 URL 完整
            if not img_url.startswith("http"):
                img_url = urljoin(IMAGE_CDN_BASE, img_url)

            ext = ".jpg"
            if "png" in img_url:
                ext = ".png"
            elif "webp" in img_url:
                ext = ".webp"

            img_path = note_dir / f"{idx + 1}{ext}"
            ok = await _download_file(img_url, img_path)
            if ok:
                image_paths.append(img_path)

        # 即使没有图片也算成功（可能有纯文字内容）
        return XhsDownloadResult(
            success=True,
            source_id=note.note_id,
            title=note.title,
            image_paths=image_paths,
            content_text=content_text,
        )


# ── 主入口 ──────────────────────────────────────────────


async def download_note(note: NoteInfo, config: Config) -> XhsDownloadResult:
    """下载小红书笔记内容，三层降级策略。

    1. XHS-Downloader Python 库（如果安装）
    2. XHS-Downloader API Server（如果配置）
    3. 直接 HTTP 下载（兜底）

    Args:
        note: 笔记信息
        config: 全局配置

    Returns:
        下载结果
    """
    logger.info(f"开始下载笔记: [{note.title}] (类型: {note.note_type})")

    # 第一层：XHS-Downloader 库
    result = await _try_xhs_downloader_lib(note, config)
    if result is not None:
        logger.info(f"[第一层] XHS-Downloader 库下载{'成功' if result.success else '失败'}")
        return result

    # 第二层：XHS-Downloader API Server
    result = await _try_xhs_downloader_api(note, config)
    if result is not None:
        logger.info(f"[第二层] XHS-Downloader API 下载{'成功' if result.success else '失败'}")
        return result

    # 第三层：直接 HTTP 下载
    logger.info("[第三层] 使用直接 HTTP 下载（兜底）")
    result = await _try_direct_download(note, config)
    logger.info(f"下载{'成功' if result.success else '失败'}: {note.title}")
    return result
