"""Tests for platforms.bilibili.dynamic._parse_dynamic — has_video 字段。

Covers spec §2: DynamicInfo 暴露 has_video 让 detector 区分视频型/纯文字动态。
"""

from __future__ import annotations

from platforms.bilibili.dynamic import _parse_dynamic


def _make_item(dynamic_type_str: str, dynamic_id: str = "123") -> dict:
    """构造一条动态 API 原始 dict。"""
    return {
        "id_str": dynamic_id,
        "type": dynamic_type_str,
        "modules": {
            "module_author": {"name": "tester", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": "desc text",
                "major": {
                    "archive": {
                        "bvid": "BV1xx9999",
                        "title": "video title",
                    }
                },
            },
        },
    }


def test_parse_dynamic_type_av_has_video_true() -> None:
    """DYNAMIC_TYPE_AV (type 8) 是视频投屏动态,has_video 必为 True。"""
    item = _make_item("DYNAMIC_TYPE_AV")
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.has_video is True
    assert dyn.linked_bvid == "BV1xx9999"


def test_parse_dynamic_type_word_has_video_false() -> None:
    """DYNAMIC_TYPE_WORD (type 4) 是纯文字动态,has_video 必为 False。

    需要清空 major.archive(纯文字动态 API 不返回 archive 字段)。
    """
    item = _make_item("DYNAMIC_TYPE_WORD")
    item["modules"]["module_dynamic"]["major"] = {}  # 纯文字无 major.archive
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.has_video is False
    assert dyn.linked_bvid == ""


def test_parse_dynamic_type_draw_has_video_false() -> None:
    """DYNAMIC_TYPE_DRAW (type 2) 是图文动态,has_video 必为 False。"""
    item = _make_item("DYNAMIC_TYPE_DRAW")
    item["modules"]["module_dynamic"]["major"] = {
        "draw": {
            "title": "draw title",
            "items": [{"src": "https://example.com/1.jpg"}],
        }
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.has_video is False
    assert dyn.linked_bvid == ""


def test_parse_dynamic_handles_dict_desc() -> None:
    """B 站 FORWARD 类型动态 desc 是 rich-text dict,不应 crash (#75)。"""
    item = {
        "id_str": "123456",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"name": "tester", "pub_ts": 1717200000},
            "module_dynamic": {
                "desc": {
                    "rich_text_nodes": [{"text": "转发视频内容"}],
                    "text": "转发视频内容",
                },
                "major": {},
            },
        },
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert isinstance(dyn.content, str)
    assert "转发视频内容" in dyn.content


def test_parse_dynamic_handles_dict_desc_without_text_field() -> None:
    """desc dict 缺少 text 字段时,JSON dump 保留信息 (#75)。"""
    item = {
        "id_str": "789",
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"name": "tester", "pub_ts": 1717200000},
            "module_dynamic": {
                "desc": {"rich_text_nodes": [{"text": "fallback"}]},
                "major": {},
            },
        },
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert isinstance(dyn.content, str)
    assert "fallback" in dyn.content


# ═══════════════════════════════════════════════════════════════════
# DRAW 动态的两种渲染协议: MAJOR_TYPE_DRAW (旧) / MAJOR_TYPE_OPUS (新)
# ═══════════════════════════════════════════════════════════════════


def test_parse_draw_opus_protocol() -> None:
    """新协议 MAJOR_TYPE_OPUS: 正文在 opus.summary.text, 图片在 opus.pics。

    B站新版图文动态 desc=null、major.draw=null, 全部内容搬到 major.opus。
    """
    item = {
        "id_str": "1220452611194355721",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "BOSS墨", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": None,  # opus 协议 desc 为空
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {
                        "title": "行情暂时选择向上",
                        "summary": {"text": "那以再次突破之前4096小分水为标准..."},
                        "pics": [
                            {"url": "https://example.com/1.jpg"},
                            {"url": "https://example.com/2.jpg"},
                        ],
                    },
                },
            },
        },
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.title == "行情暂时选择向上"
    assert dyn.content == "那以再次突破之前4096小分水为标准..."
    assert dyn.image_urls == ["https://example.com/1.jpg", "https://example.com/2.jpg"]
    assert dyn.has_video is False


def test_parse_draw_legacy_protocol() -> None:
    """旧协议 MAJOR_TYPE_DRAW: title 在 draw.title, 图片在 draw.items[].src。"""
    item = {
        "id_str": "999000",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "tester", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": "",
                "major": {
                    "type": "MAJOR_TYPE_DRAW",
                    "draw": {
                        "title": "draw title",
                        "items": [
                            {"src": "https://example.com/legacy1.jpg"},
                            {"src": "https://example.com/legacy2.jpg"},
                        ],
                    },
                },
            },
        },
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.title == "draw title"
    assert dyn.image_urls == [
        "https://example.com/legacy1.jpg",
        "https://example.com/legacy2.jpg",
    ]
    assert dyn.has_video is False


def test_parse_draw_opus_fallback_to_desc() -> None:
    """opus 和 draw 都缺失但 desc 有内容时, content 兜底取 desc_text。"""
    item = {
        "id_str": "111222",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"name": "tester", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": "正文兜底内容",
                "major": {},  # 既无 opus 也无 draw
            },
        },
    }
    dyn = _parse_dynamic(item, uid=1)
    assert dyn is not None
    assert dyn.content == "正文兜底内容"
    # 无 title 时, 截取 content 前 50 字符作为 title
    assert dyn.title.startswith("正文兜底内容")
    assert dyn.image_urls == []
