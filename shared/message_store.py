"""统一消息状态存储 — 阶段感知的 JSON 持久化，取代各平台 JsonSetStore

管理 ``data/messages.json``，单文件存储所有平台的消息及阶段状态。
支持时间窗口过滤（默认 24h）和自动清理超期消息。

设计原则：
- 每推进一个阶段立即 save()，避免中途崩溃丢失进度
- JSON 全量重写在 24h 窗口内数据量很小（百条级），IO 成本可忽略
"""

from __future__ import annotations

# pyright: basic
import json
import logging
import time
from pathlib import Path

from shared.protocols import ContentType, MessageRecord, Phase

logger = logging.getLogger(__name__)

# 默认时间窗口（小时）
DEFAULT_WINDOW_HOURS = 24


class MessageStore:
    """统一消息状态存储。

    取代各平台的 ``SubscriptionStore`` / ``XhsSubscriptionStore`` / ``WeiboSubscriptionStore``。
    使用 ``data/messages.json`` 存储所有消息，格式见 spec。
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / "messages.json"
        self._messages: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ── 内部 ─────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载消息数据。"""
        if not self._path.exists():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                raw = data.get("messages", {})
                if isinstance(raw, dict):
                    self._messages = raw
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("加载 %s 失败，使用空存储: %s", self._path, exc)

    def save(self) -> None:
        """持久化消息数据到磁盘（原子写入，先写临时文件再 rename）。"""
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 2,
                "messages": self._messages,
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self._dirty = False
        except OSError as exc:
            logger.error("保存 %s 失败: %s", self._path, exc)

    def _msg_from_dict(self, msg_id: str, data: dict) -> MessageRecord:
        """将存储的 dict 转换为 MessageRecord（处理枚举反序列化）。"""
        return MessageRecord(
            msg_id=msg_id,
            platform=data["platform"],
            content_type=ContentType(data["content_type"]),
            phase=Phase(data["phase"]),
            pubdate=data["pubdate"],
            title=data["title"],
            author=data["author"],
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            error=data.get("error", ""),
            dynamic_text=data.get("dynamic_text", ""),
        )

    # ── 时间窗口 ─────────────────────────────────────────────

    @staticmethod
    def is_in_window(pubdate: int, window_hours: int = DEFAULT_WINDOW_HOURS) -> bool:
        """检查发布时间是否在时间窗口内。"""
        return (time.time() - pubdate) < window_hours * 3600

    # ── 查询 ─────────────────────────────────────────────────

    def is_known(self, msg_id: str) -> bool:
        """检查消息是否已记录。"""
        return msg_id in self._messages

    def get_message(self, msg_id: str) -> MessageRecord | None:
        """获取单条消息。"""
        data = self._messages.get(msg_id)
        if data is None:
            return None
        return self._msg_from_dict(msg_id, data)

    def get_messages(
        self,
        *,
        phase: Phase | None = None,
        exclude: bool = False,
        platform: str | None = None,
    ) -> list[MessageRecord]:
        """获取消息列表，支持按阶段和平台过滤。

        Args:
            phase: 按阶段过滤
            exclude: 为 True 时排除指定阶段的消息（即获取未达到该阶段的消息）
            platform: 按平台过滤（可选）
        """
        results: list[MessageRecord] = []
        for msg_id, data in self._messages.items():
            if platform is not None and data.get("platform") != platform:
                continue
            msg_phase = data.get("phase", "")
            if phase is not None:
                if exclude and msg_phase == phase.value:
                    continue
                if not exclude and msg_phase != phase.value:
                    continue
            results.append(self._msg_from_dict(msg_id, data))
        return results

    def get_messages_in_window(
        self,
        window_hours: int = DEFAULT_WINDOW_HOURS,
        *,
        phase: Phase | None = None,
        exclude: bool = False,
        platform: str | None = None,
    ) -> list[MessageRecord]:
        """获取时间窗口内的消息（默认 24h），支持按阶段和平台过滤。

        与 ``get_messages`` 的区别：只返回 ``pubdate`` 在窗口内的消息，
        不会删除超期数据（只读，安全用于 dashboard 等只读视图）。
        实际清理由 ``cleanup()`` 在 pipeline 中按需调用。
        """
        cutoff = time.time() - window_hours * 3600
        results: list[MessageRecord] = []
        for msg_id, data in self._messages.items():
            if data.get("pubdate", 0) < cutoff:
                continue
            if platform is not None and data.get("platform") != platform:
                continue
            msg_phase = data.get("phase", "")
            if phase is not None:
                if exclude and msg_phase == phase.value:
                    continue
                if not exclude and msg_phase != phase.value:
                    continue
            results.append(self._msg_from_dict(msg_id, data))
        return results

    # ── 写入 ─────────────────────────────────────────────────

    def add_new(
        self,
        msg_id: str,
        platform: str,
        content_type: ContentType,
        pubdate: int,
        title: str,
        author: str,
    ) -> MessageRecord | None:
        """添加新消息。

        内部做去重和时间窗口检查。如果消息已在 store 中或超出时间窗口，返回 None。

        Returns:
            新创建的 MessageRecord，或 None（已存在 / 超期）
        """
        if self.is_known(msg_id):
            return None
        if not self.is_in_window(pubdate):
            return None

        now = time.time()
        data: dict = {
            "platform": platform,
            "content_type": content_type.value,
            "phase": Phase.DISCOVERED.value,
            "pubdate": pubdate,
            "title": title,
            "author": author,
            "created_at": now,
            "updated_at": now,
            "error": "",
        }
        self._messages[msg_id] = data
        self._dirty = True
        return self._msg_from_dict(msg_id, data)

    def mark_phase(self, msg_id: str, phase: Phase) -> None:
        """更新消息的阶段。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["phase"] = phase.value
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def mark_error(self, msg_id: str, error: str) -> None:
        """记录消息的错误信息。"""
        if msg_id not in self._messages:
            return
        self._messages[msg_id]["error"] = error
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def append_dynamic_text(self, msg_id: str, text: str) -> None:
        """向已存在消息追加 dynamic_text（动态去重场景专用）。

        当动态的 ``linked_bvid`` 指向的视频已被注册时，调用此方法将动态本身
        的文字内容追加到视频消息上，避免重复推送两条内容相同的消息。

        多次追加会用换行分隔。如果 text 为空，直接 no-op。
        """
        if msg_id not in self._messages or not text:
            return
        existing = self._messages[msg_id].get("dynamic_text", "")
        if existing:
            self._messages[msg_id]["dynamic_text"] = f"{existing}\n{text}"
        else:
            self._messages[msg_id]["dynamic_text"] = text
        self._messages[msg_id]["updated_at"] = time.time()
        self._dirty = True

    def reset_to_phase(self, target: Phase, platform: str | None = None) -> None:
        """将所有阶段 >= target 的消息回退到 target 阶段，清除 error。

        Args:
            target: 目标阶段
            platform: 可选，仅回退指定平台的消息
        """
        for msg_id, data in list(self._messages.items()):
            if platform is not None and data.get("platform") != platform:
                continue
            current_phase = data.get("phase", Phase.DISCOVERED.value)
            if current_phase >= target.value:
                data["phase"] = target.value
                data["error"] = ""
                data["updated_at"] = time.time()
                self._dirty = True

    # ── 清理 ─────────────────────────────────────────────────

    def cleanup(self, window_hours: int = DEFAULT_WINDOW_HOURS) -> None:
        """删除超出时间窗口的消息。"""
        cutoff = time.time() - window_hours * 3600
        to_remove = [msg_id for msg_id, data in self._messages.items() if data.get("pubdate", 0) < cutoff]
        for msg_id in to_remove:
            del self._messages[msg_id]
        if to_remove:
            self._dirty = True
            logger.info("MessageStore cleanup: removed %d old messages", len(to_remove))
