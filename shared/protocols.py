"""公共接口协议层 - 统一定义所有模块间的契约

所有跨模块调用的 dataclass 和 Protocol 集中定义在此，
确保 pipeline 编排层与各平台/核心模块解耦。

设计原则：
- dataclass 定义数据流经各模块的结构
- Protocol 定义模块间行为契约（鸭子类型）
- 各模块从 protocols 导入接口，而非互相 import
"""

from __future__ import annotations

# pyright: basic
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from shared.config import Config

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
    """评论亮点 — 跨平台统一模型"""

    content: str
    user_name: str
    is_author: bool  # 统一字段名（原 Bili: is_up_owner, Weibo/XHS: is_author）
    like_count: int
    # 对话链路（作者回复别人时展示完整上下文）
    reply_to: str = ""  # 被回复的用户名
    parent_content: str = ""  # 被回复的原文
    is_pinned: bool = False  # 是否被置顶


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
    is_long_text: bool = False
    long_text: str = ""  # 长文全文（isLongText=True 时填充）
    reposted_post: WeiboPost | None = None  # 转发时可嵌套


@dataclass
class WeiboDownloadResult:
    """微博帖子下载结果"""

    success: bool
    source_id: str  # post_id
    title: str
    text: str = ""
    image_paths: list[Path] = field(default_factory=list)
    error: str | None = None
    permanent: bool = False  # True = 永久失败（post 不存在/用户注销等），不 retry


# ═══════════════════════════════════════════════════════════
# 下载结果模型
# ═══════════════════════════════════════════════════════════


@dataclass
class DownloadResult:
    """B站视频下载结果"""

    success: bool
    source_id: str  # bvid
    title: str
    filepath: Path | None = None
    error: str | None = None
    access_limited: bool = False
    access_note: str = ""
    permanent: bool = False  # True = 永久失败（凭证缺失/BVID 不存在等），不 retry


@dataclass
class XhsDownloadResult:
    """小红书笔记下载结果"""

    success: bool
    source_id: str  # note_id
    title: str
    filepath: Path | None = None  # 视频文件路径（视频笔记）
    image_paths: list[Path] = field(default_factory=list)
    content_text: str = ""
    error: str | None = None
    permanent: bool = False  # True = 永久失败（笔记被删/用户不存在等），不 retry


@dataclass
class RenewalResult:
    """Result of a token check-and-renew operation."""

    platform: str
    action: str  # "skipped" | "renewed" | "expired" | "not_configured"
    message: str


# ═══════════════════════════════════════════════════════════
# 转写 & 解析结果模型
# ═══════════════════════════════════════════════════════════


@dataclass
class TranscriptResult:
    """语音转写结果"""

    success: bool
    source_id: str
    title: str
    transcript_path: Path | None = None
    json_path: Path | None = None
    text: str = ""
    language: str = "zh"
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass
class ParsedNote:
    """解析后的小红书笔记"""

    note_id: str
    is_video: bool
    text: str
    video_path: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 摘要结果
# ═══════════════════════════════════════════════════════════


@dataclass
class SummaryResult:
    """摘要生成结果"""

    text: str
    source: str  # "openai" | "ollama" | "local" | "local-fallback"
    is_ai: bool


# ═══════════════════════════════════════════════════════════
# 行为协议（Protocol）
# ═══════════════════════════════════════════════════════════


@runtime_checkable
class LLMProvider(Protocol):
    """LLM 提供商协议"""

    async def generate(self, prompt: str) -> str: ...


# ═══════════════════════════════════════════════════════════
# 消息状态管理 — ContentType, Phase, MessageRecord
# ═══════════════════════════════════════════════════════════


class ContentType(Enum):
    """内容类型"""

    VIDEO = auto()  # B站视频 / XHS视频笔记 — 完整五阶段
    TEXT = auto()  # 微博 / XHS图文笔记 — 三阶段（下载+推送）
    DYNAMIC = auto()  # B站动态 — 三阶段（摘要+推送，无下载/转写）


class Phase(Enum):
    """消息处理阶段"""

    DISCOVERED = auto()
    DOWNLOADED = auto()
    TRANSCRIBED = auto()
    SUMMARIZED = auto()
    PUSHED = auto()


# 各类型消息的阶段流转路径
PHASE_FLOW: dict[ContentType, list[Phase]] = {
    ContentType.VIDEO: [
        Phase.DISCOVERED,
        Phase.DOWNLOADED,
        Phase.TRANSCRIBED,
        Phase.SUMMARIZED,
        Phase.PUSHED,
    ],
    ContentType.TEXT: [
        Phase.DISCOVERED,
        Phase.DOWNLOADED,
        Phase.PUSHED,
    ],
    ContentType.DYNAMIC: [
        Phase.DISCOVERED,
        Phase.SUMMARIZED,
        Phase.PUSHED,
    ],
}


@dataclass
class MessageRecord:
    """单条消息在流水线中的完整状态"""

    msg_id: str  # "{platform}:{id}" e.g. "bili:BV1xx", "xhs:note_id", "weibo:post_id"
    platform: str  # "bili" | "xhs" | "weibo"
    content_type: ContentType
    phase: Phase
    pubdate: int  # Unix 时间戳（内容发布时间）
    title: str
    author: str
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    # 动态附加文字（去重场景）：当动态 linked_bvid 指向的视频已被注册时，
    # 动态本身的文字内容（UP 主的补充说明）会被追加到对应视频消息的这个字段，
    # 在摘要生成时拼到摘要输入文本前面，标注【动态内容】。
    dynamic_text: str = ""
    # detector 注入，push handler 用于精确匹配订阅（存 uid/user_id 字符串）
    subscription_ref: str = ""
    # 内容正文（清洗后纯文本）：xhs/weibo 由 download handler 经 engine flush 回写；
    # bili 视频无文本正文，保持空字符串（见 plan D2）
    body: str = ""
    # AI 摘要：summarize 阶段或 download 内联摘要（weibo 路径）经 engine flush 回写
    summary: str = ""
    # 摘要失败重试计数（engine 层使用，handler 不直接读写）
    retry_count: int = 0
    # 最近一次可重试失败的错误信息（与 error 字段区分：error 表示永久失败，cron 跳过）
    last_error: str = ""


@dataclass
class PhaseContext:
    """流水线上下文，各阶段产出逐级积累"""

    msg: MessageRecord
    config: Config
    downloaded_filepath: Path | None = None
    image_paths: list[Path] = field(default_factory=list)
    content_text: str = ""
    transcript_text: str = ""
    summary_text: str = ""
    keywords: list[str] = field(default_factory=list)
    comment_highlights: str = ""
    error: str = ""
    # handler 标记本次失败为「永久失败」：engine 跳过 retry 直接 mark_error（cron 永久跳过）。
    # 用于 fail-fast 场景：transcribe 文件路径缺失、download access_limited 等重试无意义的失败。
    # 默认 False（保持现有 retry 行为）。handler 在 return False 前置 True 即可。
    permanent_error: bool = False
    # 手动重跑模式标志（plan 2026-06-28-manual-content-check）：
    # True 时 push handler 跳过 send_to_subscription，但 phase 仍推进到 PUSHED
    # （dashboard 状态正确，只是不真发通知，避免重复打扰订阅者）。
    # 由 engine.run_specific_messages 在 ctx 创建时透传。
    skip_push: bool = False


# ═══════════════════════════════════════════════════════════
# 通知抽象层 — Notifier Protocol + 内容模型
# ═══════════════════════════════════════════════════════════


@dataclass
class NotificationContent:
    """跨平台统一的通知内容载体。

    渲染层根据 platform 字段选择 emoji 前缀和模板；
    Notifier 实现根据 type 字段决定是否省略某些字段。
    """

    platform: str  # "bili" | "xhs" | "weibo"
    source_id: str  # bvid / note_id / post_id / dynamic_id（不含 platform 前缀）
    title: str
    author: str
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    comment_highlights: str = ""
    url: str = ""  # 完整链接，空则由渲染层根据 platform + source_id 生成
    type: str = "content"  # "content" | "dynamic"


@dataclass
class SendResult:
    """单次发送结果（fan-out 中单个 endpoint 的反馈）。"""

    endpoint_name: str
    success: bool
    error: str = ""


class Notifier(Protocol):
    """通知发送器抽象。Provider 实现此接口。

    实现例：GotifyNotifier / TelegramNotifier（stub）/ EmailNotifier（stub）。
    """

    name: str

    async def send(self, content: NotificationContent) -> SendResult:
        """渲染并推送一条通知。返回 SendResult（不抛异常，失败时填 error）。"""
        ...
