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
