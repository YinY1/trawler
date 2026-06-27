"""小红书笔记监控模块 - 用户笔记列表监控与新增检测"""

from __future__ import annotations

# pyright: basic
import logging
from typing import Any

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
from platforms.xiaohongshu.auth import get_xhs_cookie
from shared.config import Config
from shared.protocols import NoteInfo

logger = logging.getLogger("trawler.xiaohongshu.monitor")


def _parse_note_from_api(note_data: dict[str, Any], author_name: str, user_id: str) -> NoteInfo | None:
    """从 API 响应中解析单条笔记信息。

    Args:
        note_data: API 返回的单条笔记数据
        author_name: 作者名称
        user_id: 作者 ID

    Returns:
        NoteInfo 或 None（解析失败时）
    """
    try:
        note_id = note_data.get("note_id", "") or note_data.get("id", "")
        if not note_id:
            return None

        note_type = note_data.get("type", "normal")
        # 判断是否为视频类型
        is_video = note_type == "video" or bool(note_data.get("video"))

        title = note_data.get("display_title", "") or note_data.get("title", "")
        desc = note_data.get("desc", "")

        # 封面图
        cover_url = ""
        cover_data = note_data.get("cover", {})
        if isinstance(cover_data, dict):
            cover_url = cover_data.get("url", "") or cover_data.get("url_default", "")
        elif isinstance(cover_data, str):
            cover_url = cover_data

        # 点赞数
        liked_count = 0
        interact_info = note_data.get("interact_info", {})
        if isinstance(interact_info, dict):
            liked_str = interact_info.get("liked_count", "0")
            try:
                liked_count = int(liked_str)
            except ValueError, TypeError:
                liked_count = 0

        # 发布时间：优先使用 API 返回字段，fallback 到 note_id 前 8 位 hex 编码的时间戳
        pubdate = (
            note_data.get("last_update_time", 0)
            or note_data.get("time", 0)
            or note_data.get("create_time", 0)
            or note_data.get("timestamp", 0)
        )
        if not pubdate:
            # XHS note_id 前 8 位 hex = Unix 时间戳（秒）
            note_id_str = note_data.get("note_id", "") or note_data.get("id", "")
            if len(note_id_str) >= 8:
                try:
                    pubdate = int(note_id_str[:8], 16)
                except ValueError, TypeError:
                    pubdate = 0
        if isinstance(pubdate, str):
            try:
                pubdate = int(pubdate)
            except ValueError, TypeError:
                pubdate = 0

        # xsec_token
        xsec_token = note_data.get("xsec_token", "") or note_data.get("xsec_token_str", "")

        return NoteInfo(
            note_id=str(note_id),
            title=title,
            author=author_name,
            user_id=user_id,
            note_type="video" if is_video else "normal",
            pubdate=pubdate,
            desc=desc,
            cover_url=cover_url,
            liked_count=liked_count,
            xsec_token=xsec_token,
        )
    except Exception as e:
        logger.debug(f"解析笔记数据失败: {e}")
        return None


async def _fetch_notes_via_api(
    user_id: str,
    cookie: str,
    cursor: str = "",
) -> list[dict[str, Any]]:
    """通过小红书 API 获取用户笔记列表 (via AsyncXhsClient)。

    Args:
        user_id: 小红书用户 ID
        cookie: Cookie 字符串
        cursor: 分页游标

    Returns:
        笔记数据列表
    """
    client = AsyncXhsClient(cookie=cookie)
    try:
        data = await client.get_user_notes(user_id, cursor=cursor)
        notes = data.get("notes", [])
        return notes if isinstance(notes, list) else []
    except Exception as e:
        logger.warning(f"小红书笔记列表 API 请求异常: {e}")
        return []
    finally:
        await client.close()


async def fetch_user_notes(
    user_id: str,
    name: str,
    config: Config,
) -> list[NoteInfo]:
    """获取指定用户的笔记列表。

    获取用户笔记列表，返回全部笔记。

    Args:
        user_id: 小红书用户 ID
        name: 用户名称（用于日志）
        config: 全局配置

    Returns:
        NoteInfo 列表（按发布时间降序）
    """
    cookie = get_xhs_cookie(config)
    if not cookie:
        logger.error(f"[{name}] 缺少 Cookie，无法检查笔记")
        return []

    # XhsClient 统一入口（签名 + 错误转译）
    raw_notes: list[dict[str, Any]] = []
    try:
        raw_notes = await _fetch_notes_via_api(user_id, cookie)
        logger.debug(f"[{name}] 签名 API 获取到 {len(raw_notes)} 条笔记")
    except Exception as e:
        logger.warning(f"[{name}] 签名 API 请求失败: {e}")

    if not raw_notes:
        logger.info(f"[{name}] 未获取到任何笔记数据")
        return []

    # 解析
    notes: list[NoteInfo] = []
    for raw in raw_notes:
        note = _parse_note_from_api(raw, name, user_id)
        if note is None:
            continue
        notes.append(note)

    # 按发布时间降序排列
    notes.sort(key=lambda n: n.pubdate, reverse=True)

    logger.info(f"[{name}] 获取到 {len(notes)} 条笔记")
    return notes
