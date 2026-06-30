# Issue #46 PR-2: weibo 视频支持 + 内联 AI 摘要移除

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 weibo 检测视频附件并支持下载,移除 download handler 内联 AI 摘要,使 weibo VIDEO 走完整 5 阶段 flow

**Architecture:** weibo detector 检查 WeiboPost.video_urls 字段决定注册为 VIDEO/TEXT。downloader 扩展支持 mp4 下载。download handler 移除内联摘要,改由 PHASE_FLOW 自然调度 SUMMARIZED handler(已有 `@register("*", Phase.SUMMARIZED)`)。

**Tech Stack:** Python 3.12, asyncio, aiohttp, pytest

---

## 文件结构

| 文件 | 改动类型 | 职责 |
|---|---|---|
| `shared/protocols.py` | Modify (L105-135) | WeiboPost 加 `video_urls` 字段;WeiboDownloadResult 加 `filepath` 字段(复用语义:视频路径) |
| `platforms/weibo/api.py` | Modify (L172-310) | `_parse_mobile_post` / `_parse_pc_post` 加视频字段解析分支 |
| `platforms/weibo/downloader.py` | Modify | 新增 `download_weibo_video` 函数,复用 `_download_file` |
| `platforms/weibo/handlers.py` | Modify (L30-149) | detector 按 video_urls 区分 VIDEO/TEXT;download handler 按 content_type 分支 + 移除内联摘要 |
| `platforms/bilibili/handlers.py` | Modify (L219-271) | `summarize_phase` 通用 handler 加 weibo 评论分支 |
| `core/engine.py` | Modify (L35-49) | `_flush_ctx_to_store` 简化为单一 if(SUMMARIZED 写 summary) |
| `tests/test_weibo_api.py` | Modify | 加视频字段解析测试 |
| `tests/test_weibo_downloader.py` | Modify | 加 `download_weibo_video` 测试 |
| `tests/test_weibo_handlers.py` | Create | 新建 detector + download handler 分支测试 |
| `tests/test_engine.py` | Modify | 删除 weibo 内联摘要测试,加 VIDEO 走 5 阶段测试 |

---

## Task 1: 探索 + 基线

**Files:**
- Read: 全部必读文件(见 plan 头部 Architecture)

- [ ] **Step 1: 确认在 issue 分支上**

Run: `git branch --show-current`
Expected: 类似 `feat/issue46-pr2-weibo-video`,如不在分支上,执行 `git checkout -b feat/issue46-pr2-weibo-video`

- [ ] **Step 2: 跑全量测试记录基线**

Run: `uv run pytest -x -q 2>&1 | tail -20`
Expected: 全部通过(假设 PR-1 已合入,无 DYNAMIC 引用)

- [ ] **Step 3: 跑 lint/type 基线**

Run: `uv run ruff check . && uv run pyright 2>&1 | tail -5`
Expected: All checks passed / 0 errors

- [ ] **Step 4: 审计 weibo 现有视频相关代码**

Run: `rg -n "video|mp4|stream_url|page_info" platforms/weibo/ shared/protocols.py`
Expected: 应当只在 protocols.py 的注释或 NoteInfo 看到 video 字段。weibo 模块当前完全没有视频处理逻辑(待证)。

- [ ] **Step 5: 审计 weibo_download 内联摘要代码块**

Run: `rg -n "analyze_content|summary_text|内联摘要" platforms/weibo/handlers.py`
Expected: 在 `platforms/weibo/handlers.py:114-134` 看到 21 行内联 AI 摘要代码块,本 PR 要整块删除。

---

## Task 2: WeiboPost 加 video_urls 字段 + WeiboDownloadResult 加 filepath

**Files:**
- Modify: `shared/protocols.py:105-135`
- Test: `tests/test_weibo_parser.py` (复用现有 `_make_post` fixture 验证默认值)

**理由:**
- `video_urls` 是 spec §3 显式要求的字段(dataclass 默认空 list)
- `WeiboDownloadResult.filepath` 必须新增 — `download_weibo_video` 返回视频路径,download handler 要把它写到 `ctx.downloaded_filepath`(否则 transcribe_phase 拿不到文件)
- 不复用 `image_paths`:语义混淆,且 `image_paths: list[Path]` 是 list 类型不适合单视频

- [ ] **Step 1: 写失败测试 — WeiboPost.video_urls 默认空 list**

> **TDD RED 阶段:** Step 1-2 / Step 3-4 写测试并验证失败(RED);Step 5-8 实现 + 验证通过(GREEN)。

在 `tests/test_weibo_parser.py` 文件末尾追加:

```python
def _make_video_post(video_urls: list[str] | None = None) -> WeiboPost:
    return WeiboPost(
        post_id="videopost1",
        text="视频微博",
        clean_text="视频微博",
        author="用户V",
        user_id="99999",
        pubdate=2000,
        video_urls=video_urls or [],
    )


class TestWeiboPostVideoField:
    def test_video_urls_defaults_to_empty_list(self):
        """WeiboPost 实例化时 video_urls 必须默认为空 list(spec §3)。"""
        post = WeiboPost(
            post_id="p1",
            text="t",
            clean_text="ct",
            author="a",
            user_id="u",
            pubdate=1,
        )
        assert post.video_urls == []

    def test_video_urls_accepts_list_of_urls(self):
        post = _make_video_post(
            video_urls=["https://example.com/v1.mp4", "https://example.com/v2.mp4"]
        )
        assert len(post.video_urls) == 2
        assert post.video_urls[0].endswith(".mp4")

    def test_existing_post_without_video_urls_still_works(self):
        """已有 WeiboPost 实例化位置(api.py parser, handlers 重建)不需改 — 默认值兜底。"""
        post = WeiboPost(
            post_id="legacy",
            text="t",
            clean_text="ct",
            author="a",
            user_id="u",
            pubdate=1,
        )
        # 旧代码没传 video_urls,字段必须存在且为空
        assert hasattr(post, "video_urls")
        assert post.video_urls == []
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_weibo_parser.py::TestWeiboPostVideoField -v`
Expected: FAIL — `AttributeError: 'WeiboPost' object has no attribute 'video_urls'` 或 dataclass 构造失败 `unexpected keyword argument 'video_urls'`

- [ ] **Step 3: 写失败测试 — WeiboDownloadResult.filepath 字段**

在 `tests/test_weibo_downloader.py` 末尾追加:

```python
class TestWeiboDownloadResultFilepathField:
    def test_filepath_defaults_to_none(self):
        """WeiboDownloadResult 必须有 filepath 字段,默认 None。
        
        download_weibo_video 把视频路径写入此字段,download handler 读它设到 ctx.downloaded_filepath。
        """
        from shared.protocols import WeiboDownloadResult

        result = WeiboDownloadResult(
            success=True,
            source_id="post1",
            title="t",
        )
        assert result.filepath is None

    def test_filepath_accepts_path(self):
        from pathlib import Path

        from shared.protocols import WeiboDownloadResult

        result = WeiboDownloadResult(
            success=True,
            source_id="post1",
            title="t",
            filepath=Path("/tmp/weibo/post1/post1.mp4"),
        )
        assert result.filepath == Path("/tmp/weibo/post1/post1.mp4")
```

- [ ] **Step 4: 运行测试验证失败**

Run: `uv run pytest tests/test_weibo_downloader.py::TestWeiboDownloadResultFilepathField -v`
Expected: FAIL — `AttributeError: 'WeiboDownloadResult' object has no attribute 'filepath'`

- [ ] **Step 5: 实现 — WeiboPost 加 video_urls 字段**

修改 `shared/protocols.py:105-122`,在 `reposted_post` 之前插入 `video_urls`:

```python
@dataclass
class WeiboPost:
    """微博帖子元信息"""

    post_id: str
    text: str
    clean_text: str
    author: str
    user_id: str
    pubdate: int  # Unix 时间戳
    image_urls: list[str] = field(default_factory=list)
    reposts_count: int = 0
    comments_count: int = 0
    likes_count: int = 0
    is_original: bool = True
    is_long_text: bool = False
    long_text: str = ""  # 长文全文（isLongText=True 时填充）
    # 视频直链(mp4)。detector 用此字段判断注册为 VIDEO 或 TEXT(spec §3 / issue #46)。
    # 实现:has_video = bool(video_urls)
    video_urls: list[str] = field(default_factory=list)
    reposted_post: WeiboPost | None = None  # 转发时可嵌套
```

- [ ] **Step 6: 实现 — WeiboDownloadResult 加 filepath 字段**

修改 `shared/protocols.py:125-135`,在 `image_paths` 之后加 `filepath`:

```python
@dataclass
class WeiboDownloadResult:
    """微博帖子下载结果"""

    success: bool
    source_id: str  # post_id
    title: str
    text: str = ""
    image_paths: list[Path] = field(default_factory=list)
    # 视频文件路径(VIDEO 类型,download_weibo_video 填入;TEXT 类型保持 None)
    filepath: Path | None = None
    error: str | None = None
    permanent: bool = False  # True = 永久失败（post 不存在/用户注销等），不 retry
```

- [ ] **Step 7: 运行测试验证通过**

Run: `uv run pytest tests/test_weibo_parser.py::TestWeiboPostVideoField tests/test_weibo_downloader.py::TestWeiboDownloadResultFilepathField -v`
Expected: PASS — 5 tests passed

- [ ] **Step 8: 跑全量回归确保 dataclass 默认值兜底**

Run: `uv run pytest -x -q 2>&1 | tail -5`
Expected: 全过(现有 WeiboPost 实例化位置不需要改)

- [ ] **Step 9: Commit**

```bash
git add shared/protocols.py tests/test_weibo_parser.py tests/test_weibo_downloader.py
git commit -m "feat(weibo): WeiboPost 加 video_urls 字段 + WeiboDownloadResult 加 filepath

为 PR-2 视频支持做数据模型准备(spec §3 / issue #46):
- WeiboPost.video_urls: list[str] — detector 据此判定 VIDEO/TEXT
- WeiboDownloadResult.filepath: Path | None — download_weibo_video 填入视频路径

dataclass 默认值兜底,现有 WeiboPost 实例化位置不需修改。"
```

---

## Task 3: api.py parser 解析视频字段

**Files:**
- Modify: `platforms/weibo/api.py:172-310` (`_parse_mobile_post`, `_parse_pc_post`)
- Test: `tests/test_weibo_api.py`

**API 调研结论(librarian 提供):**
- 移动端 `m.weibo.cn/statuses/show?id={bid}` 返回 `data.page_info.urls` (dict[str, str]) 多分辨率 + `data.page_info.media_info.stream_url` 兜底
- 但 **`fetch_user_posts` 拿到的列表接口(mobile cards / PC list) 也含 `page_info`** — 不需额外单条查询
- PC 端字段路径相同: `page_info.media_info.stream_url` / `page_info.urls`

**字段优先级:** `page_info.urls` (dict,取最高分辨率 mp4) → `page_info.media_info.stream_url_hd` → `page_info.media_info.stream_url`

- [ ] **Step 1: 写失败测试 — `_parse_mobile_post` 解析视频字段**

在 `tests/test_weibo_api.py` 的 `TestParseMobilePost` class 内追加:

```python
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
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_weibo_api.py::TestParseMobilePost -v -k "video or page_info"`
Expected: FAIL — 3 tests failed, `result.video_urls` 不存在或为空(因为实现还没加)

- [ ] **Step 3: 写失败测试 — `_parse_pc_post` 解析视频字段**

在 `tests/test_weibo_api.py` 的 `TestParsePcPost` class 内追加:

```python
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
```

- [ ] **Step 4: 运行测试验证失败**

Run: `uv run pytest tests/test_weibo_api.py::TestParsePcPost -v -k "video"`
Expected: FAIL — 2 tests failed

- [ ] **Step 5: 实现 — 抽取视频 URL 解析辅助函数**

在 `platforms/weibo/api.py` 中,在 `_parse_mobile_post` 之前(L170 附近)新增辅助函数:

```python
def _extract_video_urls(page_info: Any) -> list[str]:
    """从 page_info 提取视频直链(spec §3 / issue #46 PR-2)。

    优先级:
    1. page_info.urls (dict[str, str]) — 多分辨率 mp4 直链,取所有 .mp4 值
    2. page_info.media_info.stream_url_hd — 高清 mp4
    3. page_info.media_info.stream_url — 最低码率 mp4(兜底)

    Args:
        page_info: 原始 page_info 字段(dict),可能为 None / 非 dict

    Returns:
        视频 URL 列表;page_info.type != "video" 或无可用 URL 时返回空 list
    """
    if not isinstance(page_info, dict):
        return []
    if page_info.get("type") != "video":
        return []

    urls: list[str] = []

    # 1. page_info.urls (多分辨率 dict)
    pi_urls = page_info.get("urls")
    if isinstance(pi_urls, dict):
        for v in pi_urls.values():
            if isinstance(v, str) and v:
                urls.append(v)

    # 2/3. media_info 兜底
    if not urls:
        media_info = page_info.get("media_info")
        if isinstance(media_info, dict):
            hd = media_info.get("stream_url_hd")
            if isinstance(hd, str) and hd:
                urls.append(hd)
            else:
                low = media_info.get("stream_url")
                if isinstance(low, str) and low:
                    urls.append(low)

    return urls
```

- [ ] **Step 6: 实现 — `_parse_mobile_post` 调用辅助函数**

修改 `platforms/weibo/api.py` 的 `_parse_mobile_post`,在图片解析后(L207 之后)、统计数据之前,加视频解析:

```python
        # 视频直链(spec §3 / issue #46 PR-2)
        video_urls = _extract_video_urls(raw.get("page_info"))
```

然后在 `return WeiboPost(...)` 调用中追加 `video_urls=video_urls` 参数:

```python
        return WeiboPost(
            post_id=post_id,
            text=text,
            clean_text=clean_text,
            author=author,
            user_id=user_id,
            pubdate=pubdate,
            image_urls=image_urls,
            reposts_count=reposts_count,
            comments_count=comments_count,
            likes_count=likes_count,
            is_original=is_original,
            video_urls=video_urls,
            reposted_post=reposted_post,
        )
```

- [ ] **Step 7: 实现 — `_parse_pc_post` 调用辅助函数**

同上,在 `_parse_pc_post` 的图片解析后(L280 附近)加视频解析:

```python
        # 视频直链(spec §3 / issue #46 PR-2)
        video_urls = _extract_video_urls(raw.get("page_info"))
```

并在 `return WeiboPost(...)` 调用中追加 `video_urls=video_urls`(在 `is_long_text` 之前/`is_original` 之后):

```python
        return WeiboPost(
            post_id=post_id,
            text=text,
            clean_text=clean_text,
            author=author,
            user_id=user_id,
            pubdate=pubdate,
            image_urls=image_urls,
            reposts_count=reposts_count,
            comments_count=comments_count,
            likes_count=likes_count,
            is_original=is_original,
            is_long_text=is_long_text,
            video_urls=video_urls,
            reposted_post=reposted_post,
        )
```

- [ ] **Step 8: 运行测试验证通过**

Run: `uv run pytest tests/test_weibo_api.py::TestParseMobilePost tests/test_weibo_api.py::TestParsePcPost -v`
Expected: PASS — 全部测试通过

- [ ] **Step 9: 跑全量回归确保现有解析路径不破坏**

Run: `uv run pytest tests/test_weibo_api.py -q`
Expected: 全过

- [ ] **Step 10: Commit**

```bash
git add platforms/weibo/api.py tests/test_weibo_api.py
git commit -m "feat(weibo): api parser 解析视频字段(page_info.urls / stream_url)

spec §3 / issue #46 PR-2:
- 新增 _extract_video_urls 辅助函数(优先 page_info.urls,降级 stream_url_hd/stream_url)
- _parse_mobile_post / _parse_pc_post 调用辅助函数,填充 WeiboPost.video_urls
- page_info.type != 'video' 时不提取视频(避免误抓图文/直播卡片)"
```

---

## Task 4: download_weibo_video 新函数

**Files:**
- Modify: `platforms/weibo/downloader.py` (在文件末尾追加)
- Test: `tests/test_weibo_downloader.py`

**设计:**
- 复用 `_download_file` (已是 mp4 友好的通用下载函数)
- 文件路径:`{download_dir}/weibo/{post_id}/{post_id}.mp4`(与 bili/xhs 风格一致,单视频/单文件)
- 返回 `WeiboDownloadResult`,填 `filepath` 字段
- 无视频 URL → 返回 success=False, error="无视频 URL"

- [ ] **Step 1: 写失败测试 — `download_weibo_video` 基础行为**

在 `tests/test_weibo_downloader.py` 末尾追加:

```python
from platforms.weibo.downloader import download_weibo_video


def _make_video_post(video_urls: list[str] | None = None) -> WeiboPost:
    return WeiboPost(
        post_id="videopost1",
        text="视频微博",
        clean_text="视频微博",
        author="视频博主",
        user_id="88888",
        pubdate=2000,
        video_urls=video_urls or [],
    )


class TestDownloadWeiboVideo:
    @pytest.mark.asyncio
    async def test_downloads_video_successfully(self, tmp_path):
        """有 video_urls 时,下载第一个 URL 到 {post_id}.mp4,filepath 字段填入路径。"""
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_video_post(
            video_urls=["https://example.com/video.mp4"]
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"fake_mp4_bytes")
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await download_weibo_video(post, cfg)

        assert result.success is True
        assert result.source_id == "videopost1"
        assert result.filepath is not None
        assert result.filepath.exists()
        assert result.filepath.read_bytes() == b"fake_mp4_bytes"
        assert result.filepath.name == "videopost1.mp4"

    @pytest.mark.asyncio
    async def test_fails_when_no_video_urls(self, tmp_path):
        """video_urls 为空时返回 success=False,filepath=None。"""
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_video_post(video_urls=[])

        result = await download_weibo_video(post, cfg)

        assert result.success is False
        assert result.filepath is None
        assert result.error is not None
        assert "无视频" in result.error or "no video" in result.error.lower()

    @pytest.mark.asyncio
    async def test_fails_on_download_error(self, tmp_path):
        """HTTP 错误时返回 success=False,filepath=None。"""
        cfg = MagicMock()
        cfg.download.dir = str(tmp_path)

        post = _make_video_post(
            video_urls=["https://example.com/missing.mp4"]
        )

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__.return_value = mock_session
            result = await download_weibo_video(post, cfg)

        assert result.success is False
        assert result.filepath is None
        assert result.error is not None
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_weibo_downloader.py::TestDownloadWeiboVideo -v`
Expected: FAIL — `ImportError: cannot import name 'download_weibo_video'`

- [ ] **Step 3: 实现 — 新增 `download_weibo_video` 函数**

在 `platforms/weibo/downloader.py` 文件末尾追加:

```python
async def download_weibo_video(post: WeiboPost, config: Config) -> WeiboDownloadResult:
    """下载微博帖子的视频文件(mp4)。

    用于 VIDEO 类型 weibo 帖子(spec §3 / issue #46 PR-2)。
    下载到 ``{download_dir}/weibo/{post_id}/{post_id}.mp4``,填入返回值的 ``filepath`` 字段。

    Args:
        post: 微博帖子(需含 video_urls)
        config: 全局配置

    Returns:
        下载结果;``filepath`` 字段填入下载后的 mp4 路径
    """
    if not post.video_urls:
        return WeiboDownloadResult(
            success=False,
            source_id=post.post_id,
            title=post.clean_text[:50] if post.clean_text else post.post_id,
            text=post.clean_text,
            error="无视频 URL 可下载",
        )

    post_dir = _get_post_dir(config, post.post_id)
    video_path = post_dir / f"{post.post_id}.mp4"

    # 取第一个 URL(已在 api.py 按优先级排序:多分辨率 > stream_url_hd > stream_url)
    video_url = post.video_urls[0]
    ok = await _download_file(video_url, video_path)
    if not ok:
        return WeiboDownloadResult(
            success=False,
            source_id=post.post_id,
            title=post.clean_text[:50] if post.clean_text else post.post_id,
            text=post.clean_text,
            error="视频下载失败",
        )

    return WeiboDownloadResult(
        success=True,
        source_id=post.post_id,
        title=post.clean_text[:50] if post.clean_text else post.post_id,
        text=post.clean_text,
        filepath=video_path,
    )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_weibo_downloader.py::TestDownloadWeiboVideo -v`
Expected: PASS — 3 tests passed

- [ ] **Step 5: 跑全量回归**

Run: `uv run pytest tests/test_weibo_downloader.py -q`
Expected: 全过(原 `TestDownloadWeiboMedia` 不破坏)

- [ ] **Step 6: Commit**

```bash
git add platforms/weibo/downloader.py tests/test_weibo_downloader.py
git commit -m "feat(weibo): 新增 download_weibo_video 函数

spec §3 / issue #46 PR-2:
- 下载 mp4 到 {download_dir}/weibo/{post_id}/{post_id}.mp4
- 复用 _download_file(已是通用的 HTTP 下载函数)
- 无视频 URL / HTTP 错误时返回 success=False
- 成功时 filepath 字段填入下载后的 mp4 路径"
```

---

## Task 5: weibo_detector 视频检测(按 video_urls 区分 VIDEO/TEXT)

**Files:**
- Modify: `platforms/weibo/handlers.py:30-48` (weibo_detector)
- Test: `tests/test_weibo_handlers.py` (新建文件)

- [ ] **Step 1: 写失败测试 — detector 按 video_urls 区分类型**

创建 `tests/test_weibo_handlers.py`:

```python
"""Tests for platforms/weibo/handlers.py — detector + download handler.

PR-2 (issue #46): 视频检测 + download handler 按 content_type 分支 + 移除内联 AI 摘要。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platforms.weibo.handlers import weibo_detector, weibo_download
from shared.protocols import ContentType, Phase, PhaseContext, WeiboPost


def _make_post(
    post_id: str = "post1",
    video_urls: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> WeiboPost:
    return WeiboPost(
        post_id=post_id,
        text="t",
        clean_text="clean text",
        author="author",
        user_id="uid",
        pubdate=1000,
        image_urls=image_urls or [],
        video_urls=video_urls or [],
    )


def _make_config():
    cfg = MagicMock()
    cfg.weibo.subscriptions = [MagicMock(user_id="uid1")]
    cfg.weibo.auth.cookie = ""
    return cfg


class TestWeiboDetectorVideo:
    @pytest.mark.asyncio
    async def test_registers_as_video_when_video_urls_present(self):
        """post 含 video_urls 时 detector 注册为 ContentType.VIDEO(spec §3)。"""
        cfg = _make_config()
        store = MagicMock()

        posts_with_video = [_make_post(post_id="v1", video_urls=["https://x.com/v.mp4"])]

        with (
            patch(
                "platforms.weibo.handlers.fetch_user_posts",
                new=AsyncMock(return_value=posts_with_video),
            ),
            patch.object(store, "add_new") as mock_add,
        ):
            await weibo_detector(cfg, store)

        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args.kwargs
        assert call_kwargs["msg_id"] == "weibo:v1"
        assert call_kwargs["content_type"] == ContentType.VIDEO

    @pytest.mark.asyncio
    async def test_registers_as_text_when_no_video_urls(self):
        """post 无 video_urls 时 detector 注册为 ContentType.TEXT(当前行为)。"""
        cfg = _make_config()
        store = MagicMock()

        posts_text_only = [_make_post(post_id="t1", image_urls=["https://x.com/p.jpg"])]

        with (
            patch(
                "platforms.weibo.handlers.fetch_user_posts",
                new=AsyncMock(return_value=posts_text_only),
            ),
            patch.object(store, "add_new") as mock_add,
        ):
            await weibo_detector(cfg, store)

        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args.kwargs
        assert call_kwargs["msg_id"] == "weibo:t1"
        assert call_kwargs["content_type"] == ContentType.TEXT
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_weibo_handlers.py::TestWeiboDetectorVideo -v`
Expected: FAIL — 第一个测试失败(detector 当前硬编码 `content_type=ContentType.TEXT`)

- [ ] **Step 3: 实现 — detector 按 video_urls 区分**

修改 `platforms/weibo/handlers.py:30-48`:

```python
@PipelineEngine.register_detector("weibo")
async def weibo_detector(config: Config, store: MessageStore) -> None:
    """检测新微博帖子并加入 store。

    按 ``WeiboPost.video_urls`` 区分 VIDEO / TEXT(spec §3 / issue #46 PR-2):
    - 含视频直链 → VIDEO(走完整 5 阶段:下载→转写→摘要→推送)
    - 无视频 → TEXT(走 3 阶段:下载→推送)
    """
    for sub in config.weibo.subscriptions:
        posts = await fetch_user_posts(
            cookie=config.weibo.auth.cookie,
            user_id=sub.user_id,
            max_posts=10,
        )
        for p in posts:
            content_type = ContentType.VIDEO if p.video_urls else ContentType.TEXT
            store.add_new(
                msg_id=f"weibo:{p.post_id}",
                platform="weibo",
                content_type=content_type,
                pubdate=p.pubdate,
                title=p.clean_text[:50] if p.clean_text else p.post_id,
                author=p.author,
                subscription_ref=sub.user_id,
            )
```

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_weibo_handlers.py::TestWeiboDetectorVideo -v`
Expected: PASS — 2 tests passed

- [ ] **Step 5: Commit**

```bash
git add platforms/weibo/handlers.py tests/test_weibo_handlers.py
git commit -m "feat(weibo): detector 按 video_urls 区分 VIDEO/TEXT

spec §3 / issue #46 PR-2:
- post.video_urls 非空 → 注册为 ContentType.VIDEO(走 5 阶段)
- post.video_urls 为空 → 注册为 ContentType.TEXT(走 3 阶段,当前行为)"
```

---

## Task 6: weibo_download handler 按 content_type 分支 + 移除内联 AI 摘要

**Files:**
- Modify: `platforms/weibo/handlers.py:54-149` (weibo_download)
- Test: `tests/test_weibo_handlers.py`

**理由(同时做这两件):**
- weibo VIDEO 不再有内联摘要 → 走 SUMMARIZED 通用 handler → 需要在 download handler 设 `ctx.downloaded_filepath`
- 两个改动同时影响 weibo_download 函数,放一个 task 避免 cherry-pick 冲突

> **TDD RED 阶段:** Step 1-3 写测试并验证失败(RED,handler 当前不分 content_type、且仍调 analyze_content);Step 4-5 实现 + 验证通过(GREEN)。

- [ ] **Step 1: 写失败测试 — VIDEO 类型调 download_weibo_video 设 filepath**

在 `tests/test_weibo_handlers.py` 末尾追加:

```python
class TestWeiboDownloadVideoBranch:
    @pytest.mark.asyncio
    async def test_video_type_calls_download_weibo_video_and_sets_filepath(self):
        """VIDEO 类型 download handler 必须调 download_weibo_video 并设 ctx.downloaded_filepath。"""
        from pathlib import Path

        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:v1"
        msg.title = "视频微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.VIDEO

        ctx = PhaseContext(msg=msg, config=cfg)

        mock_video_result = MagicMock()
        mock_video_result.success = True
        mock_video_result.filepath = Path("/tmp/weibo/v1/v1.mp4")
        mock_video_result.text = "视频微博正文"
        mock_video_result.image_paths = []

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_video",
                new=AsyncMock(return_value=mock_video_result),
            ) as mock_video_fn,
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
        ):
            result = await weibo_download(ctx)

        assert result is True
        # 关键:调用了 download_weibo_video(而非 download_weibo_media)
        mock_video_fn.assert_called_once()
        # 关键:filepath 透传到 ctx(下游 transcribe_phase 需要)
        assert ctx.downloaded_filepath == Path("/tmp/weibo/v1/v1.mp4")
        assert ctx.content_text == "视频微博正文"

    @pytest.mark.asyncio
    async def test_video_type_does_not_call_analyze_content(self):
        """VIDEO 类型 download handler 不再调用 analyze_content(移除内联摘要)。

        spec §6: weibo VIDEO 通过 PHASE_FLOW 自然走 SUMMARIZED handler,
        由通用 summarize_phase 调 analyze_content,而非 download 内联。
        """
        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:v2"
        msg.title = "视频微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.VIDEO

        ctx = PhaseContext(msg=msg, config=cfg)

        mock_video_result = MagicMock()
        mock_video_result.success = True
        mock_video_result.filepath = MagicMock()
        mock_video_result.text = "正文"
        mock_video_result.image_paths = []

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_video",
                new=AsyncMock(return_value=mock_video_result),
            ),
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
            patch(
                "platforms.weibo.handlers.analyze_content",
                new=AsyncMock(),
            ) as mock_analyze,
        ):
            await weibo_download(ctx)

        # 关键:analyze_content 在 download handler 中绝对不能被调
        mock_analyze.assert_not_called()
```

- [ ] **Step 2: 写失败测试 — TEXT 类型保持图片下载且不调 analyze_content**

在 `tests/test_weibo_handlers.py` 继续追加:

```python
class TestWeiboDownloadTextBranch:
    @pytest.mark.asyncio
    async def test_text_type_calls_download_weibo_media(self):
        """TEXT 类型保持当前行为:调 download_weibo_media 下图片。"""
        from pathlib import Path

        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:t1"
        msg.title = "图文微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.TEXT

        ctx = PhaseContext(msg=msg, config=cfg)
        ctx.config.weibo.auth.cookie = ""  # 跳过长文获取

        mock_media_result = MagicMock()
        mock_media_result.success = True
        mock_media_result.image_paths = [Path("/tmp/img1.jpg")]
        mock_media_result.text = "图文正文"
        mock_media_result.filepath = None

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_media",
                new=AsyncMock(return_value=mock_media_result),
            ) as mock_media_fn,
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
        ):
            result = await weibo_download(ctx)

        assert result is True
        mock_media_fn.assert_called_once()
        assert ctx.image_paths == [Path("/tmp/img1.jpg")]
        assert ctx.content_text == "图文正文"

    @pytest.mark.asyncio
    async def test_text_type_does_not_call_analyze_content(self):
        """TEXT 类型不再调 analyze_content(移除内联摘要,推全文)。"""
        cfg = _make_config()

        msg = MagicMock()
        msg.msg_id = "weibo:t2"
        msg.title = "图文微博"
        msg.author = "博主"
        msg.pubdate = 1000
        msg.content_type = ContentType.TEXT

        ctx = PhaseContext(msg=msg, config=cfg)
        ctx.config.weibo.auth.cookie = ""

        mock_media_result = MagicMock()
        mock_media_result.success = True
        mock_media_result.image_paths = []
        mock_media_result.text = "正文"
        mock_media_result.filepath = None

        with (
            patch(
                "platforms.weibo.handlers.download_weibo_media",
                new=AsyncMock(return_value=mock_media_result),
            ),
            patch("platforms.weibo.handlers.parse_weibo_post", new=MagicMock(return_value=None)),
            patch(
                "platforms.weibo.handlers.analyze_content",
                new=AsyncMock(),
            ) as mock_analyze,
            patch(
                "platforms.weibo.handlers.fetch_weibo_comment_highlights",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await weibo_download(ctx)

        # 关键:TEXT 类型也不再调 analyze_content(推全文,无 AI 摘要)
        mock_analyze.assert_not_called()
```

- [ ] **Step 3: 运行所有新测试验证失败**

Run: `uv run pytest tests/test_weibo_handlers.py -v`
Expected: FAIL — 4 tests failed(VIDEO 没调 video 下载;analyze_content 被调用)

- [ ] **Step 4: 实现 — 重写 weibo_download handler**

修改 `platforms/weibo/handlers.py:54-149`,完整替换 `weibo_download` 函数:

```python
@PipelineEngine.register("weibo", Phase.DOWNLOADED)
async def weibo_download(ctx: PhaseContext) -> bool:
    """下载微博媒体(VIDEO: 下视频 / TEXT: 下图片)。

    spec §3 / §6 / issue #46 PR-2:
    - VIDEO 类型调 download_weibo_video,设 ctx.downloaded_filepath,后续走 transcribe + summarize
    - TEXT 类型调 download_weibo_media(下图片),推全文,不再内联 AI 摘要
    - 内联 AI 摘要代码块已移除,由 PHASE_FLOW 自然调度通用 summarize_phase
    """
    post_id = ctx.msg.msg_id.replace("weibo:", "")
    logger.info("⬇ 下载 %s (%s, %s)...", ctx.msg.title, post_id, ctx.msg.content_type.name)

    # Reconstruct WeiboPost from MessageRecord
    from shared.protocols import WeiboPost

    post = WeiboPost(
        post_id=post_id,
        text="",
        clean_text=ctx.msg.title,
        author=ctx.msg.author,
        user_id="",
        pubdate=ctx.msg.pubdate,
    )

    # 尝试获取完整长文（仅当 title 看起来被截断时才请求）
    # title 是 monitor 阶段截断到 50 字符的预览；长度等于 50 表明原文更长，
    # 需要拉长文；短于 50 直接用 title 作为 clean_text，省一次 HTTP 调用。
    cookie = ctx.config.weibo.auth.cookie
    if cookie and len(post.clean_text) >= 50:
        from platforms.weibo.api import _fetch_long_text

        full_text = await _fetch_long_text(cookie, post_id)
        if full_text:
            post.clean_text = full_text

    # 按 content_type 分支:VIDEO 走视频下载,TEXT 走图片下载
    if ctx.msg.content_type == ContentType.VIDEO:
        try:
            result = await download_weibo_video(post=post, config=ctx.config)
        except Exception as exc:
            ctx.error = f"视频下载失败: {exc}"
            logger.error("✗ %s", ctx.error)
            logger.exception("Weibo video download failed for %s", post_id)
            return False

        if not result.success:
            ctx.error = result.error or "视频下载未成功"
            if result.permanent:
                ctx.permanent_error = True
            logger.warning("⚠️  %s", ctx.error)
            return False

        # 关键:透传 filepath 到 ctx(下游 transcribe_phase 需要)
        ctx.downloaded_filepath = result.filepath
        ctx.content_text = result.text
        logger.info("✓ 视频下载完成")
        return True

    # TEXT 类型:走图片下载(原行为)
    try:
        result = await download_weibo_media(post=post, config=ctx.config)
    except Exception as exc:
        ctx.error = f"下载失败: {exc}"
        logger.error("✗ %s", ctx.error)
        logger.exception("Weibo download failed for %s", post_id)
        return False

    if not result.success:
        ctx.error = result.error or "下载未成功"
        if result.permanent:
            ctx.permanent_error = True
        logger.warning("⚠️  %s", ctx.error)
        return False

    ctx.image_paths = result.image_paths
    ctx.content_text = result.text
    logger.info("✓ 下载完成")

    # Parse content
    try:
        parsed = parse_weibo_post(post=post, download_result=result)
        if parsed:
            ctx.content_text = parsed.get("text", ctx.content_text)
    except Exception as exc:
        logger.warning("⚠️  内容解析失败: %s", exc)
        logger.warning("Weibo parse failed for %s: %s", post_id, exc)

    # TEXT 类型在 download 阶段抓评论(SUMMARIZED 阶段只对 VIDEO 触发,TEXT 不走那里)
    try:
        highlights = await fetch_weibo_comment_highlights(
            post_id=post_id,
            config=ctx.config,
        )
        ctx.comment_highlights = format_comment_highlights(highlights)
        if highlights:
            logger.info("💬 获取到 %d 条热门评论", len(highlights))
    except Exception as exc:
        logger.warning("⚠️  评论获取失败: %s", exc)
        ctx.comment_highlights = ""

    return True
```

**关键变更点:**
- 顶部 import 删除 `analyze_content`(不再使用);新增 `download_weibo_video`(VIDEO 分支用,统一顶部 import 风格,避免函数内 import 与 TEXT 分支不一致)
- VIDEO 分支调 `download_weibo_video`,设 `ctx.downloaded_filepath`,**直接 return**(不走图片解析/评论抓取,因为 VIDEO 走 SUMMARIZED 通用 handler 会抓评论)
- TEXT 分支保留图片下载 + parse_weibo_post + 评论抓取(评论必须在 download 抓,TEXT 不走 SUMMARIZED)
- 删除原来的内联 AI 摘要代码块(L114-134)

- [ ] **Step 5: 修复顶部 import**

修改 `platforms/weibo/handlers.py:7-22`。基于现有 import 块做最小修改:**只删 `analyze_content` 一行**(其他全部保留 — weibo_push 仍用 `send_to_subscription`,download handler 用 `format_comment_highlights` / `fetch_weibo_comment_highlights` / `parse_weibo_post`),并**新增 `download_weibo_video` import**(VIDEO 分支用,与 TEXT 分支的顶部 import 风格保持一致):

```python
from __future__ import annotations

# pyright: basic
import logging

from core.engine import PipelineEngine
from core.formatter import format_comment_highlights
from core.notifiers import send_to_subscription
from platforms.weibo.api import fetch_user_posts
from platforms.weibo.comments import fetch_weibo_comment_highlights
from platforms.weibo.downloader import download_weibo_media, download_weibo_video
from platforms.weibo.parser import parse_weibo_post
from shared.config import Config
from shared.message_store import MessageStore
from shared.protocols import ContentType, NotificationContent, Phase, PhaseContext

logger = logging.getLogger("trawler.weibo.handlers")
```

**注意:** 实现时务必对照现有 L7-22 的实际 import 块,只做最小差异修改 — 删 `from core.summarizer import analyze_content`,把 `from platforms.weibo.downloader import download_weibo_media` 改为 `from platforms.weibo.downloader import download_weibo_media, download_weibo_video`。其余 import 原样保留(包括 `send_to_subscription`,weibo_push handler 在用)。

- [ ] **Step 6: 运行测试验证通过**

Run: `uv run pytest tests/test_weibo_handlers.py -v`
Expected: PASS — 4 tests passed (detector 2 + download 4)

- [ ] **Step 7: Commit**

```bash
git add platforms/weibo/handlers.py tests/test_weibo_handlers.py
git commit -m "refactor(weibo): download handler 按 content_type 分支 + 移除内联 AI 摘要

spec §3 / §6 / issue #46 PR-2:
- VIDEO 类型调 download_weibo_video,设 ctx.downloaded_filepath
  (下游 transcribe_phase + summarize_phase 通过 PHASE_FLOW 自然调度)
- TEXT 类型保持图片下载,在 download 抓评论(TEXT 不走 SUMMARIZED)
- 删除内联 AI 摘要代码块(analyze_content 调用)
- 顶部 import:删除 analyze_content,新增 download_weibo_video(统一顶部 import 风格)"
```

---

## Task 7: summarize_phase 加 weibo 评论分支

**Files:**
- Modify: `platforms/bilibili/handlers.py:219-271` (summarize_phase)
- Test: `tests/test_engine.py` (在 summarize_phase 测试块附近加)

**理由:**
- spec §5: 「weibo VIDEO 抓评论用 `fetch_weibo_comment_highlights`(已有)」
- 当前 `summarize_phase` 只有 bili / xhs 分支,weibo VIDEO 走 SUMMARIED 时不会抓评论 → 必须补
- 注意:weibo TEXT 不走 SUMMARIZED(在 download 抓评论,见 Task 6)

- [ ] **Step 1: 写失败测试 — weibo VIDEO 在 SUMMARIZED 抓评论**

在 `tests/test_engine.py` 找到 `test_summarize_phase_returns_true_when_analysis_succeeds` (L490),在其后追加:

```python
@pytest.mark.asyncio
async def test_summarize_phase_fetches_weibo_comments_for_video(
    config: Config, store: MessageStore
) -> None:
    """weibo VIDEO 走 SUMMARIZED 时,summarize_phase 必须抓 weibo 评论(spec §5 / issue #46 PR-2)。

    weibo TEXT 不走 SUMMARIED 阶段(在 download 抓评论),此测试只覆盖 VIDEO。
    """
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    try:
        import platforms.bilibili.handlers  # noqa: F401  (注册通用 summarize_phase)

        config.analysis.enabled = False  # 跳过 LLM 调用,聚焦评论抓取验证

        # mock fetch_weibo_comment_highlights 返回非空
        mock_highlight = MagicMock()
        with patch(
            "platforms.weibo.comments.fetch_weibo_comment_highlights",
            new=AsyncMock(return_value=[mock_highlight]),
        ) as mock_fetch:
            handler = PipelineEngine._handlers.get(("*", Phase.SUMMARIZED))
            assert handler is not None

            msg = store.add_new("weibo:v1", "weibo", ContentType.VIDEO, 2000000000, "T", "A")
            assert msg is not None
            ctx = PhaseContext(msg=msg, config=config)
            ctx.transcript_text = "transcript"  # 提供 text_to_summarize

            result = await handler(ctx)

        assert result is True
        # 关键:weibo VIDEO 触发了评论抓取
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args.kwargs.get("post_id") == "v1"
        # comment_highlights 被填充(非空字符串)
        # format_comment_highlights 对非空 list 会返回非空 str
        assert ctx.comment_highlights  # truthy
    finally:
        sys.modules.pop("platforms.bilibili.handlers", None)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_engine.py::test_summarize_phase_fetches_weibo_comments_for_video -v`
Expected: FAIL — `mock_fetch.assert_called_once()` 失败(summarize_phase 没有 weibo 分支)

- [ ] **Step 3: 实现 — summarize_phase 加 weibo 分支**

修改 `platforms/bilibili/handlers.py:219-244`,在 xhs 分支之后追加 weibo 分支:

```python
@PipelineEngine.register("*", Phase.SUMMARIZED)
async def summarize_phase(ctx: PhaseContext) -> bool:
    """生成摘要+关键词+评论亮点（跨平台共用 handler）。"""
    source_id = ctx.msg.msg_id
    logger.info("💬 获取评论亮点...")

    if ctx.msg.platform == "bili" and ctx.msg.content_type == ContentType.VIDEO:
        bvid = source_id.replace("bili:", "")
        try:
            highlights = await fetch_comment_highlights(bvid=bvid, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("Comment highlights failed for %s: %s", source_id, exc)

    elif ctx.msg.platform == "xhs":
        note_id = source_id.replace("xhs:", "")
        try:
            from platforms.xiaohongshu.comments import fetch_xhs_comment_highlights

            highlights = await fetch_xhs_comment_highlights(note_id=note_id, config=ctx.config)
            ctx.comment_highlights = format_comment_highlights(highlights)
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("XHS comment highlights failed for %s: %s", source_id, exc)

    elif ctx.msg.platform == "weibo":
        # spec §5 / issue #46 PR-2: weibo VIDEO 抓评论(TEXT 不走 SUMMARIZED 阶段)
        post_id = source_id.replace("weibo:", "")
        try:
            from platforms.weibo.comments import fetch_weibo_comment_highlights

            highlights = await fetch_weibo_comment_highlights(
                post_id=post_id, config=ctx.config
            )
            ctx.comment_highlights = format_comment_highlights(highlights)
        except Exception as exc:
            logger.warning("⚠️  评论获取失败: %s", exc)
            logger.warning("Weibo comment highlights failed for %s: %s", source_id, exc)

    logger.info("🤖 生成摘要...")
    # ...(后续不变)
```

注意:**只新增 elif 分支,函数后半部分(text_to_summarize + analyze_content)保持不变**。

- [ ] **Step 4: 运行测试验证通过**

Run: `uv run pytest tests/test_engine.py::test_summarize_phase_fetches_weibo_comments_for_video tests/test_engine.py::test_summarize_phase_returns_true_when_analysis_succeeds tests/test_engine.py::test_summarize_phase_returns_false_on_analysis_failed -v`
Expected: PASS — 3 tests passed

- [ ] **Step 5: Commit**

```bash
git add platforms/bilibili/handlers.py tests/test_engine.py
git commit -m "feat(weibo): summarize_phase 加 weibo 评论分支

spec §5 / issue #46 PR-2:
- weibo VIDEO 走 SUMMARIED 时抓评论(fetch_weibo_comment_highlights)
- TEXT 不走 SUMMARIED 阶段(评论在 download handler 抓)"
```

---

## Task 8: engine.py _flush_ctx_to_store 简化

**Files:**
- Modify: `core/engine.py:35-49` (`_flush_ctx_to_store`)
- Test: `tests/test_engine.py`

**理由:**
- spec §6: 「engine.py `_flush_ctx_to_store` 中为兼容 weibo 路径的双 if 回写(line 48-49)可以简化为单一路径」
- weibo 不再在 DOWNLOADED 写 summary(Task 6 已移除),所以 `if ctx.summary_text and just_completed in (Phase.DOWNLOADED, Phase.SUMMARIZED)` 可简化为 `if ctx.summary_text and just_completed == Phase.SUMMARIZED`

- [ ] **Step 1: 写失败测试 — DOWNLOADED 不再 flush summary**

在 `tests/test_engine.py` 找到 `test_process_message_flushes_inline_summary_after_downloaded` (L394-421)。这个测试断言 DOWNLOADED 后能读到 summary,本 PR 要把它**重写**为反向断言:

```python
@pytest.mark.asyncio
async def test_process_message_does_not_flush_summary_after_downloaded(
    config: Config, store: MessageStore
) -> None:
    """spec §6 / issue #46 PR-2: 移除 weibo 内联摘要后,_flush_ctx_to_store 简化。
    
    DOWNLOADED handler 即使设置了 ctx.summary_text,engine 也不应在 DOWNLOADED 后 flush
    summary 到 store(只有 SUMMARIZED 阶段才 flush)。
    """
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    @PipelineEngine.register("weibo", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        ctx.content_text = "微博正文"
        ctx.summary_text = "误设的内联摘要"  # 模拟历史代码遗留
        return True

    @PipelineEngine.register("weibo", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        return True

    msg = store.add_new("weibo:post1", "weibo", ContentType.TEXT, 2000000000, "T", "A")
    assert msg is not None
    await PipelineEngine.process_message(msg, config, store)

    updated = store.get_message("weibo:post1")
    assert updated is not None
    # body 来自 content_text
    assert updated.body == "微博正文"
    # 关键:summary 不应在 DOWNLOADED 阶段被 flush(只有 SUMMARIZED 阶段才 flush)
    assert updated.summary == ""
```

- [ ] **Step 2: 运行测试验证失败**

Run: `uv run pytest tests/test_engine.py::test_process_message_does_not_flush_summary_after_downloaded -v`
Expected: FAIL — 当前实现双 if,DOWNLOADED 会 flush summary,断言 `updated.summary == ""` 失败

- [ ] **Step 3: 删除旧的 weibo 内联摘要测试**

删除 `tests/test_engine.py:394-421` 的 `test_process_message_flushes_inline_summary_after_downloaded`(被 Step 1 的新测试替代)。

同时删除 `tests/test_engine.py:526-582` 的 `test_weibo_download_returns_false_on_summary_failed`(weibo download 不再做摘要,此测试无意义)。

Run: `uv run pytest tests/test_engine.py -k "inline_summary or weibo_download_returns_false_on_summary" -v`
Expected: 0 tests selected(确认已删)

- [ ] **Step 4: 实现 — `_flush_ctx_to_store` 简化为单一 if**

修改 `core/engine.py:35-49`:

```python
def _flush_ctx_to_store(msg_id: str, ctx: PhaseContext, store: MessageStore, just_completed: Phase) -> None:
    """阶段推进成功后，把 ctx 上对应阶段的产出回写到 store（plan D5）。

    - DOWNLOADED 完成：ctx.content_text → body（截断到 _BODY_MAX_CHARS）
    - SUMMARIZED 完成：ctx.summary_text → summary
    
    spec §6 / issue #46 PR-2: 移除 weibo 内联摘要路径后,
    summary 只在 SUMMARIZED 阶段 flush(不再有 DOWNLOADED 双 if 兼容)。
    """
    if just_completed == Phase.DOWNLOADED and ctx.content_text:
        body = ctx.content_text[:_BODY_MAX_CHARS]
        if len(ctx.content_text) > _BODY_MAX_CHARS:
            body += "…"
        store.mark_body(msg_id, body)
    if just_completed == Phase.SUMMARIZED and ctx.summary_text:
        store.mark_summary(msg_id, ctx.summary_text)
```

- [ ] **Step 5: 运行测试验证通过**

Run: `uv run pytest tests/test_engine.py::test_process_message_does_not_flush_summary_after_downloaded tests/test_engine.py::test_process_message_flushes_summary_after_summarized tests/test_engine.py::test_process_message_flushes_body_after_download -v`
Expected: PASS — 3 tests passed

- [ ] **Step 6: Commit**

```bash
git add core/engine.py tests/test_engine.py
git commit -m "refactor(engine): _flush_ctx_to_store 简化(spec §6)

issue #46 PR-2:
- 移除 weibo 内联摘要路径后,summary 只在 SUMMARIZED 阶段 flush
- 双 if 兼容(DOWNLOADED 或 SUMMARIZED)简化为单一 if(仅 SUMMARIZED)
- 删除 weibo 内联摘要相关测试(已无此路径):
  - test_process_message_flushes_inline_summary_after_downloaded
  - test_weibo_download_returns_false_on_summary_failed"
```

---

## Task 9: 最终验证 + 提交

**Files:**
- 全部改动文件

- [ ] **Step 1: 跑全量测试**

Run: `uv run pytest -x -q 2>&1 | tail -10`
Expected: 全部通过(含本 PR 新增测试 + 现有测试)

- [ ] **Step 2: 跑 lint**

Run: `uv run ruff check . 2>&1 | tail -5`
Expected: `All checks passed!`

- [ ] **Step 3: 跑 type check(无参数)**

Run: `uv run pyright 2>&1 | tail -5`
Expected: `0 errors, 0 warnings`

- [ ] **Step 4: 手动验证 — weibo VIDEO 走完整 5 阶段**

Run:
```bash
# 删 messages.json 重置(用户已确认数据可重置,spec §7)
rm -f data/messages.json

# 跑 weibo 平台(假设有视频博主的订阅)
uv run trawler check --platform weibo
```
Expected:
- 看到 `[cyan]🔍 开始检查 weibo 平台...[/]`
- 看到 `weibo:{post_id} → DOWNLOADED ✓`
- 视频 post 看到 `→ TRANSCRIBED ✓` → `→ SUMMARIZED ✓` → `→ PUSHED ✓`
- 图文 post 看到 `→ DOWNLOADED ✓` → `→ PUSHED ✓`(不走 TRANSCRIBED/SUMMARIZED)

- [ ] **Step 5: 检查 git status 确认无遗漏**

Run: `git status`
Expected: `nothing to commit, working tree clean`(所有改动已分 task 提交)

- [ ] **Step 6: 查看 commit 历史**

Run: `git log --oneline -8`
Expected: 看到 7 个 commit(本 PR 的 Task 2-8 各一个):
1. `feat(weibo): WeiboPost 加 video_urls 字段 + WeiboDownloadResult 加 filepath`
2. `feat(weibo): api parser 解析视频字段`
3. `feat(weibo): 新增 download_weibo_video 函数`
4. `feat(weibo): detector 按 video_urls 区分 VIDEO/TEXT`
5. `refactor(weibo): download handler 按 content_type 分支 + 移除内联 AI 摘要`
6. `feat(weibo): summarize_phase 加 weibo 评论分支`
7. `refactor(engine): _flush_ctx_to_store 简化`

---

## Self-Review

### Spec coverage(spec §3, §6 全部要求)

| spec 要求 | 对应 Task |
|---|---|
| §3 WeiboPost 加 `video_urls: list[str]` | Task 2 ✓ |
| §3 weibo_detector 按 video_urls 注册为 VIDEO/TEXT | Task 5 ✓ |
| §3 api.py parser 解析视频字段(page_info.urls / stream_url) | Task 3 ✓ |
| §3 新增 `download_weibo_video` 函数 | Task 4 ✓ |
| §3 weibo_download 按 content_type 分支(VIDEO 调视频下载,TEXT 走图片) | Task 6 ✓ |
| §6 移除 weibo_download 内联 AI 摘要(line 116-134) | Task 6 ✓ |
| §6 engine.py `_flush_ctx_to_store` 简化(移除双 if) | Task 8 ✓ |
| §5(隐含)weibo VIDEO 抓评论用 fetch_weibo_comment_highlights | Task 7 ✓ |

### Placeholder scan

无 placeholder。每个 task 的代码块都是可直接复制粘贴的完整实现。

### Type consistency

- `video_urls: list[str]` — Task 2/3/4/5/6 全程一致
- `filepath: Path | None` (WeiboDownloadResult) — Task 2 定义,Task 4 填,Task 6 读,一致
- `ctx.downloaded_filepath: Path | None` — Task 6 写,通用 transcribe_phase 读,一致
- `download_weibo_video(post, config) -> WeiboDownloadResult` — Task 4 定义,Task 6 调用,签名一致
- `_extract_video_urls(page_info) -> list[str]` — Task 3 内部辅助,签名一致

### Mock aiohttp 模式

所有测试均采用项目现有范式(test_weibo_downloader.py / test_weibo_api.py):
```python
mock_resp = MagicMock()
mock_resp.status = 200
mock_resp.read = AsyncMock(return_value=b"data")
mock_session = MagicMock()
mock_session.get = AsyncMock(return_value=mock_resp)
with patch("platforms.weibo.downloader.aiohttp.ClientSession") as mock_cls:
    mock_cls.return_value.__aenter__.return_value = mock_session
```

### 预估执行时间

- Task 1: 5 min(基线)
- Task 2: 10 min(2 个字段 + 测试)
- Task 3: 15 min(parser 双端 + 5 个测试)
- Task 4: 10 min(下载函数 + 3 个测试)
- Task 5: 10 min(detector + 2 个测试)
- Task 6: 15 min(handler 重写 + 4 个测试,本 PR 最大改动)
- Task 7: 10 min(summarize 分支 + 1 个测试)
- Task 8: 10 min(engine 简化 + 删除旧测试 + 新测试)
- Task 9: 10 min(验证)

**总计: ~95 min**
