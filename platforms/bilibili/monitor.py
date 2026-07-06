"""B站 API 模式视频监控 - 纯检测函数，不再负责去重"""

from __future__ import annotations

# pyright: basic
import logging

import bilibili_api

from shared.config import Config
from shared.protocols import FetchedMessage, VideoInfo

logger = logging.getLogger("trawler.bilibili.monitor")


async def _fetch_user_videos(
    uid: int,
    credential: bilibili_api.Credential,
    max_count: int = 10,
) -> list[dict]:
    """调用 bilibili_api 获取 UP 主最近视频的原始数据。

    Args:
        uid: UP 主 UID
        credential: B 站凭证
        max_count: 最大获取数量

    Returns:
        视频信息字典列表
    """
    from bilibili_api import user

    u = user.User(uid=uid, credential=credential)
    results: list[dict] = []
    page = 1

    while len(results) < max_count:
        try:
            resp = await u.get_videos(pn=page, ps=min(30, max_count - len(results)))
        except Exception as e:
            logger.error(f"获取 UP 主 {uid} 视频列表失败 (page={page}): {e}")
            break

        vlist = resp.get("list", {}).get("vlist", [])
        if not vlist:
            break

        results.extend(vlist)

        total = resp.get("page", {}).get("count", 0)
        if page * 30 >= total:
            break
        page += 1

    return results[:max_count]


def _parse_video_info(raw: dict, uid: int) -> VideoInfo:
    """将 API 返回的原始字典解析为 VideoInfo。"""
    return VideoInfo(
        bvid=raw.get("bvid", ""),
        title=raw.get("title", ""),
        uid=uid,
        author=raw.get("author", ""),
        pubdate=raw.get("created", 0),
        duration=_parse_duration(raw.get("length", 0)),
        desc=raw.get("description", ""),
        pic=raw.get("pic", ""),
    )


def _parse_duration(raw_duration) -> int:
    """将 API 返回的 duration 解析为整数秒。

    API 可能返回整数秒或 "MM:SS" / "HH:MM:SS" 格式字符串。
    """
    if isinstance(raw_duration, int):
        return raw_duration
    if isinstance(raw_duration, str):
        parts = raw_duration.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError, IndexError:
            pass
    return 0


async def fetch_user_videos(
    uid: int,
    config: Config,
    max_count: int = 10,
) -> list[VideoInfo]:
    """获取 UP 主最近视频列表（纯检测，不去重）。

    Args:
        uid: UP 主 UID
        config: 全局配置
        max_count: 最大获取数量

    Returns:
        视频信息列表（按发布时间从新到旧排序）
    """
    from platforms.bilibili.auth import get_credential

    credential = get_credential(config)

    logger.info(f"获取 UP 主 {uid} 的视频列表 (最多 {max_count} 条)")

    raw_videos: list[dict]
    try:
        raw_videos = await _fetch_user_videos(uid, credential, max_count)
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 视频列表异常: {e}")
        return []

    if not raw_videos:
        logger.info(f"UP 主 {uid} 没有视频或获取失败")
        return []

    videos: list[VideoInfo] = []
    for raw in raw_videos:
        bvid = raw.get("bvid", "")
        if not bvid:
            continue
        info = _parse_video_info(raw, uid)
        videos.append(info)

    videos.sort(key=lambda v: v.pubdate, reverse=True)
    logger.info(f"UP 主 {uid} 获取到 {len(videos)} 个视频")
    return videos


async def fetch_video_by_id(
    bvid: str,
    config: Config,
) -> FetchedMessage | None:
    """按 BVID 抓取单条 B 站视频元数据（issue #101）。

    基于 ``bilibili_api.video.Video(bvid).get_info()``，不依赖订阅列表。

    Args:
        bvid: 视频 BV 号（不带 "bili:" 前缀）
        config: 全局配置（用于取 credential）

    Returns:
        ``FetchedMessage``（``content_type=VIDEO``，B 站一律走完整 5 阶段）；
        抓取失败/视频不存在 → None（调用方可重试）。

    Raises:
        无 —— 所有异常内部捕获并 log，对外只返回 None。
        （B 站目前无明确的"永久失败"信号，token/credential 缺失由
        ``get_credential`` 在 ``auth.py`` 层抛 ``AuthError``，不是这里职责。）
    """
    from platforms.bilibili.auth import get_credential
    from shared.protocols import ContentType, FetchedMessage

    credential = get_credential(config)
    try:
        v = bilibili_api.video.Video(bvid=bvid, credential=credential)
        info = await v.get_info()
    except Exception as e:
        logger.error("按 BVID 抓取失败 (%s): %s", bvid, e)
        return None

    if not isinstance(info, dict) or not info.get("bvid"):
        logger.warning("BVID %s 返回数据为空或格式异常", bvid)
        return None

    owner = info.get("owner", {}) if isinstance(info.get("owner"), dict) else {}
    return FetchedMessage(
        msg_id=f"bili:{info['bvid']}",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=int(info.get("pubdate", 0) or 0),
        title=info.get("title", "") or "",
        author=owner.get("name", "") or "",
        body=info.get("desc", "") or "",
    )
