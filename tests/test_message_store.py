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
    assert data["version"] == 2
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
        "platform": "bili", "content_type": ContentType.VIDEO.value, "phase": Phase.DISCOVERED.value,
        "pubdate": now - 3600, "title": "1h ago", "author": "A",
        "created_at": 0.0, "updated_at": 0.0, "error": "",
    }
    store._messages["msg-25h"] = {
        "platform": "bili", "content_type": ContentType.VIDEO.value, "phase": Phase.DISCOVERED.value,
        "pubdate": now - 25 * 3600, "title": "25h ago", "author": "A",
        "created_at": 0.0, "updated_at": 0.0, "error": "",
    }
    store._messages["msg-48h"] = {
        "platform": "bili", "content_type": ContentType.VIDEO.value, "phase": Phase.DISCOVERED.value,
        "pubdate": now - 48 * 3600, "title": "48h ago", "author": "A",
        "created_at": 0.0, "updated_at": 0.0, "error": "",
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
