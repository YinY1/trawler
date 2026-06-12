"""B站 API 模式视频监控 - 通过 bilibili_api 检查 UP 主新视频"""

from __future__ import annotations

import logging

import bilibili_api

from shared.config import Config
from shared.protocols import JsonSetStore, VideoInfo

logger = logging.getLogger(__name__)


# ── 已知视频存储 ─────────────────────────────────────────


class SubscriptionStore(JsonSetStore):
    """管理已知视频 ID 的持久化存储，用于去重判断。

    继承 JsonSetStore，使用 data/known_bili_videos.json 存储。
    """

    def __init__(self, data_dir: str = "data") -> None:
        super().__init__(data_dir, "known_bili_videos.json")

    def _load(self) -> set[str]:
        """从磁盘加载已知视频集合。

        兼容旧格式：旧文件用 ``{"bvids": [...]}`` 格式。
        """
        if not self._path.exists():
            return set()
        try:
            text = self._path.read_text(encoding="utf-8")
            import json

            data = json.loads(text)
            # 兼容旧格式 {"bvids": [...]}
            if isinstance(data, dict) and "bvids" in data:
                return set(data["bvids"])
            return super()._load()
        except Exception:
            return set()

    def mark_known_video(self, video: VideoInfo) -> None:
        """将视频标记为已知（便利方法）。"""
        self.mark_known(video.bvid)

    def mark_known_batch_videos(self, videos: list[VideoInfo]) -> None:
        """批量标记视频为已知（便利方法）。"""
        self.mark_known_batch([v.bvid for v in videos])


# ── 视频检查 ─────────────────────────────────────────────


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

        # 检查是否还有更多
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
        except (ValueError, IndexError):
            pass
    return 0


async def check_new_videos(
    uid: int,
    config: Config,
    store: SubscriptionStore,
) -> list[VideoInfo]:
    """检查 UP 主的新视频。

    调用 bilibili_api 获取 UP 主最近视频列表，与 store 中的已知视频对比，
    返回尚未记录的新视频列表（按发布时间降序）。

    Args:
        uid: UP 主 UID
        config: 全局配置
        store: 已知视频存储

    Returns:
        新视频列表（按发布时间从新到旧排序）
    """
    from platforms.bilibili.auth import get_credential

    credential = get_credential(config)
    max_count = config.bilibili.monitor.max_videos_per_check

    logger.info(f"检查 UP 主 {uid} 的新视频 (最多 {max_count} 条)")

    raw_videos: list[dict]
    try:
        raw_videos = await _fetch_user_videos(uid, credential, max_count)
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 视频列表异常: {e}")
        return []

    if not raw_videos:
        logger.info(f"UP 主 {uid} 没有视频或获取失败")
        return []

    new_videos: list[VideoInfo] = []
    for raw in raw_videos:
        bvid = raw.get("bvid", "")
        if not bvid or store.is_known(bvid):
            continue

        info = _parse_video_info(raw, uid)
        new_videos.append(info)

    # 按发布时间降序
    new_videos.sort(key=lambda v: v.pubdate, reverse=True)

    if new_videos:
        logger.info(f"UP 主 {uid} 发现 {len(new_videos)} 个新视频")
    else:
        logger.debug(f"UP 主 {uid} 无新视频")

    return new_videos
