"""Tests for shared/downloader.py — _download_bili_video permanent flag.

覆盖 Issue #47 改动：下载失败按可恢复/不可恢复分类，``permanent=True``
标记的失败场景（凭证缺失/视频结构异常）不会因 retry 消失，``permanent=False``
的场景（网络抖动/HTTP 非 200）保留 retry 兜底。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.config import BilibiliAuth, BilibiliConfig, Config, DownloadConfig
from shared.downloader import _download_bili_video  # noqa: PLC2701

# ── fixtures ────────────────────────────────────────────────────


def _make_config(
    *,
    sessdata: str = "fake_sessdata",
    bili_jct: str = "fake_bili_jct",
    download_dir: str = ".",
) -> Config:
    """构造一个最小 Config，默认带登录凭证。"""
    cfg = Config()
    cfg.bilibili = BilibiliConfig()
    cfg.bilibili.auth = BilibiliAuth(sessdata=sessdata, bili_jct=bili_jct)
    cfg.download = DownloadConfig(dir=download_dir)
    return cfg


def _make_video_mock(
    *,
    info: dict[str, object] | Exception | None = None,
    download_url: dict[str, object] | Exception | None = None,
) -> MagicMock:
    """构造一个模拟 bilibili_api.video.Video 的对象。

    info/download_url 为 Exception 时对应方法抛异常；为 dict 时返回该 dict；
    为 None 时对应方法不被期望调用（默认返回空 dict）。
    """
    v = MagicMock()

    if isinstance(info, Exception):
        v.get_info = AsyncMock(side_effect=info)
    else:
        v.get_info = AsyncMock(return_value=info or {})

    if isinstance(download_url, Exception):
        v.get_download_url = AsyncMock(side_effect=download_url)
    else:
        v.get_download_url = AsyncMock(return_value=download_url or {})

    return v


@pytest.fixture
def download_dir(tmp_path: Path) -> Path:
    return tmp_path


# ── permanent=True 场景（不 retry） ─────────────────────────────


@pytest.mark.asyncio
async def test_no_credentials_marks_permanent(download_dir: Path) -> None:
    """未配置 sessdata/bili_jct → permanent=True（凭证缺失，retry 无意义）。"""
    cfg = _make_config(sessdata="", bili_jct="")
    # 凭证缺失路径在 import bilibili_api 之前 return，无需 mock bilibili_api
    result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is True
    assert "凭证" in (result.error or "")


@pytest.mark.asyncio
async def test_empty_pages_marks_permanent(download_dir: Path) -> None:
    """info['pages'] 为空 → permanent=True（视频数据结构异常）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(info={"pages": []})
    with patch("bilibili_api.video.Video", return_value=v):
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is True
    assert "页面信息" in (result.error or "")


@pytest.mark.asyncio
async def test_missing_cid_marks_permanent(download_dir: Path) -> None:
    """pages[0]['cid'] 缺失 → permanent=True（结构异常）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(info={"pages": [{}]})  # pages[0] 没有 cid
    with patch("bilibili_api.video.Video", return_value=v):
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is True
    assert "CID" in (result.error or "")


@pytest.mark.asyncio
async def test_no_audio_stream_marks_permanent(download_dir: Path) -> None:
    """dash.audio 为空 → permanent=True（纯图片/无音频流）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(
        info={"pages": [{"cid": 123}]},
        download_url={"dash": {"audio": []}},
    )
    with patch("bilibili_api.video.Video", return_value=v):
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is True
    assert "音频流" in (result.error or "")


@pytest.mark.asyncio
async def test_empty_audio_url_marks_permanent(download_dir: Path) -> None:
    """audios[0] 无 baseUrl/url → permanent=True（结构异常）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(
        info={"pages": [{"cid": 123}]},
        download_url={"dash": {"audio": [{"bandwidth": 1000}]}},  # 无 baseUrl/url
    )
    with patch("bilibili_api.video.Video", return_value=v):
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is True
    assert "URL 为空" in (result.error or "")


# ── permanent=False 场景（保留 retry） ──────────────────────────


@pytest.mark.asyncio
async def test_get_info_exception_not_permanent(download_dir: Path) -> None:
    """v.get_info() 抛异常 → permanent=False（网络/404 不可区分，让 retry 兜底）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(info=RuntimeError("network error / 404"))
    with patch("bilibili_api.video.Video", return_value=v):
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is False
    assert "获取视频信息失败" in (result.error or "")


@pytest.mark.asyncio
async def test_get_download_url_exception_not_permanent(download_dir: Path) -> None:
    """v.get_download_url() 抛异常 → permanent=False（临时接口失败）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(
        info={"pages": [{"cid": 123}]},
        download_url=RuntimeError("temporary api error"),
    )
    with patch("bilibili_api.video.Video", return_value=v):
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is False
    assert "获取下载地址失败" in (result.error or "")


@pytest.mark.asyncio
async def test_http_non_200_not_permanent(download_dir: Path) -> None:
    """aiohttp GET 返回非 200 → permanent=False（CDN 临时不可用，可 retry）。"""
    cfg = _make_config(download_dir=str(download_dir))

    v = _make_video_mock(
        info={"pages": [{"cid": 123}]},
        download_url={"dash": {"audio": [{"baseUrl": "https://cdn.example.com/a.m4s"}]}},
    )

    # 生产代码：``async with aiohttp.ClientSession() as session: async with session.get() as resp``
    # 需要两层 async context manager：session 本身 + session.get() 的返回值。
    mock_resp = MagicMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)  # 同步返回，由 __aenter__ 进入
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("bilibili_api.video.Video", return_value=v),
        patch("aiohttp.ClientSession") as mock_cls,
    ):
        mock_cls.return_value = mock_session
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await _download_bili_video("BV1xxx", cfg, download_dir, "title")

    assert result.success is False
    assert result.permanent is False
    assert "HTTP 503" in (result.error or "")
