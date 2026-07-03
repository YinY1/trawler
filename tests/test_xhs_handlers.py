"""Tests for xhs handlers — detector 持久化 xsec_token/desc (issue #89)。

验证 detector 阶段把 monitor 解析出的 NoteInfo.xsec_token 和 NoteInfo.desc
通过 store.add_new 透传到 MessageRecord（xsec_token 字段 + body 字段），
download handler 重建 NoteInfo 时也能从 MessageRecord 读回。
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import platforms.xiaohongshu.handlers as xhs_handlers
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NoteInfo, PhaseContext


def _make_config_with_xhs_sub() -> Config:
    """构造带 1 个 xhs 订阅的 Config（user_id="u1"）。"""
    config = Config()
    config.xiaohongshu.subscriptions = [
        MagicMock(user_id="u1", name="测试用户"),
    ]
    return config


class TestXhsDetectorPersistsTokenAndDesc:
    """detector 必须把 NoteInfo.xsec_token / NoteInfo.desc 持久化到 store。

    注：用 ``patch.object(xhs_handlers, "fetch_user_notes", ...)`` 而非字符串路径，
    避免 test_engine.py 的 autouse fixture 弹出 ``sys.modules`` 后 patch 落在新模块、
    但调用方持有旧模块引用导致 patch 失效（issue #89 测试稳定性）。
    """

    async def test_detector_persists_xsec_token_and_desc(self, tmp_path: Path) -> None:
        """fetch_user_notes 返回带 xsec_token+desc 的 NoteInfo → store 消息含两者。"""
        config = _make_config_with_xhs_sub()
        store = MessageStore(tmp_path)

        note = NoteInfo(
            note_id="n1",
            title="图文笔记",
            author="作者",
            user_id="u1",
            note_type="normal",
            pubdate=int(time.time()),
            desc="这是正文内容",
            xsec_token="tok_abc",
        )

        with patch.object(
            xhs_handlers, "fetch_user_notes", new=AsyncMock(return_value=[note])
        ):
            await xhs_handlers.xhs_detector(config, store)

        msg = store.get_message("xhs:n1")
        assert msg is not None
        # 关键：xsec_token 和 desc(写入 body) 都不能丢
        assert msg.xsec_token == "tok_abc", "xsec_token 丢失 — issue #89 根因"
        assert msg.body == "这是正文内容", "desc 未写入 body — issue #89 根因"

    async def test_detector_no_token_no_desc_still_works(self, tmp_path: Path) -> None:
        """NoteInfo 无 xsec_token/desc（默认 ""）→ 不破坏现有行为。"""
        config = _make_config_with_xhs_sub()
        store = MessageStore(tmp_path)

        note = NoteInfo(
            note_id="n2",
            title="无 token 笔记",
            author="作者",
            user_id="u1",
            note_type="normal",
            pubdate=int(time.time()),
        )

        with patch.object(
            xhs_handlers, "fetch_user_notes", new=AsyncMock(return_value=[note])
        ):
            await xhs_handlers.xhs_detector(config, store)

        msg = store.get_message("xhs:n2")
        assert msg is not None
        assert msg.xsec_token == ""
        assert msg.body == ""


class TestXhsDownloadReconstructsNoteInfo:
    """download handler 重建 NoteInfo 时必须透传 xsec_token/body→desc。"""

    async def test_download_reconstructs_note_with_token_and_desc(
        self, tmp_path: Path
    ) -> None:
        """MessageRecord 含 xsec_token+body → 重建 NoteInfo 含两者（传给 downloader）。"""
        config = Config()
        config.download.dir = str(tmp_path)
        store = MessageStore(tmp_path)
        store.add_new(
            "xhs:n3",
            "xhs",
            ContentType.TEXT,
            int(time.time()),
            "T",
            "A",
            xsec_token="tok_passed_down",
            body="正文传到下载",
        )
        msg = store.get_message("xhs:n3")
        assert msg is not None
        ctx = PhaseContext(msg=msg, config=config)

        captured_note: dict[str, object] = {}

        async def fake_download(note: NoteInfo, config: Config) -> object:
            captured_note["note"] = note
            result = MagicMock()
            result.success = True
            result.filepath = None
            result.image_paths = []
            result.content_text = "正文"
            result.error = None
            result.permanent = False
            return result

        with (
            patch.object(
                xhs_handlers, "download_note", new=AsyncMock(side_effect=fake_download)
            ),
            patch.object(xhs_handlers, "parse_note_content", return_value=None),
            patch(
                "platforms.xiaohongshu.comments.fetch_xhs_comment_highlights",
                new=AsyncMock(return_value=[]),
            ),
            patch("core.formatter.format_comment_highlights", return_value=""),
        ):
            ok = await xhs_handlers.xhs_download(ctx)

        assert ok is True
        note = captured_note["note"]
        assert isinstance(note, NoteInfo)
        # 关键：token 和 desc 必须从 MessageRecord 透传到 NoteInfo
        assert note.xsec_token == "tok_passed_down"
        assert note.desc == "正文传到下载"
