"""B站动态监控 - 通过 bilibili_api 检查 UP 主动态"""

from __future__ import annotations

# pyright: basic
import logging
from typing import TYPE_CHECKING

from shared.config import Config
from shared.protocols import DynamicInfo

if TYPE_CHECKING:
    from bilibili_api.utils.network import Credential

logger = logging.getLogger("trawler.bilibili.dynamic")

_DYNAMIC_TYPE_MAP = {
    "DYNAMIC_TYPE_AV": 8,
    "DYNAMIC_TYPE_WORD": 4,
    "DYNAMIC_TYPE_DRAW": 2,
    "DYNAMIC_TYPE_FORWARD": 1,
}


async def _fetch_user_dynamics(
    uid: int,
    credential: Credential,
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
    from bilibili_api import user

    try:
        # NOTE: 不用 dynamic.get_dynamic_page_info(), 它的 _type 与 host_mid
        # 参数在源码里是 if/elif 互斥 (L1254-1261)，传 _type 后 host_mid 被
        # 静默丢弃，会返回登录账号的全量关注流而非指定 UP 主动态。
        # user.User.get_dynamics_new() 走 /feed/space 接口，按 UID 拉取指定
        # UP 主的空间动态，不依赖关注关系。
        user_obj = user.User(uid, credential=credential)
        resp = await user_obj.get_dynamics_new()
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 动态失败: {e}")
        return []

    if not resp or "items" not in resp:
        return []

    items = resp.get("items", [])
    return items[:max_count]


def _parse_dynamic(item: dict, uid: int) -> DynamicInfo | None:
    """解析单条动态。

    支持多种动态类型: DYNAMIC_TYPE_AV (视频), DYNAMIC_TYPE_WORD (纯文字),
    DYNAMIC_TYPE_DRAW (图文), DYNAMIC_TYPE_FORWARD (转发) 等。
    """
    dynamic_id = item.get("id_str", "")
    if not dynamic_id:
        return None

    modules = item.get("modules", {})

    # ── 作者信息 ──
    author_module = modules.get("module_author", {})
    author = author_module.get("name", "")
    timestamp = int(author_module.get("pub_ts", 0))

    # ── 动态类型 ──
    dynamic_type_str = item.get("type", "")
    dynamic_type = _DYNAMIC_TYPE_MAP.get(dynamic_type_str, 0)

    # ── 动态内容 ──
    dynamic_module = modules.get("module_dynamic", {})
    desc_text = dynamic_module.get("desc")
    if desc_text is None:
        desc_text = ""
    major = dynamic_module.get("major") or {}

    title = ""
    content = ""
    image_urls: list[str] = []
    linked_bvid = ""

    # 类型 8: 视频
    if dynamic_type == 8:
        archive = major.get("archive", {})
        if archive:
            title = archive.get("title", "")
            bvid = archive.get("bvid", "")
            if bvid:
                linked_bvid = bvid
        content = desc_text

    # 类型 4: 纯文字
    elif dynamic_type == 4:
        content = desc_text

    # 类型 2: 图文
    elif dynamic_type == 2:
        draw = major.get("draw", {})
        if draw:
            title = draw.get("title", "")
            if not title:
                title = draw.get("desc", "")
            items_list = draw.get("items", [])
            image_urls = [img.get("src", "") for img in items_list if img.get("src")]
        content = desc_text

    # 类型 1: 转发
    elif dynamic_type == 1:
        content = desc_text
        orig = item.get("orig")
        if orig:
            orig_major = orig.get("modules", {}).get("module_dynamic", {}).get("major") or {}
            orig_archive = orig_major.get("archive") or {}
            if orig_archive.get("bvid"):
                linked_bvid = orig_archive["bvid"]

    else:
        content = desc_text

    link = f"https://t.bilibili.com/{dynamic_id}"

    # 如果标题为空，截取内容前 50 字符作为标题
    # NOTE: B站 API 返回的 desc 字段可能为 dict 而非 str，直接切片会抛
    # KeyError(slice(None, 50, None)) 导致 pipeline 提前失败。
    if not title and content:
        if isinstance(content, str):
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


async def fetch_new_dynamics(
    uid: int,
    config: Config,
    max_count: int | None = None,
) -> list[DynamicInfo]:
    """获取 UP 主最近动态（纯检测，不去重）。

    PipelineEngine detector 使用此函数获取动态原始列表，
    去重由 store.add_new() 内部处理。

    Args:
        uid: UP 主 UID
        config: 全局配置
        max_count: 最大获取数量，默认使用 config 中的配置

    Returns:
        动态列表（按发布时间从新到旧排序）
    """
    from platforms.bilibili.auth import get_credential

    credential = get_credential(config)
    if max_count is None:
        max_count = config.bilibili.monitor.max_videos_per_check

    logger.info(f"获取 UP 主 {uid} 的动态列表 (最多 {max_count} 条)")

    try:
        cards = await _fetch_user_dynamics(uid, credential, max_count)
    except Exception as e:
        logger.error(f"获取 UP 主 {uid} 动态异常: {e}")
        return []

    if not cards:
        logger.info(f"UP 主 {uid} 没有动态或获取失败")
        return []

    dynamics: list[DynamicInfo] = []
    for card in cards:
        dyn = _parse_dynamic(card, uid)
        if dyn is None:
            continue
        dynamics.append(dyn)

    dynamics.sort(key=lambda d: d.pubdate, reverse=True)

    if dynamics:
        logger.info(f"UP 主 {uid} 获取到 {len(dynamics)} 条动态")
    else:
        logger.debug(f"UP 主 {uid} 无动态")

    return dynamics
