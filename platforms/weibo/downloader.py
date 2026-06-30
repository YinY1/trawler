"""微博媒体下载模块 - 下载微博图片"""

from __future__ import annotations

# pyright: basic
import logging
from pathlib import Path

import aiohttp

from shared.config import Config
from shared.constants import WEIBO_DOWNLOAD_TIMEOUT
from shared.protocols import WeiboDownloadResult, WeiboPost

logger = logging.getLogger("trawler.weibo.downloader")


def _get_post_dir(config: Config, post_id: str) -> Path:
    """获取帖子下载目录。

    Args:
        config: 全局配置
        post_id: 帖子 ID

    Returns:
        帖子专用下载目录路径
    """
    base = Path(config.download.dir) / "weibo" / post_id
    base.mkdir(parents=True, exist_ok=True)
    return base


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
            resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=WEIBO_DOWNLOAD_TIMEOUT))
            try:
                if resp.status != 200:
                    logger.debug("下载文件失败，状态码: %s, URL: %s", resp.status, url)
                    return False
                content = await resp.read()
            finally:
                resp.close()
            dest.write_bytes(content)
            return True
        except Exception as e:
            logger.debug("下载文件异常: %s, URL: %s", e, url)
            return False


async def download_weibo_media(post: WeiboPost, config: Config) -> WeiboDownloadResult:
    """下载微博帖子的媒体文件（图片）。

    Args:
        post: 微博帖子
        config: 全局配置

    Returns:
        下载结果
    """
    if not post.image_urls:
        return WeiboDownloadResult(
            success=True,
            source_id=post.post_id,
            title=post.clean_text[:50] if post.clean_text else post.post_id,
            text=post.clean_text,
        )

    post_dir = _get_post_dir(config, post.post_id)
    image_paths: list[Path] = []

    for idx, img_url in enumerate(post.image_urls):
        # 从 URL 猜测扩展名
        ext = ".jpg"
        lower_url = img_url.lower()
        if ".png" in lower_url:
            ext = ".png"
        elif ".webp" in lower_url:
            ext = ".webp"
        elif ".gif" in lower_url:
            ext = ".gif"

        img_path = post_dir / f"{idx + 1}{ext}"
        ok = await _download_file(img_url, img_path)
        if ok:
            image_paths.append(img_path)

    success = len(image_paths) > 0 or not post.image_urls
    return WeiboDownloadResult(
        success=success,
        source_id=post.post_id,
        title=post.clean_text[:50] if post.clean_text else post.post_id,
        text=post.clean_text,
        image_paths=image_paths,
        error=None if success else "图片下载全部失败",
    )


async def download_weibo_video(post: WeiboPost, config: Config) -> WeiboDownloadResult:
    """下载微博帖子的视频文件(mp4)。

    用于 VIDEO 类型 weibo 帖子(spec §3 / issue #46 PR-2)。
    下载到 ``{download_dir}/weibo/{post_id}/{post_id}.mp4``,填入返回值的 ``filepath`` 字段。

    Args:
        post: 微博帖子(需含 video_urls)
        config: 全局配置

    Returns:
        下载结果;``filepath`` 字段填入下载后的 mp4 路径
    """
    if not post.video_urls:
        return WeiboDownloadResult(
            success=False,
            source_id=post.post_id,
            title=post.clean_text[:50] if post.clean_text else post.post_id,
            text=post.clean_text,
            error="无视频 URL 可下载",
        )

    post_dir = _get_post_dir(config, post.post_id)
    video_path = post_dir / f"{post.post_id}.mp4"

    # 取第一个 URL(已在 api.py 按优先级排序:多分辨率 > stream_url_hd > stream_url)
    video_url = post.video_urls[0]
    ok = await _download_file(video_url, video_path)
    if not ok:
        return WeiboDownloadResult(
            success=False,
            source_id=post.post_id,
            title=post.clean_text[:50] if post.clean_text else post.post_id,
            text=post.clean_text,
            error="视频下载失败",
        )

    return WeiboDownloadResult(
        success=True,
        source_id=post.post_id,
        title=post.clean_text[:50] if post.clean_text else post.post_id,
        text=post.clean_text,
        filepath=video_path,
    )
