"""小红书笔记内容解析模块 - 提取正文、标签、媒体路径"""

from __future__ import annotations

# pyright: basic
import html
import re

from shared.protocols import NoteInfo, ParsedNote, XhsDownloadResult

# 标签提取正则：匹配 #标签# 格式
TAG_PATTERN = re.compile(r"#([^#\s]+)#")


def _clean_text(text: str) -> str:
    """清理文本内容。

    - 去除 HTML 实体
    - 去除多余空行
    - 去除首尾空白
    - 统一换行符

    Args:
        text: 原始文本

    Returns:
        清理后的文本
    """
    if not text:
        return ""

    # 解码 HTML 实体
    text = html.unescape(text)

    # 去除 HTML 标签（如有）
    text = re.sub(r"<[^>]+>", "", text)

    # 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 合并连续空行（最多保留一个空行）
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 去除每行首尾空白
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)

    # 去除首尾空白
    text = text.strip()

    return text


def _extract_tags(text: str) -> list[str]:
    """从文本中提取 #标签# 格式的标签。

    Args:
        text: 文本内容

    Returns:
        去重后的标签列表
    """
    tags = TAG_PATTERN.findall(text)
    # 去重并保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def parse_note_content(note: NoteInfo, download_result: XhsDownloadResult) -> ParsedNote:
    """解析笔记内容，提取正文、标签、媒体路径。

    图文笔记: 从 download_result.content_text 提取正文
    视频笔记: 返回 video_path 供后续转写

    Args:
        note: 笔记信息
        download_result: 下载结果

    Returns:
        解析后的 ParsedNote
    """
    is_video = note.note_type == "video"

    # 获取原始文本
    raw_text = download_result.content_text or note.desc or note.title

    # 清理文本
    clean_text = _clean_text(raw_text)

    # 提取标签（从原始文本和 desc 中提取）
    all_tag_sources = f"{raw_text} {note.desc}"
    tags = _extract_tags(all_tag_sources)

    # 构建解析结果
    parsed = ParsedNote(
        note_id=note.note_id,
        is_video=is_video,
        text=clean_text,
        tags=tags,
    )

    if is_video:
        parsed.video_path = download_result.filepath
    else:
        parsed.image_paths = list(download_result.image_paths)

    return parsed
