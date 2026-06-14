"""微博内容解析模块 - 提取帖子正文、标签"""

from __future__ import annotations

# pyright: basic
import re
from typing import Any

from shared.protocols import WeiboDownloadResult, WeiboPost

# 话题标签正则：匹配 #话题# 格式
_TOPIC_PATTERN = re.compile(r"#([^#]+)#")


def _extract_topics(text: str) -> list[str]:
    """从文本中提取 #话题# 格式的话题标签。

    Args:
        text: 文本内容

    Returns:
        去重后的话题标签列表
    """
    topics = _TOPIC_PATTERN.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for topic in topics:
        topic = topic.strip()
        if topic and topic not in seen:
            seen.add(topic)
            result.append(topic)
    return result


def parse_weibo_post(post: WeiboPost, download_result: WeiboDownloadResult) -> dict[str, Any]:
    """解析微博帖子，提取正文、话题标签。

    当前微博帖子结构简单（文本+图片），不需要复杂的 ParsedNote。
    返回包含提取结果的字典，便于后续摘要生成和通知。

    Args:
        post: 微博帖子
        download_result: 下载结果

    Returns:
        解析结果字典，包含:
        - post_id: str
        - text: str (clean_text)
        - topics: list[str]
        - image_paths: list[Path]
    """
    text = download_result.text or post.clean_text
    topics = _extract_topics(text)

    return {
        "post_id": post.post_id,
        "text": text,
        "topics": topics,
        "image_paths": list(download_result.image_paths),
    }
