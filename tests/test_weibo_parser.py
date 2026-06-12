"""Tests for platforms/weibo/parser.py — Weibo content parsing."""

from __future__ import annotations

from pathlib import Path

from platforms.weibo.parser import _extract_topics, parse_weibo_post
from shared.protocols import WeiboDownloadResult, WeiboPost

# ── _extract_topics ────────────────────────────────────────


class TestExtractTopics:
    def test_extracts_single_topic(self):
        text = "今天天气真好 #生活记录# 开心"
        assert _extract_topics(text) == ["生活记录"]

    def test_extracts_multiple_topics(self):
        text = "#科技# #AI# 新突破"
        assert _extract_topics(text) == ["科技", "AI"]

    def test_deduplicates_topics(self):
        text = "#科技# 话题 #科技# "
        assert _extract_topics(text) == ["科技"]

    def test_returns_empty_when_no_topics(self):
        text = "普通微博内容"
        assert _extract_topics(text) == []

    def test_handles_empty_text(self):
        assert _extract_topics("") == []

    def test_trims_whitespace(self):
        text = "#  空间话题  #"
        result = _extract_topics(text)
        assert result == ["空间话题"]


# ── parse_weibo_post ───────────────────────────────────────


def _make_post(text: str = "测试内容 #话题#") -> WeiboPost:
    return WeiboPost(
        post_id="post123",
        text=text,
        clean_text=text,
        author="用户A",
        user_id="12345",
        pubdate=1000,
    )


def _make_dl_result(image_paths: list[Path] | None = None) -> WeiboDownloadResult:
    return WeiboDownloadResult(
        success=True,
        source_id="post123",
        title="测试",
        text="",
        image_paths=image_paths or [],
    )


class TestParseWeiboPost:
    def test_returns_post_id_and_text(self):
        post = _make_post()
        dl = _make_dl_result()
        result = parse_weibo_post(post, dl)

        assert result["post_id"] == "post123"
        assert result["text"] == "测试内容 #话题#"

    def test_extracts_topics(self):
        post = _make_post("今天 #科技# 发展 #AI#")
        dl = _make_dl_result()
        result = parse_weibo_post(post, dl)

        assert result["topics"] == ["科技", "AI"]

    def test_prefers_download_result_text(self):
        post = _make_post("原始内容")
        dl = _make_dl_result()
        dl.text = "下载结果文本"
        result = parse_weibo_post(post, dl)

        assert result["text"] == "下载结果文本"

    def test_includes_image_paths(self):
        post = _make_post()
        dl = _make_dl_result(image_paths=[Path("/tmp/img1.jpg"), Path("/tmp/img2.jpg")])
        result = parse_weibo_post(post, dl)

        assert len(result["image_paths"]) == 2
        assert result["image_paths"][0] == Path("/tmp/img1.jpg")

    def test_returns_empty_topics_when_none(self):
        post = _make_post(text="无话题")
        dl = _make_dl_result()
        result = parse_weibo_post(post, dl)

        assert result["topics"] == []
