from __future__ import annotations

import pytest

from shared.exceptions import PermanentFetchError, TrawlerError


def test_permanent_fetch_error_is_trawler_error():
    """PermanentFetchError 必须是 TrawlerError 子类（catch 契约）。"""
    with pytest.raises(TrawlerError):
        raise PermanentFetchError("xhs: xsec_token 缺失")


def test_permanent_fetch_error_message_preserved():
    """异常 message 不被改写（CI/日志分析依赖原文）。"""
    err = PermanentFetchError("xhs: 笔记正文为空")
    assert str(err) == "xhs: 笔记正文为空"
