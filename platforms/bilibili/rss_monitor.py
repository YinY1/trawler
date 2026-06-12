"""B站 RSS 模式视频监控 - 多实例并发请求 + 健康度评分"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import feedparser

from platforms.bilibili.monitor import SubscriptionStore
from shared.config import Config
from shared.constants import RSS_REQUEST_TIMEOUT
from shared.http import get_session
from shared.protocols import VideoInfo

logger = logging.getLogger(__name__)

# ── 异常 ──────────────────────────────────────────────────


class RSSAllFailedError(Exception):
    """所有 RSSHub 实例均请求失败时抛出。"""

    pass


# ── 健康度评分 ────────────────────────────────────────────


@dataclass
class InstanceHealth:
    """RSSHub 实例健康度数据。

    Attributes:
        success_count: 成功请求次数
        fail_count: 失败请求次数
        last_success: 最近一次成功的 Unix 时间戳
        avg_response_time: 平均响应时间（秒）
    """

    success_count: int = 0
    fail_count: int = 0
    last_success: float = 0.0
    avg_response_time: float = 0.0

    @property
    def score(self) -> float:
        """综合健康度评分 (0.0 ~ 1.0)。

        计算方式:
        - 成功率权重 70%: success_count / max(total, 1)
        - 近期活跃度权重 30%: 基于距上次成功的时间衰减
        """
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.0

        # 成功率
        success_rate = self.success_count / total

        # 近期活跃度: 指数衰减，半衰期 1 小时
        if self.last_success > 0:
            elapsed = time.time() - self.last_success
            activity = max(0.0, 1.0 - elapsed / 7200)  # 2 小时衰减到 0
        else:
            activity = 0.0

        return 0.7 * success_rate + 0.3 * activity


# ── 健康度持久化 ──────────────────────────────────────────

_HEALTH_DIR = Path("data")
_HEALTH_FILE = _HEALTH_DIR / "instance_health.json"


def _load_health() -> dict[str, InstanceHealth]:
    """从磁盘加载健康度数据。"""
    if not _HEALTH_FILE.exists():
        return {}
    try:
        text = _HEALTH_FILE.read_text(encoding="utf-8")
        raw = json.loads(text)
        return {k: InstanceHealth(**v) for k, v in raw.items()}
    except (json.JSONDecodeError, OSError, TypeError) as e:
        logger.warning(f"加载 instance_health.json 失败: {e}")
        return {}


def _save_health(data: dict[str, InstanceHealth]) -> None:
    """持久化健康度数据到磁盘。"""
    try:
        _HEALTH_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            k: {
                "success_count": v.success_count,
                "fail_count": v.fail_count,
                "last_success": v.last_success,
                "avg_response_time": v.avg_response_time,
            }
            for k, v in data.items()
        }
        _HEALTH_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error(f"保存 instance_health.json 失败: {e}")


# ── RSS Feed 解析 ────────────────────────────────────────


def _parse_rss_video(entry: dict) -> Optional[VideoInfo]:
    """从 RSS feed entry 解析视频信息。"""
    bvid = entry.get("bvid", "")
    if not bvid:
        # 尝试从链接中提取 BV 号
        link = entry.get("link", "")
        if "bilibili.com/video/" in link:
            import re

            match = re.search(r"(BV[\w]+)", link)
            if match:
                bvid = match.group(1)
    if not bvid:
        return None

    return VideoInfo(
        bvid=bvid,
        title=entry.get("title", ""),
        uid=int(entry.get("uid", 0)),
        author=entry.get("author", ""),
        pubdate=int(entry.get("pubdate", 0)),
        duration=int(entry.get("duration", 0)),
        desc=entry.get("description", ""),
        pic=entry.get("pic", ""),
    )


# ── RSS 监控器 ────────────────────────────────────────────


class RSSMonitor:
    """RSS 模式视频监控器。

    并发请求多个 RSSHub 实例，选择最新发布时间最大的 feed（避免缓存滞后），
    同时跟踪各实例的健康度。
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._instances = config.bilibili.monitor.rsshub_instances
        self._health: dict[str, InstanceHealth] = _load_health()

    def _update_health_success(self, instance: str, response_time: float) -> None:
        """更新实例健康度 - 成功。"""
        h = self._health.get(instance, InstanceHealth())
        # 增量平均
        total = h.success_count + 1
        h.avg_response_time = (h.avg_response_time * h.success_count + response_time) / total
        h.success_count = total
        h.last_success = time.time()
        self._health[instance] = h

    def _update_health_failure(self, instance: str) -> None:
        """更新实例健康度 - 失败。"""
        h = self._health.get(instance, InstanceHealth())
        h.fail_count += 1
        self._health[instance] = h

    def _persist_health(self) -> None:
        """持久化健康度数据。"""
        _save_health(self._health)

    async def _fetch_feed(
        self,
        session: aiohttp.ClientSession,
        instance: str,
        uid: int,
    ) -> Optional[feedparser.FeedParserDict]:
        """从单个 RSSHub 实例获取 feed。

        Args:
            session: aiohttp 会话
            instance: RSSHub 实例 URL
            uid: UP 主 UID

        Returns:
            解析后的 feed 对象，失败返回 None
        """
        url = f"{instance.rstrip('/')}/bilibili/user/video/{uid}"
        start = time.monotonic()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=RSS_REQUEST_TIMEOUT)) as resp:
                elapsed = time.monotonic() - start
                if resp.status != 200:
                    logger.debug(f"实例 {instance} 返回 HTTP {resp.status}")
                    self._update_health_failure(instance)
                    return None
                body = await resp.text()
                feed = feedparser.parse(body)
                if feed.bozo and not feed.entries:
                    logger.debug(f"实例 {instance} 返回无效 feed")
                    self._update_health_failure(instance)
                    return None
                self._update_health_success(instance, elapsed)
                return feed
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug(f"实例 {instance} 请求失败: {e}")
            self._update_health_failure(instance)
            return None

    async def check_up(
        self,
        uid: int,
        name: str,
        store: Optional[SubscriptionStore] = None,
    ) -> tuple[list[VideoInfo], bool]:
        """检查 UP 主的新视频。

        并发请求所有 RSSHub 实例，选择最新发布时间最大的 feed 作为权威来源。

        Args:
            uid: UP 主 UID
            name: UP 主名称（用于日志）
            store: 已知视频存储（可选，用于去重）

        Returns:
            (新视频列表, 是否成功获取至少一个实例)
        """
        logger.info(f"[RSS] 检查 UP 主 {name}({uid}) 的视频")

        session = await get_session()
        tasks = [self._fetch_feed(session, inst, uid) for inst in self._instances]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 过滤有效结果
        feeds: list[feedparser.FeedParserDict] = []
        for r in results:
            if isinstance(r, feedparser.FeedParserDict):
                feeds.append(r)

        if not feeds:
            self._persist_health()
            raise RSSAllFailedError(f"所有 RSSHub 实例均无法获取 UP 主 {name}({uid}) 的视频")

        # 选择最新发布时间最大的 feed
        def _max_pubdate(feed: feedparser.FeedParserDict) -> int:
            max_ts = 0
            for entry in feed.entries:
                ts = int(entry.get("pubdate", 0))
                if ts > max_ts:
                    max_ts = ts
            return max_ts

        best_feed = max(feeds, key=_max_pubdate)

        # 解析视频
        all_videos: list[VideoInfo] = []
        for entry in best_feed.entries:
            video = _parse_rss_video(entry)
            if video and (store is None or not store.is_known(video.bvid)):
                all_videos.append(video)

        # 按发布时间降序
        all_videos.sort(key=lambda v: v.pubdate, reverse=True)

        # 限制数量
        max_count = self._config.bilibili.monitor.max_videos_per_check
        all_videos = all_videos[:max_count]

        self._persist_health()

        logger.info(f"[RSS] UP 主 {name}({uid}): 发现 {len(all_videos)} 个新视频")
        return all_videos, True
