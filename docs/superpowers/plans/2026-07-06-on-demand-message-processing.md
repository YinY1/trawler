# Implementation Plan: 按需消息处理(fetch-and-process)

- **Issue**: [#101](https://github.com/YinY1/trawler/issues/101)
- **设计文档**: `docs/superpowers/specs/2026-07-06-on-demand-message-processing-design.md`
- **分支**: `feat/on-demand-message-processing`
- **日期**: 2026-07-06
- **状态**: Ready

---

## 目标

为不在 store 中的历史消息提供独立入口（CLI + API），让其走完整流水线：
按 ID 抓取 → 入库（突破 24h 限制）→ detector 之后的所有阶段 → 推送。

## 范围

- **in**: `FetchedMessage` / `PermanentFetchError` / `add_new(force=True)` /
  三平台 `fetch_by_id` / `run_fetch_and_process` / CLI `trawler fetch` /
  API `POST /messages/fetch`
- **out**: Web UI fetch 表单 / cleanup 保护 / xsec_token 可选参数

## 实现顺序与 Task 列表

按依赖顺序排列；每个 task 严格遵循 TDD：**先写测试 → 跑红 → 实现 → 跑绿**。
所有验证命令：`uv run pytest -x <test>`(单测) → `uv run ruff check .` → `uv run pyright`。

---

## Task 1: `FetchedMessage` dataclass（`shared/protocols.py`）

### 1.1 文件路径

- 实现: `shared/protocols.py`
- 测试: `tests/test_fetched_message.py`（新建）

### 1.2 红 — 先写测试

```python
# tests/test_fetched_message.py
from __future__ import annotations

from shared.protocols import ContentType, FetchedMessage


def test_fetched_message_required_fields():
    """必填字段构造成功。"""
    fm = FetchedMessage(
        msg_id="bili:BV1xx",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=1700000000,
        title="测试视频",
        author="UP主",
    )
    assert fm.msg_id == "bili:BV1xx"
    assert fm.platform == "bili"
    assert fm.content_type is ContentType.VIDEO
    assert fm.pubdate == 1700000000


def test_fetched_message_optional_fields_default_empty():
    """xsec_token / body 默认空字符串。"""
    fm = FetchedMessage(
        msg_id="xhs:abc",
        platform="xhs",
        content_type=ContentType.TEXT,
        pubdate=0,
        title="",
        author="",
    )
    assert fm.xsec_token == ""
    assert fm.body == ""
```

跑：`uv run pytest -x tests/test_fetched_message.py` → **应 ImportError 失败**（红）。

### 1.3 绿 — 实现

在 `shared/protocols.py` 文件末尾（所有 dataclass 之后）追加：

```python
# ═══════════════════════════════════════════════════════════
# fetch_by_id 返回载体（按需消息处理入口专用，issue #101）
# ═══════════════════════════════════════════════════════════


@dataclass
class FetchedMessage:
    """``fetch_by_id`` 返回的轻量载体，字段映射到 ``MessageRecord`` 所需。

    与 ``MessageRecord`` 区别：无 phase / error / retry_count 等流水线状态，
    仅承载"原始抓取结果"。引擎层 ``run_fetch_and_process`` 调
    ``store.add_new(force=True)`` 时把这些字段写入 ``MessageRecord``。

    所有平台 fetcher（bili/xhs/weibo）统一返回此类型。
    """

    msg_id: str  # "{platform}:{id}" e.g. "bili:BV1xx"
    platform: str  # "bili" | "xhs" | "weibo"
    content_type: ContentType
    pubdate: int
    title: str
    author: str
    xsec_token: str = ""  # 仅 xhs 用，bili/weibo 保持空
    body: str = ""  # 内容正文（weibo 长文 / xhs desc / bili 视频无正文留空）
```

跑：`uv run pytest -x tests/test_fetched_message.py` → **绿**。

### 1.4 验证

```bash
uv run pytest -x tests/test_fetched_message.py
uv run ruff check shared/protocols.py tests/test_fetched_message.py
uv run pyright
```

---

## Task 2: `PermanentFetchError` 异常（`shared/exceptions.py`）

### 2.1 文件路径

- 实现: `shared/exceptions.py`
- 测试: `tests/test_exceptions.py`（若存在则追加，否则新建）

### 2.2 红 — 先写测试

```python
# tests/test_exceptions.py（追加到现有文件末尾，或新建）
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
```

跑：`uv run pytest -x tests/test_exceptions.py` → **应 ImportError 失败**（红）。

### 2.3 绿 — 实现

在 `shared/exceptions.py` 的 `RetryableError` 之后追加：

```python
class PermanentFetchError(TrawlerError):
    """按 ID 抓取永久失败（issue #101）。

    调用方（``run_fetch_and_process``）应明示给用户、不重试、不创建 record。

    典型场景：
    - xhs ``xsec_token`` 缺失导致 server 拒绝（``DataError`` 等价信号）
    - 平台明确返回"资源不存在 / 已删除"
    - ``note_card`` 正文为空（desc/image_list/video 全空）

    与 ``NotFoundError`` 区别：``NotFoundError`` 是数据层"资源不存在"，
    ``PermanentFetchError`` 是抓取层"无法获取"的更宽口径（含 token 缺失等）。
    """
```

跑：`uv run pytest -x tests/test_exceptions.py` → **绿**。

### 2.4 验证

```bash
uv run pytest -x tests/test_exceptions.py
uv run ruff check shared/exceptions.py
uv run pyright
```

---

## Task 3: `MessageStore.add_new` 加 `force` 参数（`shared/message_store.py`）

### 3.1 文件路径

- 实现: `shared/message_store.py`
- 测试: `tests/test_message_store.py`（追加）

### 3.2 红 — 先写测试

在 `tests/test_message_store.py` 末尾追加：

```python
def test_add_new_force_bypasses_time_window(tmp_path):
    """``force=True`` 绕过 24h 时间窗，允许任意历史消息入库（issue #101）。"""
    from shared.protocols import ContentType

    store = MessageStore(str(tmp_path))
    # pubdate 设为 30 天前（远超 24h 窗口）
    old_pubdate = int(time.time()) - 30 * 86400

    # 默认（force=False）：超 24h 被丢弃
    rec_default = store.add_new(
        msg_id="bili:BV_old",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=old_pubdate,
        title="历史视频",
        author="UP",
    )
    assert rec_default is None

    # force=True：突破时间窗
    rec_force = store.add_new(
        msg_id="bili:BV_old",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=old_pubdate,
        title="历史视频",
        author="UP",
        force=True,
    )
    assert rec_force is not None
    assert rec_force.msg_id == "bili:BV_old"
    assert rec_force.pubdate == old_pubdate


def test_add_new_force_still_respects_is_known(tmp_path):
    """``force=True`` 不绕过 ``is_known`` 去重（重复 fetch 不创建重复记录）。"""
    from shared.protocols import ContentType

    store = MessageStore(str(tmp_path))
    old_pubdate = int(time.time()) - 30 * 86400

    rec1 = store.add_new(
        msg_id="xhs:note1",
        platform="xhs",
        content_type=ContentType.TEXT,
        pubdate=old_pubdate,
        title="笔记",
        author="作者",
        force=True,
    )
    assert rec1 is not None

    # 重复 fetch 同一 ID：force 不绕过 is_known
    rec2 = store.add_new(
        msg_id="xhs:note1",
        platform="xhs",
        content_type=ContentType.TEXT,
        pubdate=old_pubdate,
        title="笔记",
        author="作者",
        force=True,
    )
    assert rec2 is None
```

跑：`uv run pytest -x tests/test_message_store.py -k "force"` → **应 TypeError 失败**（`add_new() got an unexpected keyword argument 'force'`，红）。

### 3.3 绿 — 实现

修改 `shared/message_store.py` 的 `add_new` 方法签名和文档：

```python
def add_new(
    self,
    msg_id: str,
    platform: str,
    content_type: ContentType,
    pubdate: int,
    title: str,
    author: str,
    subscription_ref: str = "",
    *,
    xsec_token: str = "",
    body: str = "",
    force: bool = False,
) -> MessageRecord | None:
    """添加新消息。

    内部做去重和时间窗口检查。如果消息已在 store 中或（``force=False`` 时）超出
    时间窗口，返回 None。

    ``xsec_token`` 和 ``body`` 为 keyword-only（issue #89）：
    - ``xsec_token``：xhs 专属鉴权 token，detector 透传，download handler 读回
    - ``body``：内容正文（xhs detector 阶段预填 NoteInfo.desc）

    ``force`` keyword-only（issue #101）：
    - True 时绕过 ``is_in_window`` 时间窗口检查，按需 fetch 入口专用
    - ``is_known`` 去重检查**不变**（force 不绕过去重）
    - cron detector 调用方默认 False，行为完全不变（45 个现有调用点零影响）

    Returns:
        新创建的 MessageRecord，或 None（已存在 / 超期且未 force）
    """
    if self.is_known(msg_id):
        return None
    if not force and not self.is_in_window(pubdate):
        return None
    # ... 以下不变
```

跑：`uv run pytest -x tests/test_message_store.py -k "force"` → **绿**。
跑全量回归：`uv run pytest -x tests/test_message_store.py` → **绿**（确认 45 个调用点不破坏）。

### 3.4 验证

```bash
uv run pytest -x tests/test_message_store.py
uv run ruff check shared/message_store.py
uv run pyright
```

---

## Task 4: B 站 `fetch_video_by_id`（`platforms/bilibili/monitor.py`）

### 4.1 文件路径

- 实现: `platforms/bilibili/monitor.py`（追加 `fetch_video_by_id` 函数）
- 实现: `platforms/bilibili/handlers.py`（追加注册装饰器）
- 测试: `tests/test_bilibili_fetch_by_id.py`（新建）

### 4.2 红 — 先写测试

```python
# tests/test_bilibili_fetch_by_id.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.config import Config
from shared.protocols import ContentType, FetchedMessage


@pytest.mark.asyncio
async def test_fetch_video_by_id_success(tmp_path):
    """mock ``bilibili_api.video.Video.get_info`` → 正确解析为 FetchedMessage。"""
    from platforms.bilibili.monitor import fetch_video_by_id

    fake_info = {
        "bvid": "BV1xxTest",
        "title": "测试视频标题",
        "pubdate": 1700000000,
        "desc": "视频简介",
        "pic": "//example.com/cover.jpg",
        "owner": {"name": "UP主名字", "mid": 12345},
    }

    # bili get_credential(config) 不抛异常（无凭证时返回未登录 Credential），
    # 但为了测试稳定（不依赖网络/库版本），mock 它返回 None（fetcher 不使用返回值）。
    # 同时显式设 cookie，统一三平台测试的"cookie 来源明确"约定。
    with patch("platforms.bilibili.monitor.bilibili_api") as mock_api:
        mock_video_cls = mock_api.video.Video
        mock_instance = mock_video_cls.return_value
        mock_instance.get_info = AsyncMock(return_value=fake_info)
        with patch("platforms.bilibili.auth.get_credential", return_value=None):
            from shared.config import Config as Cfg
            config = Cfg()
            # 显式 cookie 来源（P1-1：三平台测试统一约定，避免默认空 cookie 误触失败）
            config.bilibili.auth.sessdata = "fake_sessdata"
            config.bilibili.auth.bili_jct = "fake_bili_jct"

            fm = await fetch_video_by_id("BV1xxTest", config)

    assert fm is not None
    assert isinstance(fm, FetchedMessage)
    assert fm.msg_id == "bili:BV1xxTest"
    assert fm.platform == "bili"
    assert fm.content_type is ContentType.VIDEO  # bili 一律 VIDEO
    assert fm.title == "测试视频标题"
    assert fm.author == "UP主名字"
    assert fm.pubdate == 1700000000
    assert fm.body == "视频简介"


@pytest.mark.asyncio
async def test_fetch_video_by_id_api_failure_returns_none(tmp_path):
    """API 抛异常 → 返回 None（调用方可重试）。"""
    from platforms.bilibili.monitor import fetch_video_by_id

    with patch("platforms.bilibili.monitor.bilibili_api") as mock_api:
        mock_video_cls = mock_api.video.Video
        mock_instance = mock_video_cls.return_value
        mock_instance.get_info = AsyncMock(side_effect=Exception("network error"))
        with patch("platforms.bilibili.auth.get_credential", return_value=None):
            from shared.config import Config as Cfg
            config = Cfg()
            config.bilibili.auth.sessdata = "fake_sessdata"
            config.bilibili.auth.bili_jct = "fake_bili_jct"

            fm = await fetch_video_by_id("BV1xxMissing", config)

    assert fm is None
```

注：若测试需更复杂的 Config fixture，参考 `tests/conftest.py` 现有 fixture 复用。

跑：`uv run pytest -x tests/test_bilibili_fetch_by_id.py` → **应 ImportError 失败**（红）。

### 4.3 绿 — 实现

**1) `platforms/bilibili/monitor.py`** 末尾追加：

```python
async def fetch_video_by_id(
    bvid: str,
    config: Config,
) -> FetchedMessage | None:
    """按 BVID 抓取单条 B 站视频元数据（issue #101）。

    基于 ``bilibili_api.video.Video(bvid).get_info()``，不依赖订阅列表。

    Args:
        bvid: 视频 BV 号（不带 "bili:" 前缀）
        config: 全局配置（用于取 credential）

    Returns:
        ``FetchedMessage``（``content_type=VIDEO``，B 站一律走完整 5 阶段）；
        抓取失败/视频不存在 → None（调用方可重试）。

    Raises:
        无 —— 所有异常内部捕获并 log，对外只返回 None。
        （B 站目前无明确的"永久失败"信号，token/credential 缺失由
        ``get_credential`` 在 ``auth.py`` 层抛 ``AuthError``，不是这里职责。）
    """
    from bilibili_api import video

    from platforms.bilibili.auth import get_credential
    from shared.protocols import ContentType, FetchedMessage

    credential = get_credential(config)
    try:
        v = video.Video(bvid=bvid, credential=credential)
        info = await v.get_info()
    except Exception as e:
        logger.error("按 BVID 抓取失败 (%s): %s", bvid, e)
        return None

    if not isinstance(info, dict) or not info.get("bvid"):
        logger.warning("BVID %s 返回数据为空或格式异常", bvid)
        return None

    owner = info.get("owner", {}) if isinstance(info.get("owner"), dict) else {}
    return FetchedMessage(
        msg_id=f"bili:{info['bvid']}",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=int(info.get("pubdate", 0) or 0),
        title=info.get("title", "") or "",
        author=owner.get("name", "") or "",
        body=info.get("desc", "") or "",
    )
```

并在文件顶部 import 补充 `FetchedMessage`：

```python
from shared.protocols import FetchedMessage, VideoInfo
```

**2) `platforms/bilibili/handlers.py`** 末尾追加 fetcher 注册：

```python
# ═══════════════════════════════════════════════════════════
# Phase: FETCH（按需抓取，issue #101）
# ═══════════════════════════════════════════════════════════


@PipelineEngine.register_fetcher("bili")
async def bili_fetch_by_id(msg_id: str, config: Config) -> FetchedMessage | None:
    """B 站 fetcher 入口：剥离 "bili:" 前缀，调 ``fetch_video_by_id``。"""
    from platforms.bilibili.monitor import fetch_video_by_id

    bvid = msg_id.removeprefix("bili:")
    return await fetch_video_by_id(bvid, config)
```

（import `FetchedMessage` / `Config` 已在 handlers.py 顶部存在则不重复。）

跑：`uv run pytest -x tests/test_bilibili_fetch_by_id.py` → **绿**。

### 4.4 验证

```bash
uv run pytest -x tests/test_bilibili_fetch_by_id.py
uv run ruff check platforms/bilibili/monitor.py platforms/bilibili/handlers.py
uv run pyright
```

---

## Task 5: 小红书 `fetch_note_by_id`（`platforms/xiaohongshu/monitor.py`）

### 5.1 文件路径

- 实现: `platforms/xiaohongshu/monitor.py`（追加 `fetch_note_by_id`）
- 实现: `platforms/xiaohongshu/handlers.py`（追加 fetcher 注册）
- 测试: `tests/test_xhs_fetch_by_id.py`（新建）

### 5.2 红 — 先写测试

```python
# tests/test_xhs_fetch_by_id.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.exceptions import DataError, PermanentFetchError
from shared.protocols import ContentType, FetchedMessage


@pytest.mark.asyncio
async def test_fetch_note_by_id_text_note_success():
    """normal 笔记（有 desc）→ FetchedMessage(TEXT)。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    fake_note_card = {
        "note_id": "note_abc",
        "type": "normal",
        "display_title": "测试笔记标题",
        "desc": "笔记正文内容",
        "user": {"nickname": "作者昵称", "userid": "u_1"},
        "xsec_token": "",
        "last_update_time": 1700000000,
    }

    # mock 风格与现有 tests/test_xhs_monitor.py:33 一致：
    # AsyncXhsClient 实际库无 __aenter__/__aexit__（仅 async def close，见
    # async_xhs_wrapper.py:345），实现是裸 client + try/finally: await client.close()
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(return_value=fake_note_card)
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            fm = await fetch_note_by_id("note_abc", config)

    assert fm is not None
    assert isinstance(fm, FetchedMessage)
    assert fm.msg_id == "xhs:note_abc"
    assert fm.platform == "xhs"
    assert fm.content_type is ContentType.TEXT  # type=normal
    assert fm.title == "测试笔记标题"
    assert fm.body == "笔记正文内容"
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_note_by_id_video_note_success():
    """video 笔记 → FetchedMessage(VIDEO)。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    fake_note_card = {
        "note_id": "note_v",
        "type": "video",
        "display_title": "视频笔记",
        "desc": "视频简介",
        "video": {"media": {"stream": {"h264": [{"master_url": "http://x"}]}}},
        "user": {"nickname": "UP", "userid": "u_2"},
    }

    # 与 test_fetch_note_by_id_text_note_success 同款 mock（裸 client）
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(return_value=fake_note_card)
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            fm = await fetch_note_by_id("note_v", config)

    assert fm is not None
    assert fm.content_type is ContentType.VIDEO


@pytest.mark.asyncio
async def test_fetch_note_by_id_data_error_raises_permanent():
    """``DataError``（server 拒绝/-100，token 缺失等）→ PermanentFetchError。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    # 裸 client mock（get_note_by_id 抛 DataError）
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(side_effect=DataError("server rejected"))
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            with pytest.raises(PermanentFetchError):
                await fetch_note_by_id("note_missing", config)


@pytest.mark.asyncio
async def test_fetch_note_by_id_empty_body_raises_permanent():
    """拿到 note_card 但 desc/image_list/video 全空 → PermanentFetchError。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    fake_note_card = {
        "note_id": "note_empty",
        "type": "normal",
        "display_title": "",
        "desc": "",
        "image_list": [],
        "user": {"nickname": "X", "userid": "u_3"},
    }

    # 裸 client mock（拿到 note_card 但正文全空）
    mock_client = MagicMock()
    mock_client.get_note_by_id = AsyncMock(return_value=fake_note_card)
    mock_client.close = AsyncMock()

    with patch(
        "platforms.xiaohongshu.monitor.AsyncXhsClient", return_value=mock_client,
    ):
        with patch("platforms.xiaohongshu.auth.get_xhs_cookie", return_value="fake_cookie"):
            from shared.config import Config
            config = Config()
            config.xiaohongshu.auth.cookie = "fake_cookie"

            with pytest.raises(PermanentFetchError):
                await fetch_note_by_id("note_empty", config)
```

跑：`uv run pytest -x tests/test_xhs_fetch_by_id.py` → **红**（ImportError）。

### 5.3 绿 — 实现

**1) `platforms/xiaohongshu/monitor.py`** 末尾追加：

```python
async def fetch_note_by_id(
    note_id: str,
    config: Config,
) -> FetchedMessage | None:
    """按 note_id 抓取单条小红书笔记元数据（issue #101）。

    调 ``AsyncXhsClient.get_note_by_id(note_id, xsec_token="", xsec_source="pc_feed")``。
    **xsec_token 缺失是主要失败原因**：``pc_feed`` 链路对外部仅给 note_id 的场景
    可能拿不到正文。

    失败信号（spec §1）：
    - ``DataError`` 异常 → 抛 ``PermanentFetchError``（server 拒绝，token 缺失等）
    - 拿到 ``note_card`` 但 ``desc`` / ``image_list`` / ``video`` 全空
      → 抛 ``PermanentFetchError``（"xhs: 笔记正文为空，可能 xsec_token 缺失"）

    Args:
        note_id: 笔记 ID（不带 "xhs:" 前缀）
        config: 全局配置（用于取 cookie）

    Returns:
        ``FetchedMessage``；``content_type`` 按 ``note_card.type == "video"`` 判断。

    Raises:
        PermanentFetchError: 永久失败（见上）。
    """
    from platforms.xiaohongshu.auth import get_xhs_cookie
    from shared.exceptions import DataError, PermanentFetchError
    from shared.protocols import ContentType, FetchedMessage

    cookie = get_xhs_cookie(config)
    if not cookie:
        raise PermanentFetchError("xhs: cookie 缺失")

    client = AsyncXhsClient(cookie=cookie)
    try:
        note_card = await client.get_note_by_id(
            note_id, xsec_token="", xsec_source="pc_feed",
        )
    except DataError as e:
        raise PermanentFetchError(f"xhs: server 拒绝（可能 xsec_token 缺失）: {e}") from e
    finally:
        # 关闭语义参考现有 monitor.py:130 的 await client.close() 用法
        # （AsyncXhsClient 无 __aenter__/__aexit__，仅 async def close，见
        # async_xhs_wrapper.py:345）
        try:
            await client.close()
        except Exception:
            pass

    if not isinstance(note_card, dict) or not note_card.get("note_id"):
        raise PermanentFetchError(f"xhs: note_card 为空或格式异常 (note_id={note_id})")

    # 正文为空检测（spec §1 xhs 失败信号）
    desc = note_card.get("desc", "") or ""
    image_list = note_card.get("image_list", [])
    video = note_card.get("video")
    has_video = isinstance(video, dict) and bool(video)
    has_images = isinstance(image_list, list) and len(image_list) > 0
    if not desc and not has_video and not has_images:
        raise PermanentFetchError("xhs: 笔记正文为空，可能 xsec_token 缺失")

    note_type = note_card.get("type", "normal")
    is_video = note_type == "video" or has_video
    title = note_card.get("display_title", "") or note_card.get("title", "") or ""
    user_info = note_card.get("user", {}) if isinstance(note_card.get("user"), dict) else {}
    author = user_info.get("nickname", "") or ""

    # pubdate 优先级与 _parse_note_data 一致
    pubdate = (
        note_card.get("last_update_time", 0)
        or note_card.get("time", 0)
        or note_card.get("create_time", 0)
        or note_card.get("timestamp", 0)
    )
    if not pubdate and len(note_id) >= 8:
        try:
            pubdate = int(note_id[:8], 16)
        except (ValueError, TypeError):
            pubdate = 0
    try:
        pubdate = int(pubdate) if pubdate else 0
    except (ValueError, TypeError):
        pubdate = 0

    return FetchedMessage(
        msg_id=f"xhs:{note_card.get('note_id', note_id)}",
        platform="xhs",
        content_type=ContentType.VIDEO if is_video else ContentType.TEXT,
        pubdate=pubdate,
        title=title,
        author=author,
        xsec_token=note_card.get("xsec_token", "") or note_card.get("xsec_token_str", "") or "",
        body=desc,
    )
```

注：`AsyncXhsClient.close()` 用法参考现有 `monitor.py:130` 的 `await client.close()`（裸 client + try/finally，**非** `async with`，因为该库无 `__aenter__/__aexit__`，仅 `async def close`，见 `async_xhs_wrapper.py:345`）。

**2) `platforms/xiaohongshu/handlers.py`** 末尾追加：

```python
# ═══════════════════════════════════════════════════════════
# Phase: FETCH（按需抓取，issue #101）
# ═══════════════════════════════════════════════════════════


@PipelineEngine.register_fetcher("xhs")
async def xhs_fetch_by_id(msg_id: str, config: Config) -> FetchedMessage | None:
    """xhs fetcher 入口：剥离 "xhs:" 前缀，调 ``fetch_note_by_id``。"""
    from platforms.xiaohongshu.monitor import fetch_note_by_id

    note_id = msg_id.removeprefix("xhs:")
    return await fetch_note_by_id(note_id, config)
```

跑：`uv run pytest -x tests/test_xhs_fetch_by_id.py` → **绿**。

### 5.4 验证

```bash
uv run pytest -x tests/test_xhs_fetch_by_id.py
uv run ruff check platforms/xiaohongshu/monitor.py platforms/xiaohongshu/handlers.py
uv run pyright
```

---

## Task 6: 微博 `fetch_post_by_id`（新建 `platforms/weibo/monitor.py`）

### 6.1 文件路径

- 实现: `platforms/weibo/monitor.py`（**新建**，weibo 目前无此文件）
- 实现: `platforms/weibo/handlers.py`（追加 fetcher 注册）
- 测试: `tests/test_weibo_fetch_by_id.py`（新建）

### 6.2 红 — 先写测试

```python
# tests/test_weibo_fetch_by_id.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.protocols import ContentType, FetchedMessage


@pytest.mark.asyncio
async def test_fetch_post_by_id_text_post_success():
    """无视频 → FetchedMessage(TEXT)。"""
    from platforms.weibo.monitor import fetch_post_by_id

    fake_detail = {
        "id": 12345,
        "text": "<a>作者</a>纯文字微博内容",
        "text_raw": "纯文字微博内容",
        "user": {"screen_name": "作者名"},
        "created_at": "Tue Jun 11 10:00:00 +0800 2026",
        "page_info": {"type": "text"},
    }

    with patch("platforms.weibo.monitor.fetch_post_detail", new=AsyncMock(return_value=fake_detail)):
        from shared.config import Config
        config = Config()
        config.weibo.auth.cookie = "fake_cookie"

        fm = await fetch_post_by_id("12345", config)

    assert fm is not None
    assert isinstance(fm, FetchedMessage)
    assert fm.msg_id == "weibo:12345"
    assert fm.platform == "weibo"
    assert fm.content_type is ContentType.TEXT
    assert fm.author == "作者名"


@pytest.mark.asyncio
async def test_fetch_post_by_id_video_post_success():
    """含视频 page_info → FetchedMessage(VIDEO)。"""
    from platforms.weibo.monitor import fetch_post_by_id

    fake_detail = {
        "id": 67890,
        "text": "<a>UP</a>视频微博",
        "text_raw": "视频微博",
        "user": {"screen_name": "UP主"},
        "created_at": "Tue Jun 11 10:00:00 +0800 2026",
        "page_info": {
            "type": "video",
            "media_info": {"stream_url": "http://example.com/v.mp4"},
        },
    }

    with patch("platforms.weibo.monitor.fetch_post_detail", new=AsyncMock(return_value=fake_detail)):
        from shared.config import Config
        config = Config()
        config.weibo.auth.cookie = "fake_cookie"

        fm = await fetch_post_by_id("67890", config)

    assert fm is not None
    assert fm.content_type is ContentType.VIDEO


@pytest.mark.asyncio
async def test_fetch_post_by_id_detail_empty_returns_none():
    """``fetch_post_detail`` 返回 {} → None。"""
    from platforms.weibo.monitor import fetch_post_by_id

    with patch("platforms.weibo.monitor.fetch_post_detail", new=AsyncMock(return_value={})):
        from shared.config import Config
        config = Config()
        config.weibo.auth.cookie = "fake_cookie"
        fm = await fetch_post_by_id("000", config)

    assert fm is None
```

跑：`uv run pytest -x tests/test_weibo_fetch_by_id.py` → **红**（ImportError，文件不存在）。

### 6.3 绿 — 实现

**1) 新建 `platforms/weibo/monitor.py`**：

```python
"""微博监控模块 — 按需抓取单条 post（issue #101）。

weibo 的 detector 逻辑在 ``handlers.py::weibo_detector`` 内联（基于
``fetch_user_posts``），本模块仅承载按 ID 单条抓取入口 ``fetch_post_by_id``，
对称 bili/xhs 的 ``fetch_video_by_id`` / ``fetch_note_by_id``。
"""

from __future__ import annotations

# pyright: basic
import logging

from shared.config import Config
from shared.protocols import FetchedMessage

logger = logging.getLogger("trawler.weibo.monitor")


async def fetch_post_by_id(
    post_id: str,
    config: Config,
) -> FetchedMessage | None:
    """按 post_id 抓取单条微博元数据（issue #101）。

    包装 ``api.fetch_post_detail``（download handler 已用同一 API 反查 video_urls）。

    Args:
        post_id: 微博 post ID（不带 "weibo:" 前缀）
        config: 全局配置（取 ``config.weibo.auth.cookie``）

    Returns:
        ``FetchedMessage``；``content_type`` 按 ``page_info.type == "video"``
        或 ``_extract_video_urls(page_info)`` 非空判断。
        ``fetch_post_detail`` 返回空 dict → None（可能 post 不存在或网络问题）。

    Raises:
        无 —— 失败信号通过 None 表达，调用方可重试。
    """
    from platforms.weibo.api import (
        _clean_html,
        _extract_video_urls,
        _parse_weibo_time,
        fetch_post_detail,
    )
    from shared.protocols import ContentType

    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.warning("weibo cookie 缺失，无法 fetch (post_id=%s)", post_id)
        return None

    detail = await fetch_post_detail(cookie, post_id)
    if not detail:
        logger.info("weibo fetch_post_detail 返回空 (post_id=%s)", post_id)
        return None

    # 文本与作者
    text_raw = detail.get("text_raw", "") or detail.get("text", "") or ""
    clean_text = _clean_html(text_raw)
    user_info = detail.get("user", {}) if isinstance(detail.get("user"), dict) else {}
    author = user_info.get("screen_name", "") or ""

    # content_type 判断（与 handlers.py::weibo_detector 一致）
    page_info = detail.get("page_info", {}) if isinstance(detail.get("page_info"), dict) else {}
    video_urls = _extract_video_urls(page_info)
    content_type = ContentType.VIDEO if video_urls else ContentType.TEXT

    # pubdate
    pubdate = _parse_weibo_time(detail.get("created_at", ""))

    # title 截断到 50 字符预览；body 留空 —— 长文由 download 阶段统一拉
    # （handlers.py:89-98 已实现，fetch 阶段再拉会浪费配额，P2-1 修复）
    title = clean_text[:50] if clean_text else post_id

    return FetchedMessage(
        msg_id=f"weibo:{post_id}",
        platform="weibo",
        content_type=content_type,
        pubdate=pubdate,
        title=title,
        author=author,
        body="",
    )
```

**2) `platforms/weibo/handlers.py`** 末尾追加：

```python
# ═══════════════════════════════════════════════════════════
# Phase: FETCH（按需抓取，issue #101）
# ═══════════════════════════════════════════════════════════


@PipelineEngine.register_fetcher("weibo")
async def weibo_fetch_by_id(msg_id: str, config: Config) -> FetchedMessage | None:
    """weibo fetcher 入口：剥离 "weibo:" 前缀，调 ``fetch_post_by_id``。"""
    from platforms.weibo.monitor import fetch_post_by_id

    post_id = msg_id.removeprefix("weibo:")
    return await fetch_post_by_id(post_id, config)
```

跑：`uv run pytest -x tests/test_weibo_fetch_by_id.py` → **绿**。

注：weibo `monitor.py` 新建后，更新 `__init__.py` docstring（如需要）。检查 `_clean_html` / `_parse_weibo_time` 是否在 `api.py` 中为 module-level 可导入（grep 确认）。若 `_clean_html` 是私有，照现有 download handler 用法（`from platforms.weibo.api import _fetch_long_text`）。

### 6.4 验证

```bash
uv run pytest -x tests/test_weibo_fetch_by_id.py
uv run ruff check platforms/weibo/monitor.py platforms/weibo/handlers.py
uv run pyright
```

---

## Task 7: 引擎 `register_fetcher` + `_fetchers` + `run_fetch_and_process`（`core/engine.py`）

### 7.1 文件路径

- 实现: `core/engine.py`
- 测试: `tests/test_engine_fetch_and_process.py`（新建）

### 7.2 红 — 先写测试

> **pytest-asyncio 配置说明**（P2-2）：`pyproject.toml` 已配置
> `[tool.pytest.ini_options] asyncio_mode = "auto"`（行 65-66，已查证），
> async 测试函数**无需** `@pytest.mark.asyncio` 装饰器即可运行。
> 但项目现有 `tests/test_engine.py`（行 72 等）**惯例**带 `@pytest.mark.asyncio`
> 装饰器，本测试文件跟随该惯例，每个 async 测试前加 `@pytest.mark.asyncio`，
> 与现有风格保持一致（避免 lint/review 风格分歧）。

```python
# tests/test_engine_fetch_and_process.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.exceptions import PermanentFetchError
from shared.protocols import ContentType, FetchedMessage, Phase


@pytest.mark.asyncio
async def test_platform_from_msg_id():
    """前缀路由正确。"""
    from core.engine import PipelineEngine

    assert PipelineEngine._platform_from_msg_id("bili:BV1xx") == "bili"
    assert PipelineEngine._platform_from_msg_id("xhs:note1") == "xhs"
    assert PipelineEngine._platform_from_msg_id("weibo:123") == "weibo"
    assert PipelineEngine._platform_from_msg_id("unknown:xx") is None
    assert PipelineEngine._platform_from_msg_id("no_prefix") is None


@pytest.mark.asyncio
async def test_run_fetch_and_process_existing_message_skips_fetch(tmp_path):
    """store 已存在的消息 → 不调 fetcher，直接走 process。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    store.add_new(
        msg_id="bili:BV_existing",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=1700000000,
        title="已存在",
        author="UP",
        force=True,
    )

    config = Config()
    # mock fetcher 确保不被调用
    PipelineEngine._fetchers["bili"] = AsyncMock(side_effect=AssertionError("不该调 fetcher"))
    # mock _safe_process_message 避免真跑流水线
    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["bili:BV_existing"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 1


@pytest.mark.asyncio
async def test_run_fetch_and_process_new_message_calls_fetcher(tmp_path):
    """store 不存在的消息 → 调 fetcher → add_new(force=True) → process。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    fake_fm = FetchedMessage(
        msg_id="bili:BV_new",
        platform="bili",
        content_type=ContentType.VIDEO,
        pubdate=1700000000,
        title="新视频",
        author="UP",
    )
    PipelineEngine._fetchers["bili"] = AsyncMock(return_value=fake_fm)

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        fetched = await PipelineEngine.run_fetch_and_process(
            msg_ids=["bili:BV_new"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 1
        # 入库成功
        rec = store.get_message("bili:BV_new")
        assert rec is not None
        assert rec.title == "新视频"
        # 返回值是实际抓取入库数（P0-2 修复：run_fetch_and_process 返回 int）
        assert fetched == 1


@pytest.mark.asyncio
async def test_run_fetch_and_process_permanent_error_skips_no_record(tmp_path):
    """fetcher 抛 PermanentFetchError → log + skip，不创建 record。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    PipelineEngine._fetchers["xhs"] = AsyncMock(
        side_effect=PermanentFetchError("xhs: xsec_token 缺失"),
    )

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["xhs:note_fail"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 0  # 未进入处理
        assert store.get_message("xhs:note_fail") is None  # 未入库


@pytest.mark.asyncio
async def test_run_fetch_and_process_fetcher_returns_none_skips(tmp_path):
    """fetcher 返回 None → log + skip，不创建 record。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    PipelineEngine._fetchers["bili"] = AsyncMock(return_value=None)

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["bili:BV_none"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 0
        assert store.get_message("bili:BV_none") is None


@pytest.mark.asyncio
async def test_run_fetch_and_process_unknown_prefix_skips(tmp_path):
    """未知前缀 → log warning + skip。"""
    from core.engine import PipelineEngine
    from shared.config import Config
    from shared.message_store import MessageStore

    store = MessageStore(str(tmp_path))
    config = Config()

    with patch.object(PipelineEngine, "_safe_process_message", new=AsyncMock()) as mock_proc:
        await PipelineEngine.run_fetch_and_process(
            msg_ids=["unknown:xx"],
            skip_push=False,
            config=config,
            store=store,
        )
        assert mock_proc.call_count == 0
```

跑：`uv run pytest -x tests/test_engine_fetch_and_process.py` → **红**（`_platform_from_msg_id` / `run_fetch_and_process` 不存在）。

### 7.3 绿 — 实现

在 `core/engine.py` 修改如下：

**1) 类变量区**（在 `_detectors` 之后追加）：

```python
    _detectors: dict[str, Callable[..., Awaitable[None]]] = {}
    # issue #101: 按需 fetcher 注册表（对称 _detectors）
    _fetchers: dict[str, Callable[[str, Config], Awaitable[FetchedMessage | None]]] = {}
```

并在顶部 import `FetchedMessage`：

```python
from shared.protocols import (
    Config,  # 如已存在不重复
    FetchedMessage,
    # ... 其他
)
```

**2) 注册装饰器**（在 `register_detector` 之后追加）：

```python
    @classmethod
    def register_fetcher(cls, platform: str) -> Callable[..., Any]:
        """装饰器：注册某平台的 fetcher 函数（issue #101，对称 ``register_detector``）。

        Usage::

            @PipelineEngine.register_fetcher("bili")
            async def bili_fetch_by_id(msg_id: str, config: Config) -> FetchedMessage | None:
                ...
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            cls._fetchers[platform] = func
            return func

        return decorator
```

**3) msg_id 前缀路由**（在 `run_specific_messages` 之后追加，作为 classmethod）：

```python
    @staticmethod
    def _platform_from_msg_id(msg_id: str) -> str | None:
        """根据 msg_id 前缀判断平台（issue #101）。

        Returns:
            "bili" / "xhs" / "weibo"；未知前缀返回 None。
        """
        if msg_id.startswith("bili:"):
            return "bili"
        if msg_id.startswith("xhs:"):
            return "xhs"
        if msg_id.startswith("weibo:"):
            return "weibo"
        return None
```

**4) `run_fetch_and_process`**（在 `run_specific_messages` 之后追加）：

```python
    @classmethod
    async def run_fetch_and_process(
        cls,
        msg_ids: list[str],
        skip_push: bool,
        config: Config,
        store: MessageStore,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> int:
        """按 ID 抓取并处理（issue #101，不依赖订阅 / 已入库记录）。

        对每个 msg_id:
        1. 已存在 store → 直接 ``_safe_process_message``（走当前 phase 续跑）
        2. 不存在 → 调对应平台 ``fetch_by_id``:
           - PermanentFetchError → log error + 跳过（不创建 record）
           - 返回 None（抓取失败但可重试） → log warning + 跳过
           - 成功 → ``store.add_new(force=True)`` → ``_safe_process_message``

        Returns:
            实际成功抓取并入库的 ID 数（供 API ``fetch_count`` 字段使用，
            区别于 ``len(msg_ids)`` —— 永久失败 / 返回 None / 未知前缀
            / 未注册 fetcher 的不计入；已存在 store 的不计入，因为没走抓取）。

        与 ``run_specific_messages`` 区别:
        - 跑 fetch（detector 的按 ID 单条等价物）
        - 不调 cleanup（同 D6 决策，避免误删历史）
        - 不调 ``reset_specific``（新消息从 DISCOVERED 开始；已存在消息走当前 phase）

        ⚠️ 并发安全：与 ``run_specific_messages`` 相同 —— 不持有文件锁，
        复用 ``state.check_running`` 单锁（在 API/Web 入口占锁）。
        """
        from shared.exceptions import PermanentFetchError

        if log_callback:
            log_callback("log", f"▶ 按需抓取处理 {len(msg_ids)} 条消息")

        # 延迟导入所有平台 handler 模块（触发装饰器注册，含 fetcher）
        for module_path in cls._HANDLER_MODULES.values():
            importlib.import_module(module_path)

        fetched_count = 0
        for msg_id in msg_ids:
            msg = store.get_message(msg_id)
            if msg is not None:
                # 已存在 → 续跑（从当前 phase）
                logger.info("▶ %s 已存在，直接处理（phase=%s）", msg_id, msg.phase.name)
                if log_callback:
                    log_callback("log", f"▶ {msg_id} 已存在，续跑处理")
                setattr(msg, "_skip_push", skip_push)
                await cls._safe_process_message(msg, config, store)
                continue

            # 不存在 → fetch
            platform = cls._platform_from_msg_id(msg_id)
            if platform is None:
                logger.warning("⏭ 跳过 %s：未知前缀", msg_id)
                if log_callback:
                    log_callback("log", f"⏭ {msg_id} 未知前缀，跳过")
                continue

            fetcher = cls._fetchers.get(platform)
            if fetcher is None:
                logger.error("✗ %s 平台未注册 fetcher", platform)
                if log_callback:
                    log_callback("log", f"✗ {platform} 未注册 fetcher")
                continue

            try:
                fm = await fetcher(msg_id, config)
            except PermanentFetchError as e:
                logger.error("✗ %s 抓取永久失败: %s", msg_id, e)
                if log_callback:
                    log_callback("log", f"✗ {msg_id} 抓取失败: {e}")
                continue
            except Exception as e:
                # 未预期异常（网络、解析 bug 等）→ log + skip，不创建 record
                logger.exception("✗ %s 抓取异常", msg_id)
                if log_callback:
                    log_callback("log", f"✗ {msg_id} 抓取异常: {e}")
                continue

            if fm is None:
                logger.warning("⏭ %s 抓取返回空（可重试）", msg_id)
                if log_callback:
                    log_callback("log", f"⏭ {msg_id} 抓取返回空")
                continue

            # 入库（force=True 突破时间窗；is_known 去重仍生效，理论不会重复）
            rec = store.add_new(
                msg_id=fm.msg_id,
                platform=fm.platform,
                content_type=fm.content_type,
                pubdate=fm.pubdate,
                title=fm.title,
                author=fm.author,
                xsec_token=fm.xsec_token,
                body=fm.body,
                force=True,
            )
            if rec is None:
                # is_known 命中（理论不该发生，step 1 已检查）；fallback 取已有记录
                rec = store.get_message(fm.msg_id)
                if rec is None:
                    logger.error("✗ %s 入库失败且无已有记录", fm.msg_id)
                    continue

            fetched_count += 1
            setattr(rec, "_skip_push", skip_push)
            if log_callback:
                log_callback("log", f"▶ {fm.msg_id} 抓取入库，开始处理")
            await cls._safe_process_message(rec, config, store)

        store.save()
        logger.info("✓ 按需处理完成（抓取 %d / 共 %d）", fetched_count, len(msg_ids))
        if log_callback:
            log_callback("done", f"✅ 按需处理完成（抓取 {fetched_count} 条）")
        return fetched_count
```

跑：`uv run pytest -x tests/test_engine_fetch_and_process.py` → **绿**。
全量回归：`uv run pytest -x tests/test_engine*.py` → **绿**。

### 7.4 验证

```bash
uv run pytest -x tests/test_engine_fetch_and_process.py
uv run ruff check core/engine.py
uv run pyright
```

---

## Task 8: CLI `trawler fetch`（`run_check.py`）

### 8.1 文件路径

- 实现: `run_check.py`
- 测试: `tests/test_cli_fetch.py`（新建）

### 8.2 红 — 先写测试

```python
# tests/test_cli_fetch.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from shared.config import Config


def test_fetch_command_invokes_run_fetch_and_process(tmp_path):
    """CLI ``trawler fetch --ids ...`` 调用引擎 ``run_fetch_and_process``。"""
    from run_check import cli

    # 用真实最小 Config（避免 AsyncMock 的 .general.data_dir 返回 Mock 让
    # setup_logging / MessageStore 调用炸 —— P1-3 修复）
    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    runner = CliRunner()
    with patch("run_check.load_config", new=mock_load), \
         patch("run_check.MessageStore"), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        result = runner.invoke(cli, ["fetch", "--ids", "bili:BV1xx,xhs:note1"])

    assert result.exit_code == 0, f"output: {result.output}"
    # run_fetch_and_process 被调用一次，msg_ids 拆分正确
    mock_run.assert_awaited_once()
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["msg_ids"] == ["bili:BV1xx", "xhs:note1"]


def test_fetch_command_invalid_prefix_exits_nonzero(tmp_path):
    """无效前缀 → sys.exit(1)，不调引擎。"""
    from run_check import cli

    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    runner = CliRunner()
    with patch("run_check.load_config", new=mock_load), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        result = runner.invoke(cli, ["fetch", "--ids", "unknown:xx"])

    assert result.exit_code != 0
    assert "无效的 msg_id" in result.output
    mock_run.assert_not_awaited()


def test_fetch_command_skip_push_flag(tmp_path):
    """``--skip-push`` 透传到引擎。"""
    from run_check import cli

    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    runner = CliRunner()
    with patch("run_check.load_config", new=mock_load), \
         patch("run_check.MessageStore"), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        runner.invoke(cli, ["fetch", "--ids", "bili:BV1", "--skip-push"])

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["skip_push"] is True
```

跑：`uv run pytest -x tests/test_cli_fetch.py` → **红**（`fetch` 命令不存在）。

### 8.3 绿 — 实现

在 `run_check.py` 的 `check` 命令之后追加新命令：

```python
# ═══════════════════════════════════════════════════════════
# 命令: fetch（按需消息处理，issue #101）
# ═══════════════════════════════════════════════════════════


@cli.command()
@click.option(
    "--ids",
    "ids",
    required=True,
    help="逗号分隔的消息 ID，如 bili:BV1xx,xhs:note1,weibo:123",
)
@click.option(
    "--skip-push",
    is_flag=True,
    default=False,
    help="跳过推送通知（默认推送）",
)
@click.option(
    "--config",
    "config_path",
    default="config/config.toml",
    show_default=True,
    help="配置文件路径",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="启用详细日志输出",
)
def fetch(
    ids: str,
    skip_push: bool,
    config_path: str,
    verbose: bool,
) -> None:
    """按指定消息 ID 抓取并处理（不依赖订阅）。

    - 对每个 ID：不存在则抓取入库 + 走完整流水线；已存在则直接处理
    - 默认推送给订阅者（--skip-push 跳过）
    - 突破 24h 时间窗限制，允许任意历史消息

    msg_id 必须带平台前缀（bili:/xhs:/weibo:），无前缀会被拒绝。
    """
    try:
        config = asyncio.run(load_config(config_path))
    except Exception as exc:
        console.print(f"[red]✗ 配置加载失败: {exc}[/]")
        sys.exit(1)

    setup_logging(verbose=verbose, log_dir=config.general.data_dir)
    if verbose:
        console.print("[dim]调试模式已启用[/]")

    # 拆分 + 前缀校验（快速失败）
    msg_ids = [m.strip() for m in ids.split(",") if m.strip()]
    valid_prefixes = {"bili:", "xhs:", "weibo:"}
    invalid = [m for m in msg_ids if not any(m.startswith(p) for p in valid_prefixes)]
    if invalid:
        console.print(
            f"[red]✗[/] 无效的 msg_id（需 bili:/xhs:/weibo: 前缀）: {invalid}"
        )
        sys.exit(1)

    console.print(f"[bold blue]▶[/] 按需抓取处理 {len(msg_ids)} 条消息")
    console.print(f"[dim]{' / '.join(msg_ids)}[/]")

    store = MessageStore(config.general.data_dir)

    try:
        asyncio.run(
            PipelineEngine.run_fetch_and_process(
                msg_ids=msg_ids,
                skip_push=skip_push,
                config=config,
                store=store,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/]")
        sys.exit(130)
    except Exception as exc:
        console.print(f"[red]✗ 运行出错: {exc}[/]")
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    console.print("[green]✓[/] 处理完成")
```

跑：`uv run pytest -x tests/test_cli_fetch.py` → **绿**。

### 8.4 验证

```bash
uv run pytest -x tests/test_cli_fetch.py
uv run ruff check run_check.py
uv run pyright
```

---

## Task 9: API `POST /messages/fetch`（`api/schemas.py` + `api/routes/messages.py`）

### 9.1 文件路径

- 实现: `api/schemas.py`（追加 `FetchRequest` / `FetchResponse`）
- 实现: `api/routes/messages.py`（追加 `fetch_messages` 路由）
- 测试: `tests/test_api_fetch.py`（新建）

### 9.2 红 — 先写测试

```python
# tests/test_api_fetch.py
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """构造 TestClient，bypass token 鉴权。"""
    # 参考 tests/test_api_rerun.py 现有 fixture 模式
    # require_token 实际在 api.auth（非 api.deps，已查证 api/routes/messages.py:28）
    from api.app import create_app

    app = create_app()
    # bypass token check
    monkeypatch.setattr("api.auth.require_token", lambda: lambda: "test")
    return TestClient(app)


def test_fetch_messages_success(client, tmp_path):
    """202 + {"status": "started", "task_id": ...}。fetch_count 为 None（异步未跑完）。"""
    # 用真实最小 Config（避免 AsyncMock 的 .general.data_dir 让 MessageStore 炸，P1-3 同款修复）
    from shared.config import Config
    config = Config()
    config.general.data_dir = str(tmp_path)
    mock_load = AsyncMock(return_value=config)

    with patch("api.routes.messages.load_config", new=mock_load), \
         patch("api.routes.messages.MessageStore"), \
         patch("core.engine.PipelineEngine.run_fetch_and_process", new=AsyncMock()) as mock_run:
        mock_run.return_value = 2  # 引擎返回实际抓取数（202 响应不消费此值，走 SSE done 事件）
        resp = client.post(
            "/messages/fetch",
            json={"msg_ids": ["bili:BV1xx", "xhs:note1"], "skip_push": False},
        )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "started"
    assert data["task_id"] is not None
    # fetch_count 在 202 响应里为 None（抓取异步未跑完，实际数走 SSE "done" 事件）
    assert data["fetch_count"] is None


def test_fetch_messages_empty_ids_returns_422(client):
    """msg_ids 空 → 422。"""
    resp = client.post("/messages/fetch", json={"msg_ids": []})
    assert resp.status_code == 422


def test_fetch_messages_already_running_returns_409(client, monkeypatch):
    """已有 run 在跑 → 409。"""
    with patch("api.routes.messages.load_config", new=AsyncMock()):
        # 模拟 state.check_running = True
        from api.app import create_app

        app = create_app()
        # 直接设置 state
        monkeypatch.setattr("api.app.create_app", lambda: app)
        app.state.check_running = True
        app.state.api_task_id = "existing-task"
        client = TestClient(app)
        resp = client.post("/messages/fetch", json={"msg_ids": ["bili:BV1"]})
    assert resp.status_code == 409
    data = resp.json()
    assert data["status"] == "already_running"
```

跑：`uv run pytest -x tests/test_api_fetch.py` → **红**（路由不存在）。

### 9.3 绿 — 实现

**1) `api/schemas.py`** 追加：

```python
class FetchRequest(BaseModel):
    """``POST /messages/fetch`` 请求体（issue #101）。

    - ``msg_ids`` 必须非空
    - ``skip_push`` 默认 ``False``（与 ``RerunRequest`` 默认 ``True`` 相反，
      按需入口语义是"处理新消息"，应当推送）
    """

    msg_ids: list[str]
    skip_push: bool = False


class FetchResponse(BaseModel):
    """``POST /messages/fetch`` 成功响应（202）。

    ``fetch_count`` 恒为 ``None``：抓取是异步的，202 响应提交时尚未跑完。
    实际抓取数通过 SSE ``done`` 事件推送（见路由 docstring）。
    字段保留是为与 ``RerunResponse`` 对称、未来如需同步模式可填。
    """

    status: str
    task_id: str | None = None
    fetch_count: int | None = None  # 恒 None（异步），保留为对称字段
```

**2) `api/routes/messages.py`** 在 `rerun_messages` 之后追加：

```python
# ═══════════════════════════════════════════════════════════
# POST /messages/fetch（按需抓取处理，issue #101）
# ═══════════════════════════════════════════════════════════


@router.post("/messages/fetch", response_model=FetchResponse, status_code=202)
async def fetch_messages(
    body: FetchRequest,
    request: Request,
    _token_name: str = Depends(require_token),
) -> FetchResponse | JSONResponse:
    """按 ID 抓取并处理（不依赖订阅）。

    走 ``state.check_running`` 单锁（与 ``/messages/rerun`` / ``/check/run``
    完全对称、互斥）。

    - ``msg_ids`` 空 → 422
    - 已有 run 在跑 → 409 ``{"status": "already_running", "task_id": ...}``
    - 成功 → 202 + ``{"status": "started", "task_id": ..., "fetch_count": null}``

    后台 task 调 ``PipelineEngine.run_fetch_and_process``。
    与 ``/messages/rerun`` 区别：不返回 ``reset_count``（无 reset 操作）。
    ``fetch_count`` 在 202 响应里**恒为 null**（抓取是异步的，提交时尚未跑完）；
    引擎 ``run_fetch_and_process`` 返回的实际抓取数通过 ``log_callback`` 的
    "done" 事件以 SSE 推送给前端（``log_callback("done", f"...抓取 {n} 条")``）。

    **设计权衡**：之所以不做"同步抓取再返 202"，是因为 fetch 可能耗时数十秒
    （每条 ID 一次外部 API 调用），HTTP 请求必须立即返回避免超时；实际抓取数
    通过 SSE 异步通知是更合理的契约（与 ``/check/run`` / ``/messages/rerun`` 的
    ``reset_count`` 也通过 SSE 推送的模式一致）。
    """
    if not body.msg_ids:
        raise HTTPException(
            status_code=422, detail="msg_ids 不能为空"
        )

    state = request.app.state
    if state.check_running:
        existing_task_id = getattr(state, "api_task_id", None)
        return JSONResponse(
            status_code=409,
            content={"status": "already_running", "task_id": existing_task_id},
        )

    cfg = await load_config()
    store = MessageStore(cfg.general.data_dir)

    # 占锁 + 初始化 run state（与 /messages/rerun 完全对称）
    task_id = uuid4().hex
    state.check_running = True
    state.check_processed_count = 0
    state.check_started_at = time.time()
    state.log_history.clear()
    state.api_task_id = task_id  # type: ignore[attr-defined]
    cb = make_log_callback(state)

    async def _fetch() -> None:
        try:
            await PipelineEngine.run_fetch_and_process(
                msg_ids=body.msg_ids,
                skip_push=body.skip_push,
                config=cfg,
                store=store,
                log_callback=cb,
            )
        except Exception as exc:
            err_item: dict[str, object] = {
                "type": "error",
                "message": f"按需抓取失败: {exc}",
                "time": time.strftime("%H:%M:%S"),
                "_ts": time.time(),
            }
            state.log_history.append(err_item)
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(err_item)
                except asyncio.QueueFull:
                    pass
        finally:
            state.check_running = False
            state.check_started_at = None
            state.check_task = None
            state.api_task_id = None  # type: ignore[attr-defined]
            for sub in list(state.subscribers):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    state.check_task = asyncio.create_task(_fetch())
    # fetch_count 在 202 响应里为 None（抓取异步，提交时未跑完）；
    # 实际成功抓取数通过 /check/status SSE "done" 事件返回（由引擎 log_callback 推送）。
    return FetchResponse(
        status="started", task_id=task_id, fetch_count=None,
    )
```

**3) `api/routes/messages.py` 顶部 import 显式更新**（P1-2 修复：明确列出已存在 + 新增的 import）：

当前 `api/routes/messages.py` 顶部 import 块（已查证，行 18-40）：

```python
from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from api.auth import require_token
from api.schemas import (
    MessageListResponse,
    MessageOut,
    RerunRequest,
    RerunResponse,
)
from core.engine import PipelineEngine
from run_check import parse_since
from shared.config import load_config
from shared.message_store import MessageStore
from shared.protocols import MessageRecord, Phase
from web.routes.check import make_log_callback
```

**仅一处改动**：把 `FetchRequest, FetchResponse` 加入已有的 `api.schemas` import 块：

```python
from api.schemas import (
    FetchRequest,
    FetchResponse,
    MessageListResponse,
    MessageOut,
    RerunRequest,
    RerunResponse,
)
```

（其他 import 已全部存在 —— `asyncio` / `time` / `uuid4` / `APIRouter` / `Depends` / `HTTPException` / `Request` / `JSONResponse` / `require_token` / `PipelineEngine` / `load_config` / `MessageStore` / `make_log_callback` 实现都已用，无需新增。）

跑：`uv run pytest -x tests/test_api_fetch.py` → **绿**。

### 9.4 验证

```bash
uv run pytest -x tests/test_api_fetch.py
uv run ruff check api/schemas.py api/routes/messages.py
uv run pyright
```

---

## Task 10: 文档与收尾

### 10.1 README

在 `README.md` 的 CLI 命令列表（与 `trawler check` / `trawler subscription` 同位置）追加：

```markdown
### `trawler fetch` — 按需抓取消息

按指定 ID 抓取并处理消息（不依赖订阅），突破 24h 时间窗，允许处理任意历史消息。

```bash
# 抓取并处理（默认推送）
trawler fetch --ids bili:BV1xx,xhs:note1,weibo:123

# 跳过推送
trawler fetch --ids bili:BV1xx --skip-push
```

msg_id 必须带平台前缀：`bili:` / `xhs:` / `weibo:`。
对已存在的消息会直接续跑当前 phase（不重复推送已 PUSHED 消息，重推走 `trawler check --since`）。
```

### 10.2 `platforms/weibo/__init__.py` docstring 检查（P2-4）

项目 `AGENTS.md` 要求新增模块需更新 `__init__.py` docstring。本 plan 新建了
`platforms/weibo/monitor.py`（Task 6），需检查 `platforms/weibo/__init__.py`
（当前内容仅为 `"""微博平台适配层"""`）是否需要补 monitor 模块描述。

**操作**：

```bash
# 查看 weibo __init__.py 与其他平台对比
cat platforms/weibo/__init__.py
cat platforms/bilibili/__init__.py
cat platforms/xiaohongshu/__init__.py
```

若 bili/xhs `__init__.py` 也仅是一行 docstring（已查证 bili 也是单行），则保持
weibo 现状不变（一致性优先，不为单平台破例）。若其他平台 __init__.py 已展开列出
各子模块，则在 weibo __init__.py 补一行提及 monitor，格式与现有平台对齐。

**判定标准**：跟随现有平台 __init__.py 风格，不为本次新功能破例。

### 10.3 全量回归

完成所有 task 后跑：

```bash
uv run pytest -x                  # 全套测试（确认 45 个 add_new 调用点不破坏）
uv run ruff check .               # lint
uv run ruff format .              # format（如有改动）
uv run pyright                    # type check（无参数）
```

### 10.4 手动 smoke test

```bash
# 真实跑一次（需 cookie 配置）
uv run trawler fetch --ids bili:BV1axxxxx --verbose

# API 端到端
curl -X POST http://localhost:8000/messages/fetch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"msg_ids": ["bili:BV1axxxxx"], "skip_push": true}'
```

---

## 风险与回退

| 风险 | 缓解 |
|---|---|
| `add_new(force=True)` 破坏现有调用点 | keyword-only 参数，默认 False，全量 pytest 覆盖 |
| bili `bilibili_api.video.Video` 接口变更 | 失败返回 None，用户可重试；接口以实现时安装的库版本为准 |
| xhs `pc_feed` 链路拿不到正文 | 抛 `PermanentFetchError` 明示，用户知道原因 |
| weibo 长文 `_fetch_long_text` 失败（download 阶段） | body 留空，summary 基于标题 | fetch 阶段不调 `_fetch_long_text`（避免与 download 重复拉，P2-1），download 阶段失败时 handler 已有降级（handlers.py:89-98） |
| `_fetchers` 装饰器注册时机 | 引擎入口 `run_fetch_and_process` 内延迟 `importlib.import_module`，与现有 `_HANDLER_MODULES` 模式一致 |

回退方案：每个 Task 独立 commit，单点失败可 `git revert` 单个 commit。

## 验证清单

- [ ] Task 1-9 全部 TDD 红→绿
- [ ] `uv run pytest -x` 全量绿
- [ ] `uv run ruff check .` 无新增 warning
- [ ] `uv run pyright` 0 error
- [ ] 手动 `trawler fetch` 端到端跑通至少 1 个平台
- [ ] API `POST /messages/fetch` 返回 202 + task_id
- [ ] README 已更新
