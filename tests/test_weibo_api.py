"""Tests for platforms/weibo/api.py — Weibo HTTP API wrapper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from platforms.weibo.api import (
    _parse_mobile_post,
    _parse_pc_post,
    fetch_user_posts_mobile,
    fetch_user_posts_pc,
    search_user_by_name,
)

# ── Cookie helper ─────────────────────────────────────────


def _make_cookie_str() -> str:
    return "SUB=fake_sub; SUBP=fake_subp; WBPSESS=fake_wbpsess; SSOLoginState=123"


# ── fetch_user_posts_mobile ───────────────────────────────


class TestFetchUserPostsMobile:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "cards": [
                        {
                            "card_type": 9,
                            "mblog": {
                                "id": "post123",
                                "text": "测试微博内容",
                                "user": {"screen_name": "测试用户", "id": 12345},
                                "created_at": "Tue Jun 11 10:00:00 +0800 2026",
                                "pics": [{"url": "https://wx1.sinaimg.cn/large/001.jpg"}],
                                "reposts_count": 10,
                                "comments_count": 5,
                                "attitudes_count": 100,
                                "is_original": 0,
                            },
                        }
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert len(posts) == 1
        assert posts[0]["id"] == "post123"

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"ok": 0}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert posts == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_mobile(cookie, user_id="12345")

        assert posts == []


# ── fetch_user_posts_pc ───────────────────────────────────


class TestFetchUserPostsPc:
    @pytest.mark.asyncio
    async def test_returns_posts_list(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {
                "ok": 1,
                "data": {
                    "list": [
                        {
                            "id": 456789,
                            "idstr": "456789",
                            "text": "PC端微博内容",
                            "user": {"screen_name": "PC用户", "id": 67890},
                            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
                            "pic_ids": ["001", "002"],
                            "reposts_count": "20",
                            "comments_count": "15",
                            "attitudes_count": "200",
                            "is_original": 0,
                        }
                    ]
                },
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_pc(cookie, user_id="67890")

        assert len(posts) == 1
        assert posts[0]["id"] == 456789

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_error(self):
        cookie = _make_cookie_str()
        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side() -> dict:
            return {"ok": 0}

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            posts = await fetch_user_posts_pc(cookie, user_id="67890")

        assert posts == []


# ── _parse_mobile_post ────────────────────────────────────


class TestParseMobilePost:
    def test_parses_basic_post(self):
        raw = {
            "id": "post123",
            "text": "测试微博 <a href='/n/test'>@test</a> 内容",
            "user": {"screen_name": "测试用户", "id": 12345},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pics": [{"url": "https://wx1.sinaimg.cn/large/001.jpg"}],
            "reposts_count": 10,
            "comments_count": 5,
            "attitudes_count": 100,
            "is_original": 1,
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.post_id == "post123"
        assert "测试微博" in result.text
        assert "测试微博" in result.clean_text  # HTML tags stripped
        assert result.author == "测试用户"
        assert result.user_id == "12345"
        assert result.pubdate > 0
        assert len(result.image_urls) == 1
        assert result.reposts_count == 10
        assert result.comments_count == 5
        assert result.likes_count == 100
        assert result.is_original is True

    def test_handles_reposted_post(self):
        raw = {
            "id": "post456",
            "text": "转发微博",
            "user": {"screen_name": "转发者", "id": 999},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "reposts_count": 0,
            "comments_count": 0,
            "attitudes_count": 0,
            "is_original": 0,
            "retweeted_status": {
                "id": "original123",
                "text": "原始微博内容",
                "user": {"screen_name": "原作者", "id": 111},
                "created_at": "Mon Jun 10 08:00:00 +0800 2026",
                "reposts_count": 50,
                "comments_count": 20,
                "attitudes_count": 300,
                "is_original": 1,
            },
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.is_original is False
        assert result.reposted_post is not None
        assert result.reposted_post.post_id == "original123"
        assert result.reposted_post.author == "原作者"

    def test_returns_none_on_missing_id(self):
        raw = {"text": "no id post"}
        result = _parse_mobile_post(raw)
        assert result is None

    def test_parses_video_urls_from_page_info(self):
        """移动端 mblog 含 page_info.type=video 时,提取 mp4 URL 到 video_urls(spec §3)。"""
        raw = {
            "id": "videopost1",
            "text": "视频微博内容",
            "user": {"screen_name": "视频博主", "id": 88888},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "page_info": {
                "type": "video",
                "urls": {
                    "mp4_720p": "https://example.com/720p.mp4",
                    "mp4_360p": "https://example.com/360p.mp4",
                },
                "media_info": {
                    "stream_url": "https://example.com/low.mp4",
                    "stream_url_hd": "https://example.com/hd.mp4",
                },
            },
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert len(result.video_urls) > 0
        # 优先取 page_info.urls 中的 mp4 直链
        assert all(url.endswith(".mp4") for url in result.video_urls)
        assert "https://example.com/720p.mp4" in result.video_urls
        assert "https://example.com/360p.mp4" in result.video_urls

    def test_parses_video_fallback_to_stream_url(self):
        """page_info.urls 为空时,降级到 media_info.stream_url(spec §3)。"""
        raw = {
            "id": "videopost2",
            "text": "视频微博",
            "user": {"screen_name": "博主", "id": 77777},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "page_info": {
                "type": "video",
                "media_info": {
                    "stream_url": "https://example.com/fallback.mp4",
                },
            },
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.video_urls == ["https://example.com/fallback.mp4"]

    def test_ignores_non_video_page_info(self):
        """page_info.type != 'video' 时不提取视频字段(避免误抓图文/直播卡片)。"""
        raw = {
            "id": "picpost1",
            "text": "图文微博",
            "user": {"screen_name": "博主", "id": 66666},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pics": [{"url": "https://example.com/pic.jpg"}],
            "page_info": {
                "type": "pic",
                "media_info": {"stream_url": "https://example.com/should_be_ignored.mp4"},
            },
        }
        result = _parse_mobile_post(raw)
        assert result is not None
        assert result.video_urls == []
        assert len(result.image_urls) == 1


# ── _parse_pc_post ────────────────────────────────────────


class TestParsePcPost:
    def test_parses_basic_post(self):
        raw = {
            "id": 456789,
            "idstr": "456789",
            "text": "PC端微博 <a href='/n/test'>@test</a>",
            "user": {"screen_name": "PC用户", "id": 67890},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "pic_ids": ["001", "002"],
            "pic_infos": {
                "001": {"original": {"url": "https://wx1.sinaimg.cn/large/001.jpg"}},
                "002": {"original": {"url": "https://wx1.sinaimg.cn/large/002.jpg"}},
            },
            "reposts_count": "20",
            "comments_count": "15",
            "attitudes_count": "200",
            "is_original": 1,
        }
        result = _parse_pc_post(raw)
        assert result is not None
        assert result.post_id == "456789"
        assert "PC端微博" in result.text
        assert result.author == "PC用户"
        assert result.user_id == "67890"
        assert len(result.image_urls) == 2
        assert result.reposts_count == 20
        assert result.comments_count == 15
        assert result.likes_count == 200
        assert result.is_original is True

    def test_returns_none_on_missing_id(self):
        raw = {"text": "no id"}
        result = _parse_pc_post(raw)
        assert result is None

    def test_parses_video_urls_from_page_info(self):
        """PC 端 post 含 page_info.type=video 时提取 mp4 URL(spec §3)。"""
        raw = {
            "id": 100001,
            "idstr": "100001",
            "text": "PC视频微博",
            "user": {"screen_name": "PC视频博主", "id": 55555},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "page_info": {
                "type": "video",
                "urls": {
                    "mp4_720p": "https://example.com/pc_720p.mp4",
                    "mp4_360p": "https://example.com/pc_360p.mp4",
                },
                "media_info": {
                    "stream_url": "https://example.com/pc_low.mp4",
                },
            },
        }
        result = _parse_pc_post(raw)
        assert result is not None
        assert len(result.video_urls) > 0
        assert "https://example.com/pc_720p.mp4" in result.video_urls

    def test_parses_video_fallback_to_stream_url(self):
        raw = {
            "id": 100002,
            "idstr": "100002",
            "text": "PC视频",
            "user": {"screen_name": "博主", "id": 44444},
            "created_at": "Tue Jun 11 10:00:00 +0800 2026",
            "page_info": {
                "type": "video",
                "media_info": {"stream_url": "https://example.com/pc_fb.mp4"},
            },
        }
        result = _parse_pc_post(raw)
        assert result is not None
        assert result.video_urls == ["https://example.com/pc_fb.mp4"]


# ── search_user_by_name ─────────────────────────────────────────


# ── search_user_by_name ─────────────────────────────────────

# 真实抓取样本（已脱敏，2026-06-28 抓自服务器 s.weibo.com/user?q=人民日报）。
# 页面里同时含 s.weibo.com 自身标记和 $CONFIG（通过风控校验）。
# 注意样本里还含一条 html.unescape 测试项（Tom &amp; Jerry）。
_SAMPLE_SEARCH_HTML = """
<!DOCTYPE html>
<html><head>
    <title>微博搜索</title>
    <script>var $CONFIG = {}; $CONFIG['islogin'] = '1';</script>
</head>
<body>
  <!-- s.weibo.com 模板标记 -->
  <a href="//weibo.com/u/2803301701" class="name" target="_blank">人民日报</a>
  <p class="info">...简介...</p>
  <a href="//weibo.com/u/1411163204" class="name" target="_blank">人民日报健康客户端</a>
  <a href="//weibo.com/u/5703735355" class="name" target="_blank">人民日报体育</a>
  <a href="//weibo.com/u/9999999999" class="name">Tom &amp; Jerry</a>
  <a href="//weibo.com/u/2803301701" class="name">人民日报</a>  <!-- 重复出现，应去重 -->
</body></html>
"""

# 「正常但 0 结果」样本：页面通过风控校验（含 s.weibo.com + $CONFIG），
# 但没有任何 class="name" 的用户卡。
_SAMPLE_EMPTY_RESULT_HTML = (
    "<!DOCTYPE html><html><head><title>搜索-微博</title></head>"
    "<body><!-- s.weibo.com --><script>$CONFIG['islogin']='1';</script>"
    "<div>无匹配用户</div></body></html>"
)


class TestSearchUserByName:
    def _mock_response(self, status: int, body: str) -> MagicMock:
        """构造 mock 响应。

        显式把 ``.json`` 设为抛 ``json.JSONDecodeError`` 的 AsyncMock，
        以便 RED 阶段（旧实现走 ``resp.json()``）能稳定触发解码异常，
        而不是落到 MagicMock 默认行为导致的隐晦 ``TypeError``。
        """
        mock_resp = MagicMock()
        mock_resp.status = status
        mock_resp.text = AsyncMock(return_value=body)
        mock_resp.json = AsyncMock(side_effect=json.JSONDecodeError("Expecting value", body, 0))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    def _setup_mocks(self, mock_resp: MagicMock) -> tuple[MagicMock, MagicMock]:
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__.return_value = mock_session
        return mock_cls, mock_session

    @pytest.mark.asyncio
    async def test_returns_matching_users_from_html(self):
        """TDD-RED: 旧实现走 JSON 解析（resp.json()）。

        由于 ``_mock_response`` 显式把 ``.json`` 设为抛 ``JSONDecodeError``，
        旧实现调用 ``await resp.json()`` 时直接抛 ``json.JSONDecodeError``，
        测试失败，错误形式直观可读（不是隐晦的 MagicMock TypeError）。
        """
        mock_resp = self._mock_response(200, _SAMPLE_SEARCH_HTML)
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "人民日报")

        assert len(users) == 4  # 3 个人民日报 + 1 个 Tom & Jerry（重复 uid 去重）
        assert users[0]["id"] == 2803301701
        assert users[0]["screen_name"] == "人民日报"
        assert users[1]["id"] == 1411163204
        assert users[2]["screen_name"] == "人民日报体育"
        # html.unescape 路径
        assert users[3]["id"] == 9999999999
        assert users[3]["screen_name"] == "Tom & Jerry"

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_match(self):
        """页面正常返回但没有任何匹配项 → 空 list。

        样本通过风控校验（含 s.weibo.com + $CONFIG），避免与
        ``test_returns_empty_on_risk_control_page`` 走同一分支。
        """
        mock_resp = self._mock_response(200, _SAMPLE_EMPTY_RESULT_HTML)
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "不存在的用户名XYZ")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self):
        """服务端 404（与当前 bug 同症状）→ 空 list + warning log。"""
        mock_resp = self._mock_response(404, "<html>404</html>")
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_request_exception(self):
        """网络异常 → 空 list + exception log。

        注意: 这是「兜底回归」测试，验证 ``try/except Exception`` 兜住任何异常并返回 []。
        不依赖具体的异常类型——只要异常从 ``session.get`` 路径抛出，实现都应该返回 []。

        这里把异常挂在 ``session.get`` 上（更接近真实网络层抛 ClientError 的语义），
        而不是 ``__aenter__``，因为实现写的是
        ``try: async with session.get(...) as resp:``——异常既可能从 ``get()``
        也可能从 ``__aenter__`` 抛，但挂在 ``get`` 上语义更直白。
        """
        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__.return_value = mock_session

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_risk_control_page(self):
        """风控场景: s.weibo.com 返回 200，但页面是验证/跳转页（无 s.weibo.com 自身标记、无 $CONFIG）。

        必须与「正常但 0 结果」区分：当前实现先做页面有效性校验后 return []，
        避免把风控页错误地解析成「正常但 0 结果」。
        """
        risk_html = (
            "<!DOCTYPE html><html><head><title>验证码</title></head>"
            "<body><script>location.href='https://passport.weibo.com/...'</script>"
            "</body></html>"
        )
        mock_resp = self._mock_response(200, risk_html)
        mock_cls, _session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            users = await search_user_by_name("cookie", "test")

        assert users == []

    @pytest.mark.asyncio
    async def test_uses_pc_search_endpoint(self):
        """回归测试：断言请求走 s.weibo.com/user 而非 m.weibo.cn suggestion。"""
        mock_resp = self._mock_response(200, _SAMPLE_SEARCH_HTML)
        mock_cls, mock_session = self._setup_mocks(mock_resp)

        with patch("platforms.weibo.api.aiohttp.ClientSession", mock_cls):
            await search_user_by_name("cookie", "人民日报")

        called_url = mock_session.get.call_args[0][0]
        assert called_url.startswith("https://s.weibo.com/user"), (
            f"必须走 s.weibo.com PC 网页搜索，当前 URL: {called_url}"
        )
        assert "q=%E4%BA%BA%E6%B0%91%E6%97%A5%E6%8A%A5" in called_url  # urlencoded 人民日报
