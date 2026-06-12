"""B站动态监控 - 通过 bilibili_api 检查 UP 主动态"""

from __future__ import annotations

import logging
import re
from typing import Optional

from platforms.bilibili.monitor import SubscriptionStore
from shared.config import Config
from shared.protocols import DynamicInfo

logger = logging.getLogger(__name__)


async def _fetch_user_dynamics(
    uid: int,
    credential: object,
    max_count: int = 10,
) -> list[dict]:
    """获取 UP 主最近动态的原始数据。

    Args:
        uid: UP 主 UID
        credential: bilibili_api Credential 对象
        max_count: 最大获取数量

    Returns:
        动态信息字典列表
    """
    from bilibili_api import dynamic

    try:
        resp = await dynamic.get_user_dynamics(uid=uid, credential=credential)
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 动态失败: {e}")
        return []

    if not resp or "cards" not in resp:
        return []

    cards = resp.get("cards", [])
    return cards[:max_count]


def _extract_bvid(text: str) -> str:
    """从文本中提取 BV 号。"""
    match = re.search(r"(BV[\w]+)", text)
    return match.group(1) if match else ""


def _parse_dynamic(card: dict, uid: int) -> Optional[DynamicInfo]:
    """解析单条动态。

    支持多种动态类型: DYNAMIC_TYPE_AV (视频), DYNAMIC_TYPE_WORD (纯文字),
    DYNAMIC_TYPE_DRAW (图文), DYNAMIC_TYPE_FORWARD (转发) 等。
    """
    desc = card.get("desc", {})
    dynamic_id = str(desc.get("dynamic_id", ""))
    if not dynamic_id:
        return None

    author = desc.get("user_profile", {}).get("info", {}).get("uname", "")
    timestamp = desc.get("timestamp", 0)
    dynamic_type = desc.get("type", 0)

    card_str = card.get("card", "{}")
    if isinstance(card_str, str):
        import json

        try:
            card_data = json.loads(card_str)
        except json.JSONDecodeError:
            card_data = {}
    else:
        card_data = card_str

    title = ""
    content = ""
    image_urls: list[str] = []
    linked_bvid = ""

    # 类型 8: 视频
    if dynamic_type == 8:
        title = card_data.get("title", "")
        content = card_data.get("desc", "")
        pic = card_data.get("pic", "")
        if pic:
            image_urls.append(pic)
        # 提取 BV 号
        bvid = desc.get("bvid", "") or card_data.get("bvid", "")
        if bvid:
            linked_bvid = bvid
        else:
            # 尝试从短链接中提取
            linked_bvid = _extract_bvid(card_data.get("short_link_v2", ""))

    # 类型 4: 纯文字
    elif dynamic_type == 4:
        content = card_data.get("item", {}).get("content", "")

    # 类型 2: 图文
    elif dynamic_type == 2:
        item = card_data.get("item", {})
        title = item.get("title", "")
        content = item.get("description", "")
        image_urls = [img.get("img_src", "") for img in item.get("pictures", []) if img.get("img_src")]

    # 类型 1: 转发
    elif dynamic_type == 1:
        content = card_data.get("item", {}).get("content", "")
        # 尝试从原始动态提取 BV 号
        origin_str = card_data.get("origin", "{}")
        if isinstance(origin_str, str):
            linked_bvid = _extract_bvid(origin_str)

    else:
        # 通用回退: 尝试提取标题和内容
        title = card_data.get("title", "")
        content = card_data.get("desc", "") or card_data.get("item", {}).get("content", "")

    link = f"https://t.bilibili.com/{dynamic_id}"

    # 如果标题为空，截取内容前 50 字符作为标题
    if not title and content:
        title = content[:50] + ("..." if len(content) > 50 else "")

    return DynamicInfo(
        dynamic_id=dynamic_id,
        title=title,
        author=author,
        uid=uid,
        pubdate=timestamp,
        link=link,
        content=content,
        image_urls=image_urls,
        linked_bvid=linked_bvid,
    )


async def check_new_dynamics(
    uid: int,
    config: Config,
    store: SubscriptionStore,
) -> list[DynamicInfo]:
    """检查 UP 主的新动态。

    调用 bilibili_api 获取 UP 主最近动态列表，与 store 中已知的动态 ID 对比，
    返回尚未记录的新动态列表。

    Args:
        uid: UP 主 UID
        config: 全局配置
        store: 已知视频/动态存储（用 bvid/dynamic_id 去重）

    Returns:
        新动态列表（按发布时间从新到旧排序）
    """
    from platforms.bilibili.auth import get_credential

    credential = get_credential(config)
    max_count = config.bilibili.monitor.max_videos_per_check

    logger.info(f"检查 UP 主 {uid} 的新动态 (最多 {max_count} 条)")

    try:
        cards = await _fetch_user_dynamics(uid, credential, max_count)
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 动态异常: {e}")
        return []

    if not cards:
        logger.info(f"UP 主 {uid} 没有动态或获取失败")
        return []

    new_dynamics: list[DynamicInfo] = []
    for card in cards:
        dyn = _parse_dynamic(card, uid)
        if dyn is None:
            continue
        # 用 dynamic_id 做去重 (复用 store 的 bvid 机制，以 "dyn_" 前缀区分)
        dedup_key = f"dyn_{dyn.dynamic_id}"
        if store.is_known(dedup_key):
            continue
        # 同时检查关联的 BV 号是否已知（仅去重，不阻止动态通知）
        # 注：linked_bvid 已知时跳过是为了避免重复处理视频，但动态通知仍会发送
        new_dynamics.append(dyn)

    # 按发布时间降序
    new_dynamics.sort(key=lambda d: d.pubdate, reverse=True)

    if new_dynamics:
        logger.info(f"UP 主 {uid} 发现 {len(new_dynamics)} 条新动态")
    else:
        logger.debug(f"UP 主 {uid} 无新动态")

    return new_dynamics
