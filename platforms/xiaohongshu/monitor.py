"""小红书笔记监控模块 - 用户笔记列表监控与新增检测"""

from __future__ import annotations

# pyright: basic
import logging
from typing import Any

from platforms.xiaohongshu.async_xhs_wrapper import AsyncXhsClient
from platforms.xiaohongshu.auth import get_xhs_cookie
from shared.config import Config
from shared.exceptions import DataError, is_session_expired_error
from shared.protocols import FetchedMessage, NoteInfo

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
    except DataError as e:
        # -100 / 登录已过期：服务端明确拒绝 session → 重新抛出供上层写回
        if is_session_expired_error(e):
            raise
        logger.warning(f"小红书笔记列表 API 请求异常: {e}")
        return []
    except Exception as e:
        logger.warning(f"小红书笔记列表 API 请求异常: {e}")
        return []
    finally:
        await client.close()


async def fetch_user_notes(
    user_id: str,
    name: str,
    config: Config,
    config_path: str = "config/config.toml",
) -> list[NoteInfo]:
    """获取指定用户的笔记列表。

    当 XHS 服务端返回 -100（登录已过期）时，同步将
    ``config.xiaohongshu.auth.expires_at`` 置为 ``0.0`` 并写回
    ``cookies.toml``，使 Web UI 状态与真实服务端状态一致。

    Args:
        user_id: 小红书用户 ID
        name: 用户名称（用于日志）
        config: 全局配置
        config_path: config.toml 路径，用于派生 cookies.toml 写入位置

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
    except DataError as e:
        # XHS -100 session 失效 → 写回 expires_at=0 以同步 Web UI 状态
        logger.warning("XHS 登录已失效 (%s)，请重新登录", e)
        await _mark_xhs_expired(config, config_path)
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


async def _mark_xhs_expired(config: Config, config_path: str) -> None:
    """将 XHS expires_at 置 0 并写回 cookies.toml。

    更新内存（config dataclass）与磁盘（cookies.toml）双侧状态，
    确保 Web UI 立刻显示"已失效"。写盘失败仅 warn，不阻塞主流程。
    """
    # 更新内存
    config.xiaohongshu.auth.expires_at = 0.0

    # 写回磁盘
    try:
        from shared.auth import update_auth_section

        await update_auth_section(
            "xhs",
            {"expires_at": 0.0},
            config_path=config_path,
        )
        logger.info("已将 XHS expires_at=0 写回 cookies.toml")
    except Exception as exc:
        logger.warning("写回 XHS expires_at=0 失败: %s", exc)


async def fetch_note_by_id(
    note_id: str,
    config: Config,
) -> FetchedMessage | None:
    """按 note_id 抓取单条小红书笔记元数据（issue #101）。

    调 ``AsyncXhsClient.get_note_by_id(note_id, xsec_token="", xsec_source="pc_feed")``。
    **xsec_token 缺失是主要失败原因**：``pc_feed`` 链路对外部仅给 note_id 的场景
    可能拿不到正文。

    失败信号（spec §1）：
    - ``DataError`` 异常 → 抛 ``PermanentFetchError``（server 拒绝，token 缺失等）
    - 拿到 ``note_card`` 但 ``desc`` / ``image_list`` / ``video`` 全空
      → 抛 ``PermanentFetchError``（"xhs: 笔记正文为空，可能 xsec_token 缺失"）

    Args:
        note_id: 笔记 ID（不带 "xhs:" 前缀）
        config: 全局配置（用于取 cookie）

    Returns:
        ``FetchedMessage``；``content_type`` 按 ``note_card.type == "video"`` 判断。

    Raises:
        PermanentFetchError: 永久失败（见上）。
    """
    from platforms.xiaohongshu.auth import get_xhs_cookie
    from shared.exceptions import DataError, PermanentFetchError
    from shared.protocols import ContentType, FetchedMessage

    cookie = get_xhs_cookie(config)
    if not cookie:
        raise PermanentFetchError("xhs: cookie 缺失")

    client = AsyncXhsClient(cookie=cookie)
    try:
        note_card = await client.get_note_by_id(
            note_id, xsec_token="", xsec_source="pc_feed",
        )
    except DataError as e:
        raise PermanentFetchError(f"xhs: server 拒绝（可能 xsec_token 缺失）: {e}") from e
    finally:
        # 关闭语义参考现有 monitor.py:130 的 await client.close() 用法
        # （AsyncXhsClient 无 __aenter__/__aexit__，仅 async def close，见
        # async_xhs_wrapper.py:345）
        try:
            await client.close()
        except Exception:
            pass

    if not isinstance(note_card, dict) or not note_card.get("note_id"):
        raise PermanentFetchError(f"xhs: note_card 为空或格式异常 (note_id={note_id})")

    # 正文为空检测（spec §1 xhs 失败信号）
    desc = note_card.get("desc", "") or ""
    image_list = note_card.get("image_list", [])
    video = note_card.get("video")
    has_video = isinstance(video, dict) and bool(video)
    has_images = isinstance(image_list, list) and len(image_list) > 0
    if not desc and not has_video and not has_images:
        raise PermanentFetchError("xhs: 笔记正文为空，可能 xsec_token 缺失")

    note_type = note_card.get("type", "normal")
    is_video = note_type == "video" or has_video
    title = note_card.get("display_title", "") or note_card.get("title", "") or ""
    user_info = note_card.get("user", {}) if isinstance(note_card.get("user"), dict) else {}
    author = user_info.get("nickname", "") or ""

    # pubdate 优先级与 _parse_note_data 一致
    pubdate = (
        note_card.get("last_update_time", 0)
        or note_card.get("time", 0)
        or note_card.get("create_time", 0)
        or note_card.get("timestamp", 0)
    )
    if not pubdate and len(note_id) >= 8:
        try:
            pubdate = int(note_id[:8], 16)
        except (ValueError, TypeError):
            pubdate = 0
    try:
        pubdate = int(pubdate) if pubdate else 0
    except (ValueError, TypeError):
        pubdate = 0

    return FetchedMessage(
        msg_id=f"xhs:{note_card.get('note_id', note_id)}",
        platform="xhs",
        content_type=ContentType.VIDEO if is_video else ContentType.TEXT,
        pubdate=pubdate,
        title=title,
        author=author,
        xsec_token=note_card.get("xsec_token", "") or note_card.get("xsec_token_str", "") or "",
        body=desc,
    )
