"""Tests for MessageStore — unified phase-aware message storage."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from shared.message_store import MessageStore
from shared.protocols import ContentType, Phase


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    """Create a MessageStore backed by a temp directory."""
    return MessageStore(tmp_path)


# ── add_new / is_known ──────────────────────────────────────────


def test_add_new_creates_record(store: MessageStore) -> None:
    msg = store.add_new(
        msg_id="bili:BV1xx",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=int(time.time()),
        title="Test Video",
        author="Test Author",
    )
    assert msg is not None
    assert msg.msg_id == "bili:BV1xx"
    assert msg.content_type == ContentType.VIDEO
    assert msg.phase == Phase.DISCOVERED
    assert store.is_known("bili:BV1xx")


def test_add_new_returns_none_for_duplicate(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    result = store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    assert result is None


def test_add_new_returns_none_outside_window(store: MessageStore) -> None:
    old_pubdate = int(time.time()) - 48 * 3600  # 48 hours ago
    result = store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, old_pubdate, "T", "A")
    assert result is None


# ── get_message ─────────────────────────────────────────────────


def test_get_message_returns_record(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    msg = store.get_message("bili:BV1xx")
    assert msg is not None
    assert msg.msg_id == "bili:BV1xx"
    assert msg.platform == "bili"


def test_get_message_returns_none_for_unknown(store: MessageStore) -> None:
    assert store.get_message("nonexistent") is None


# ── mark_phase / mark_error ────────────────────────────────────


def test_mark_phase_updates_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_phase("bili:BV1xx", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1xx")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED


def test_mark_error_records_error(store: MessageStore) -> None:
    store.add_new("bili:BV1xx", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1xx", "download failed")
    msg = store.get_message("bili:BV1xx")
    assert msg is not None
    assert msg.error == "download failed"


# ── get_messages ────────────────────────────────────────────────


def test_get_messages_all(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    all_msgs = store.get_messages()
    assert len(all_msgs) == 2


def test_get_messages_filter_by_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    discovered = store.get_messages(phase=Phase.DISCOVERED)
    assert len(discovered) == 1
    assert discovered[0].msg_id == "bili:BV2"


def test_get_messages_exclude_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    unfinished = store.get_messages(phase=Phase.PUSHED, exclude=True)
    assert len(unfinished) == 1
    assert unfinished[0].msg_id == "bili:BV2"


# ── cleanup ─────────────────────────────────────────────────────


def test_cleanup_removes_old_messages(store: MessageStore) -> None:
    old_pubdate = int(time.time()) - 48 * 3600
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, old_pubdate, "T1", "A")  # will be rejected
    # Manually insert an old message (bypass window check)
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": "video",
        "phase": "discovered",
        "pubdate": old_pubdate,
        "title": "Old",
        "author": "A",
        "created_at": 0.0,
        "updated_at": 0.0,
        "error": "",
    }
    store._dirty = True
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.cleanup(window_hours=24)
    assert not store.is_known("bili:BV1")
    assert store.is_known("bili:BV2")


# ── persistence (save + reload) ────────────────────────────────


def test_save_and_reload(tmp_path: Path) -> None:
    s1 = MessageStore(tmp_path)
    s1.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    s1.mark_phase("bili:BV1", Phase.DOWNLOADED)
    s1.save()

    s2 = MessageStore(tmp_path)
    assert s2.is_known("bili:BV1")
    msg = s2.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED


def test_save_creates_json_file(tmp_path: Path) -> None:
    store = MessageStore(tmp_path)
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.save()
    json_path = tmp_path / "messages.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    assert "bili:BV1" in data["messages"]


# ── reset_to_phase ──────────────────────────────────────────────


def test_reset_to_phase_downgrades_messages(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    store.reset_to_phase(Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED
    assert msg.error == ""  # error cleared on reset


# ── get_messages_in_window ───────────────────────────────────────


def test_get_messages_in_window(store: MessageStore) -> None:
    now = int(time.time())
    # Manually insert 3 messages with different ages (bypass add_new window check)
    store._messages["msg-1h"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": now - 3600,
        "title": "1h ago",
        "author": "A",
        "created_at": 0.0,
        "updated_at": 0.0,
        "error": "",
    }
    store._messages["msg-25h"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": now - 25 * 3600,
        "title": "25h ago",
        "author": "A",
        "created_at": 0.0,
        "updated_at": 0.0,
        "error": "",
    }
    store._messages["msg-48h"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": now - 48 * 3600,
        "title": "48h ago",
        "author": "A",
        "created_at": 0.0,
        "updated_at": 0.0,
        "error": "",
    }
    store._dirty = True

    # Default 24h window: only the 1h message
    window24 = store.get_messages_in_window()
    assert len(window24) == 1
    assert window24[0].msg_id == "msg-1h"

    # 48h window: 1h + 25h (48h is excluded)
    window48 = store.get_messages_in_window(window_hours=48)
    assert len(window48) == 2
    ids = {m.msg_id for m in window48}
    assert "msg-1h" in ids
    assert "msg-25h" in ids
    assert "msg-48h" not in ids


# ── body / summary (plan 2026-06-25) ─────────────────────────────


def test_record_has_body_and_summary_defaults() -> None:
    """MessageRecord 新字段 body/summary 必须默认空字符串。"""
    from shared.protocols import MessageRecord

    r = MessageRecord(
        msg_id="x",
        platform="bili",
        content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED,
        pubdate=0,
        title="t",
        author="a",
    )
    assert r.body == ""
    assert r.summary == ""


def test_msg_from_dict_loads_body_and_summary(store: MessageStore) -> None:
    """_msg_from_dict 必须把存储中的 body/summary 反序列化进 MessageRecord。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
        "body": "正文",
        "summary": "摘要",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.body == "正文"
    assert msg.summary == "摘要"


def test_msg_from_dict_defaults_body_summary_when_missing(store: MessageStore) -> None:
    """旧 messages.json 不含新字段时，反序列化默认空（向后兼容）。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.body == ""
    assert msg.summary == ""


def test_mark_body_sets_body(store: MessageStore) -> None:
    """mark_body 写入后 get_message 必须读回。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_body("bili:BV1", "新正文")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.body == "新正文"


def test_mark_summary_sets_summary(store: MessageStore) -> None:
    """mark_summary 写入后 get_message 必须读回。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_summary("bili:BV1", "AI 摘要")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.summary == "AI 摘要"


# ── subscription_ref round-trip ─────────────────────────────────


def test_record_has_retry_and_last_error_defaults() -> None:
    """MessageRecord 新字段 retry_count / last_error 默认值（向后兼容）。"""
    from shared.protocols import MessageRecord

    r = MessageRecord(
        msg_id="x",
        platform="bili",
        content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED,
        pubdate=0,
        title="t",
        author="a",
    )
    assert r.retry_count == 0
    assert r.last_error == ""


def test_subscription_ref_persists(tmp_path: Path) -> None:
    """subscription_ref 写入后 reload 不丢失（落盘回归测试）。"""
    from shared.message_store import MessageStore

    s1 = MessageStore(tmp_path)
    s1.add_new(
        msg_id="bili:BVref",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=int(time.time()),
        title="ref test",
        author="A",
        subscription_ref="42",
    )
    s1.save()

    s2 = MessageStore(tmp_path)
    msg = s2.get_message("bili:BVref")
    assert msg is not None
    assert msg.subscription_ref == "42", f"落盘丢失! got {msg.subscription_ref!r}"


# ── retry_count / last_error (plan 2026-06-28) ──────────────────


def test_msg_from_dict_loads_retry_and_last_error(store: MessageStore) -> None:
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
        "retry_count": 3,
        "last_error": "API timeout",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.retry_count == 3
    assert msg.last_error == "API timeout"


def test_msg_from_dict_defaults_retry_when_missing(store: MessageStore) -> None:
    """旧 messages.json 兼容：缺字段时取默认。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.retry_count == 0
    assert msg.last_error == ""


def test_mark_retry_failure_increments_count(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_retry_failure("bili:BV1", "first fail")
    store.mark_retry_failure("bili:BV1", "second fail")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.retry_count == 2
    assert msg.last_error == "second fail"
    assert msg.error == ""  # 关键：不写 error，cron 不跳过


def test_mark_retry_reset_clears_count(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_retry_failure("bili:BV1", "fail")
    store.mark_retry_reset("bili:BV1")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.retry_count == 0
    assert msg.last_error == ""


def test_reset_to_phase_clears_retry_state(store: MessageStore) -> None:
    """reset_to_phase 必须同步重置 retry_count / last_error（用户手动 reset 清状态）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_retry_failure("bili:BV1", "fail")
    store.mark_retry_failure("bili:BV1", "fail")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)

    store.reset_to_phase(Phase.DOWNLOADED)

    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.DOWNLOADED
    assert msg.retry_count == 0
    assert msg.last_error == ""


# ── query_messages (plan 2026-06-28-manual-content-check) ────────


def test_query_messages_by_platform(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "T2", "A")
    result = store.query_messages(platform="bili")
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_phase(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    result = store.query_messages(phase=Phase.SUMMARIZED)
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_title_substring_case_insensitive(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "Python Tutorial", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "Java Guide", "A")
    result = store.query_messages(title="python")
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_author_substring_case_insensitive(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "Alice")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "Bob")
    result = store.query_messages(author="ali")
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_by_since(store: MessageStore) -> None:
    now = int(time.time())
    # bypass add_new window check to inject old message
    store._messages["bili:old"] = {
        "platform": "bili", "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value, "pubdate": now - 48 * 3600,
        "title": "Old", "author": "A", "created_at": 0.0, "updated_at": 0.0, "error": "",
    }
    store._messages["bili:new"] = {
        "platform": "bili", "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value, "pubdate": now - 3600,
        "title": "New", "author": "A", "created_at": 0.0, "updated_at": 0.0, "error": "",
    }
    store._dirty = True
    # since = 24h ago → only "new"
    since_ts = now - 24 * 3600
    result = store.query_messages(since=since_ts)
    assert len(result) == 1
    assert result[0].msg_id == "bili:new"


def test_query_messages_combined_filters(store: MessageStore) -> None:
    """AND 组合：platform + title + phase 同时过滤。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "Python Guide", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "Python Tips", "A")
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "Python Notes", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    result = store.query_messages(platform="bili", title="python", phase=Phase.SUMMARIZED)
    assert len(result) == 1
    assert result[0].msg_id == "bili:BV1"


def test_query_messages_no_filters_returns_all(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("xhs:N1", "xhs", ContentType.TEXT, int(time.time()), "T2", "A")
    result = store.query_messages()
    assert len(result) == 2


def test_query_messages_empty_result(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    result = store.query_messages(title="nonexistent")
    assert result == []


# ── reset_specific (plan 2026-06-28-manual-content-check) ────────


def test_reset_specific_resets_target_ids(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.add_new("bili:BV2", "bili", ContentType.VIDEO, int(time.time()), "T2", "A")
    store.add_new("bili:BV3", "bili", ContentType.VIDEO, int(time.time()), "T3", "A")
    store.mark_phase("bili:BV1", Phase.PUSHED)
    store.mark_phase("bili:BV2", Phase.PUSHED)
    store.mark_phase("bili:BV3", Phase.PUSHED)

    count = store.reset_specific(["bili:BV1", "bili:BV3"], Phase.SUMMARIZED)
    assert count == 2
    assert store.get_message("bili:BV1").phase == Phase.SUMMARIZED
    assert store.get_message("bili:BV2").phase == Phase.PUSHED  # 未被 reset
    assert store.get_message("bili:BV3").phase == Phase.SUMMARIZED


def test_reset_specific_clears_error(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    store.mark_error("bili:BV1", "summary failed")
    store.reset_specific(["bili:BV1"], Phase.SUMMARIZED)
    assert store.get_message("bili:BV1").error == ""


def test_reset_specific_skips_lower_phase_messages(store: MessageStore) -> None:
    """目标阶段 >= current phase 的消息才 reset，低于的不动。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    store.mark_phase("bili:BV1", Phase.DISCOVERED)  # 比 SUMMARIZED 低
    count = store.reset_specific(["bili:BV1"], Phase.SUMMARIZED)
    assert count == 0
    assert store.get_message("bili:BV1").phase == Phase.DISCOVERED


def test_reset_specific_empty_list(store: MessageStore) -> None:
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    count = store.reset_specific([], Phase.SUMMARIZED)
    assert count == 0


def test_reset_specific_unknown_id(store: MessageStore) -> None:
    """未知 msg_id 静默跳过，不抛异常。"""
    count = store.reset_specific(["bili:nonexistent"], Phase.SUMMARIZED)
    assert count == 0


def test_reset_specific_persists_immediately(tmp_path: Path) -> None:
    """reset_specific 内部必须 save()，确保崩溃不丢数据（D5）。"""
    s1 = MessageStore(tmp_path)
    s1.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T1", "A")
    s1.mark_phase("bili:BV1", Phase.PUSHED)

    s1.reset_specific(["bili:BV1"], Phase.SUMMARIZED)
    # 不显式 save()，直接 reload
    s2 = MessageStore(tmp_path)
    msg = s2.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.SUMMARIZED


def test_load_tolerates_legacy_dynamic_content_type(tmp_path: Path) -> None:
    """issue #46 PR-1 删除了 ContentType.DYNAMIC。

    旧 messages.json 可能含 content_type=3(原 DYNAMIC 的 auto() 值)，
    _msg_from_dict 必须降级为 TEXT 而不是抛 ValueError 中断整个 store 加载。
    """
    # 手动构造一个含旧 DYNAMIC=3 的 messages.json
    legacy_data = {
        "messages": {
            "bili_dyn:legacy": {
                "platform": "bili",
                "content_type": 3,  # 旧 DYNAMIC 枚举值
                "phase": Phase.DISCOVERED.value,
                "pubdate": int(time.time()),
                "title": "legacy dynamic",
                "author": "UP",
            }
        }
    }
    (tmp_path / "messages.json").write_text(
        json.dumps(legacy_data), encoding="utf-8"
    )

    # 不抛异常
    store = MessageStore(tmp_path)
    msg = store.get_message("bili_dyn:legacy")
    assert msg is not None
    # 关键：降级为 TEXT 而不是 crash
    assert msg.content_type == ContentType.TEXT


def test_record_has_permanent_error_default() -> None:
    """MessageRecord 新字段 permanent_error 默认 False（向后兼容）。"""
    from shared.protocols import MessageRecord

    r = MessageRecord(
        msg_id="x",
        platform="bili",
        content_type=ContentType.VIDEO,
        phase=Phase.DISCOVERED,
        pubdate=0,
        title="t",
        author="a",
    )
    assert r.permanent_error is False


# ── permanent_error (plan 2026-06-30-webui-message-state-display) ──


def test_msg_from_dict_loads_permanent_error(store: MessageStore) -> None:
    """_msg_from_dict 必须把存储中的 permanent_error 反序列化进 MessageRecord。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
        "permanent_error": True,
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is True


def test_msg_from_dict_defaults_permanent_error_when_missing(store: MessageStore) -> None:
    """旧 messages.json 兼容：缺 permanent_error 字段时默认 False。"""
    store._messages["bili:BV1"] = {
        "platform": "bili",
        "content_type": ContentType.VIDEO.value,
        "phase": Phase.DISCOVERED.value,
        "pubdate": int(time.time()),
        "title": "T",
        "author": "A",
    }
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is False


def test_mark_error_default_not_permanent(store: MessageStore) -> None:
    """mark_error 不传 permanent 时默认 False（保持向后兼容）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "some error")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.error == "some error"
    assert msg.permanent_error is False  # 默认不标永久


def test_mark_error_with_permanent_true(store: MessageStore) -> None:
    """mark_error(permanent=True) 必须同时写 error 和 permanent_error=True。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "handler 标记永久失败", permanent=True)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.error == "handler 标记永久失败"
    assert msg.permanent_error is True


def test_mark_retry_reset_clears_permanent_error(store: MessageStore) -> None:
    """handler 成功后 mark_retry_reset 必须同步清零 permanent_error。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "fail", permanent=True)
    store.mark_retry_reset("bili:BV1")
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is False


def test_reset_to_phase_clears_permanent_error(store: MessageStore) -> None:
    """reset_to_phase 必须同步清零 permanent_error（与 retry_count/last_error 一致）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_error("bili:BV1", "fail", permanent=True)
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)

    store.reset_to_phase(Phase.DOWNLOADED)

    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.permanent_error is False


def test_reset_specific_clears_permanent_error(store: MessageStore) -> None:
    """reset_specific 必须同步清零 permanent_error（手动重跑清状态）。"""
    store.add_new("bili:BV1", "bili", ContentType.VIDEO, int(time.time()), "T", "A")
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    store.mark_error("bili:BV1", "summary failed", permanent=True)

    store.reset_specific(["bili:BV1"], Phase.SUMMARIZED)

    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.error == ""
    assert msg.permanent_error is False
