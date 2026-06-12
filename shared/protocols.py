"""公共接口协议层 - 统一定义所有模块间的契约

所有跨模块调用的 dataclass 和 Protocol 集中定义在此，
确保 pipeline 编排层与各平台/核心模块解耦。

设计原则：
- dataclass 定义数据流经各模块的结构
- Protocol 定义模块间行为契约（鸭子类型）
- 各模块从 protocols 导入接口，而非互相 import
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# B站数据模型
# ═══════════════════════════════════════════════════════════


@dataclass
class VideoInfo:
    """B站视频元信息"""

    bvid: str
    title: str
    uid: int
    author: str
    pubdate: int  # Unix 时间戳
    duration: int  # 秒
    desc: str = ""
    pic: str = ""  # 封面 URL


@dataclass
class DynamicInfo:
    """B站动态信息"""

    dynamic_id: str
    title: str
    author: str
    uid: int
    pubdate: int  # Unix 时间戳
    link: str
    content: str = ""
    image_urls: list[str] = field(default_factory=list)
    linked_bvid: str = ""


@dataclass
class CommentHighlight:
    """评论亮点"""

    content: str
    user_name: str
    is_up_owner: bool
    like_count: int


# ═══════════════════════════════════════════════════════════
# 小红书数据模型
# ═══════════════════════════════════════════════════════════


@dataclass
class NoteInfo:
    """小红书笔记信息"""

    note_id: str
    title: str
    author: str
    user_id: str
    note_type: str  # "video" | "normal"
    pubdate: int
    desc: str = ""
    cover_url: str = ""
    liked_count: int = 0
    xsec_token: str = ""


@dataclass
class XhsCommentHighlight:
    """小红书评论亮点"""

    content: str
    user_name: str
    is_author: bool
    like_count: int


# ═══════════════════════════════════════════════════════════
# 微博数据模型
# ═══════════════════════════════════════════════════════════


@dataclass
class WeiboPost:
    """微博帖子元信息"""

    post_id: str
    text: str
    clean_text: str
    author: str
    user_id: str
    pubdate: int  # Unix 时间戳
    image_urls: list[str] = field(default_factory=list)
    reposts_count: int = 0
    comments_count: int = 0
    likes_count: int = 0
    is_original: bool = True
    reposted_post: Optional[WeiboPost] = None  # 转发时可嵌套


@dataclass
class WeiboCommentHighlight:
    """微博评论亮点"""

    content: str
    user_name: str
    is_author: bool
    like_count: int


@dataclass
class WeiboDownloadResult:
    """微博帖子下载结果"""

    success: bool
    source_id: str  # post_id
    title: str
    text: str = ""
    image_paths: list[Path] = field(default_factory=list)
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════
# 下载结果模型
# ═══════════════════════════════════════════════════════════


@dataclass
class DownloadResult:
    """B站视频下载结果"""

    success: bool
    source_id: str  # bvid
    title: str
    filepath: Optional[Path] = None
    error: Optional[str] = None
    access_limited: bool = False
    access_note: str = ""

    @property
    def bvid(self) -> str:
        """向后兼容：source_id 就是 bvid。"""
        return self.source_id


@dataclass
class XhsDownloadResult:
    """小红书笔记下载结果"""

    success: bool
    source_id: str  # note_id
    title: str
    filepath: Optional[Path] = None  # 视频文件路径（视频笔记）
    image_paths: list[Path] = field(default_factory=list)
    content_text: str = ""
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════
# 转写 & 解析结果模型
# ═══════════════════════════════════════════════════════════


@dataclass
class TranscriptResult:
    """语音转写结果"""

    success: bool
    source_id: str
    title: str
    transcript_path: Optional[Path] = None
    json_path: Optional[Path] = None
    text: str = ""
    language: str = "zh"
    duration_seconds: float = 0.0
    error: Optional[str] = None


@dataclass
class ParsedNote:
    """解析后的小红书笔记"""

    note_id: str
    is_video: bool
    text: str
    video_path: Optional[Path] = None
    image_paths: list[Path] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 摘要结果
# ═══════════════════════════════════════════════════════════


@dataclass
class SummaryResult:
    """摘要生成结果"""

    text: str
    source: str  # "codebuddy" | "openai" | "ollama" | "local" | "local-fallback"
    is_ai: bool


# ═══════════════════════════════════════════════════════════
# 行为协议（Protocol）
# ═══════════════════════════════════════════════════════════


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 提供商协议"""

    def generate(self, prompt: str) -> str: ...


# ═══════════════════════════════════════════════════════════
# 通用存储基类
# ═══════════════════════════════════════════════════════════


class JsonSetStore:
    """通用的 JSON 集合存储，用于内容去重。

    管理 ``data/<filename>`` 文件，维护一个 ``set[str]`` 集合。
    - ``mark_known`` 只修改内存，不写磁盘（高性能）
    - 由调用方在适当时机调用 ``save()`` 持久化（安全）
    """

    def __init__(self, data_dir: str | Path, filename: str) -> None:
        self._path = Path(data_dir) / filename
        self._data: set[str] = self._load()

    def _load(self) -> set[str]:
        """从 JSON 文件加载已知 ID 集合。"""
        if not self._path.exists():
            return set()
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                return set(data.get("known_ids", []))
            elif isinstance(data, list):
                return set(data)
            return set()
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("加载 %s 失败，将使用空集合: %s", self._path, e)
            return set()

    def save(self) -> None:
        """持久化已知 ID 集合到磁盘。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"known_ids": sorted(self._data)},
                ensure_ascii=False,
                indent=2,
            )
            self._path.write_text(payload, encoding="utf-8")
        except OSError as e:
            logger.error("保存 %s 失败: %s", self._path, e)

    def is_known(self, key: str) -> bool:
        """检查 key 是否已知。"""
        return key in self._data

    def mark_known(self, key: str) -> None:
        """将 key 标记为已知（仅内存）。"""
        self._data.add(key)

    def mark_known_batch(self, keys: list[str]) -> None:
        """批量标记 key 为已知（仅内存）。"""
        self._data.update(keys)

    @property
    def known_count(self) -> int:
        """已知条目数量。"""
        return len(self._data)
