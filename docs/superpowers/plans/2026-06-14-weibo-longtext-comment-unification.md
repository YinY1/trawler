# 微博长文扩展 + 评论链统一 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解决微博长文截断 + 统一三平台评论高亮模型和格式化逻辑

**Architecture:** 
- `shared/protocols.py`: 用统一 `CommentHighlight` 替代三个平台各自的评论 dataclass
- `platforms/weibo/comments.py`: 从移动端 hotflow API 切换到 PC 端 buildComments API（cookie 正常），改用统一模型
- `platforms/weibo/api.py`: 长文检测后调用 longtext API 补充完整内容
- `core/formatter.py` (新): 共用评论格式化为 Markdown 的逻辑
- 三平台 handlers 改用统一模型和格式化函数

**Tech Stack:** Python 3.12, aiohttp, dataclasses

---

### Task 1: 统一 CommentHighlight 数据模型

**Files:**
- Modify: `shared/protocols.py:61-136`
- Modify: `platforms/bilibili/comments.py:11`
- Modify: `platforms/weibo/comments.py:15`
- Modify: `platforms/xiaohongshu/comments.py:20`
- Test: `tests/test_weibo_comments.py`

- [ ] **Step 1: 替换 `shared/protocols.py` 中的三个 CommentHighlight 为一个**

将 Bilibili 的 `CommentHighlight`、`WeiboCommentHighlight`、`XhsCommentHighlight` 合并为统一版本：

```python
@dataclass
class CommentHighlight:
    """评论亮点 — 跨平台统一模型"""

    content: str
    user_name: str
    is_author: bool  # 统一字段名（原 Bili: is_up_owner, Weibo/XHS: is_author）
    like_count: int
    # 对话链路（作者回复别人时展示完整上下文）
    reply_to: str = ""  # 被回复的用户名
    parent_content: str = ""  # 被回复的原文
    is_pinned: bool = False  # 是否被置顶
```

删除 `WeiboCommentHighlight` 和 `XhsCommentHighlight` 两个旧 dataclass。

- [ ] **Step 2: 更新 Bilibili 引用**

`platforms/bilibili/comments.py`: 
- 第 11 行 `from shared.protocols import CommentHighlight` 不变
- 所有 `is_up_owner` → `is_author`（共 ~8 处）
- `_build_highlight()` 参数名 `is_up_owner` → `is_author`

- [ ] **Step 3: 更新 Weibo 引用**

`platforms/weibo/comments.py`:
- 第 15 行 `from shared.protocols import WeiboCommentHighlight` → `from shared.protocols import CommentHighlight`
- `_parse_comment()` 返回值类型 `WeiboCommentHighlight` → `CommentHighlight`
- 返回 `CommentHighlight(...)` 替换 `WeiboCommentHighlight(...)`

`platforms/weibo/handlers.py`:
- 第 66 行 `_format_weibo_comment_highlights()` 中 `WeiboCommentHighlight` → `CommentHighlight`
- 条件中的 `is_author` 保持不变（统一模型同名）

- [ ] **Step 4: 更新 XHS 引用**

`platforms/xiaohongshu/comments.py`:
- 第 20 行 `from shared.protocols import XhsCommentHighlight` → `from shared.protocols import CommentHighlight`
- 返回值类型和返回语句中的 `XhsCommentHighlight` → `CommentHighlight`

- [ ] **Step 5: 更新 Weibo 评论测试**

`tests/test_weibo_comments.py`:
- 无需改 import（`fetch_weibo_comment_highlights` 不变，内部类型变了）
- 测试断言中 `result.is_author` 字段名不变
- 确认所有测试通过

- [ ] **Step 6: 运行测试验证当前状态**

```bash
uv run pytest tests/test_weibo_comments.py tests/test_weibo_integration.py -v --tb=short 2>&1 | tail -30
```
Expected: Weibo comment tests pass (12 tests).

---

### Task 2: 微博长文扩展

**Files:**
- Modify: `platforms/weibo/api.py`
- Modify: `shared/protocols.py:111-126` (WeiboPost 加字段)
- Test: Verify manually with real API

- [ ] **Step 1: WeiboPost 加 long_text 字段**

`shared/protocols.py`:
```python
@dataclass
class WeiboPost:
    ...
    is_long_text: bool = False
    long_text: str = ""  # 长文全文（isLongText=True 时填充）
```

- [ ] **Step 2: 添加 longtext API 调用函数**

`platforms/weibo/api.py` 添加：

```python
LONGTEXT_API = "https://weibo.com/ajax/statuses/longtext?id={post_id}"


async def _fetch_long_text(cookie: str, post_id: str) -> str:
    """获取微博长文完整内容。

    Args:
        cookie: Cookie 字符串
        post_id: 帖子 ID

    Returns:
        完整长文文本，失败时返回空字符串
    """
    if not cookie or not post_id:
        return ""

    url = LONGTEXT_API.format(post_id=post_id)
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Referer": "https://weibo.com/",
        "Cookie": cookie,
    }

    session = await get_session()
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
            lt = data.get("data", {})
            # longTextContent_raw 是纯文本版本
            return lt.get("longTextContent_raw", "") or lt.get("longTextContent", "")
    except Exception:
        logger.debug("获取长文失败: %s", post_id)
        return ""
```

- [ ] **Step 3: 在 _parse_pc_post 中填充 is_long_text**

`platforms/weibo/api.py` `_parse_pc_post()` 函数中添加：
```python
# 长文标记
is_long_text = raw.get("isLongText", False)
# 用 text_raw 作为初始 clean_text（无 HTML）
if raw.get("text_raw"):
    clean_text = raw["text_raw"].rstrip("\u200b")
```

注意：需要在 `WeiboPost(...)` 调用中加入 `is_long_text=is_long_text`。

- [ ] **Step 4: 在 fetch_user_posts 中补充长文内容**

`platforms/weibo/api.py` `fetch_user_posts()` 函数中，在解析完成后，对 `is_long_text` 的帖子调用 longtext API 填充 `long_text` 字段：

```python
# 长文补充
if cookie:
    for post in results:
        if post.is_long_text:
            full_text = await _fetch_long_text(cookie, post.post_id)
            if full_text:
                post.long_text = full_text
                post.clean_text = full_text
```

放在返回 `results` 之前。

- [ ] **Step 5: 手动验证长文获取**

```bash
uv run python3 -c "
import asyncio, tomllib
from pathlib import Path
from platforms.weibo.api import fetch_user_posts

async def test():
    cfg = tomllib.loads(Path('config.toml').read_text())
    cookie = cfg['weibo']['auth']['cookie']
    posts = await fetch_user_posts(cookie, '2803301701', max_posts=5)
    for p in posts:
        if p.is_long_text:
            print(f'Post {p.post_id}: long_text={len(p.long_text)} chars')
            print(f'  Preview: {p.long_text[:100]}...')
            print(f'  clean_text length: {len(p.clean_text)}')
        else:
            print(f'Post {p.post_id}: short ({len(p.clean_text)} chars)')
    from shared.http import close_session
    await close_session()

asyncio.run(test())
" 2>&1
```
Expected: Long text posts show expanded content in `long_text` field.

---

### Task 3: 微博评论改用 PC API

**Files:**
- Modify: `platforms/weibo/comments.py`
- Modify: `platforms/weibo/handlers.py:139-150` (评论参数)
- Modify: `platforms/weibo/handlers.py:54-66` (格式化用统一函数)
- Test: `tests/test_weibo_comments.py` (更新 mock)

- [ ] **Step 1: 重写 fetch_weibo_comment_highlights 使用 PC API**

`platforms/weibo/comments.py` 完整替换：

```python
"""微博评论亮点抓取模块 - 使用 PC 端 API"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from rich.console import Console

from shared.config import Config
from shared.constants import MAX_COMMENT_HIGHLIGHTS, WEIBO_REQUEST_TIMEOUT
from shared.http import get_session
from shared.protocols import CommentHighlight

logger = logging.getLogger(__name__)
console = Console()

# PC 端评论 API
COMMENT_API = (
    "https://weibo.com/ajax/statuses/buildComments"
    "?flow=default&id={post_id}&is_show_bulletin=2&key="
)


def _get_default_ua() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )


def _clean_html(text: str) -> str:
    """去除 HTML 标签和 &entity;。"""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", "", text)
    return text.strip()


def _parse_comment(
    comment_data: dict[str, Any],
    author_user_id: str = "",
) -> CommentHighlight | None:
    """解析 PC 端 API 返回的单条评论。

    Args:
        comment_data: API 返回的评论数据
        author_user_id: 微博作者 ID（用于判断 is_author）

    Returns:
        CommentHighlight 或 None
    """
    try:
        text = comment_data.get("text_raw", "") or comment_data.get("text", "")
        if not text:
            return None
        content = _clean_html(text)
        if not content:
            return None

        user_info = comment_data.get("user", {})
        user_name = (
            user_info.get("screen_name", "") if isinstance(user_info, dict) else ""
        )
        user_id = (
            str(user_info.get("id", "")) if isinstance(user_info, dict) else ""
        )

        like_count = int(comment_data.get("like_count", 0) or 0)
        is_author = bool(author_user_id and user_id == author_user_id)

        # PC API 没有置顶字段在单条评论上，但 rootComment 可能提供

        return CommentHighlight(
            content=content,
            user_name=user_name,
            is_author=is_author,
            like_count=like_count,
        )
    except Exception as e:
        logger.debug("解析评论数据失败: %s", e)
        return None


async def fetch_weibo_comment_highlights(
    post_id: str,
    config: Config,
    *,
    author_user_id: str = "",
    max_count: int = MAX_COMMENT_HIGHLIGHTS,
) -> list[CommentHighlight]:
    """获取微博帖子的评论亮点（PC 端 API）。

    按点赞数降序排列，最多返回 max_count 条。
    翻页获取更多评论，失败时返回空列表。

    Args:
        post_id: 帖子 ID
        config: 全局配置
        author_user_id: 帖子作者 ID（用于标记作者评论）
        max_count: 最大返回数量

    Returns:
        评论亮点列表
    """
    cookie = config.weibo.auth.cookie
    if not cookie:
        logger.debug("[评论] 缺少 Cookie，跳过评论抓取: %s", post_id)
        return []

    headers = {
        "User-Agent": _get_default_ua(),
        "Referer": "https://weibo.com/",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
    }

    all_comments: list[CommentHighlight] = []
    session = await get_session()
    max_id = 0
    page = 0
    max_pages = 5  # 最多 5 页

    while len(all_comments) < max_count * 2 and page < max_pages:
        page += 1
        try:
            url = COMMENT_API.format(post_id=post_id)
            if max_id:
                url += f"&max_id={max_id}"

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=WEIBO_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    logger.debug(
                        "[评论] API 返回状态码: %s, post_id: %s",
                        resp.status,
                        post_id,
                    )
                    break

                data = await resp.json()

            if not data.get("ok"):
                logger.debug(
                    "[评论] API 失败: %s, post_id: %s",
                    data.get("msg", "unknown"),
                    post_id,
                )
                break

            comments_raw = data.get("data", [])
            if not isinstance(comments_raw, list) or not comments_raw:
                break

            for raw in comments_raw:
                comment = _parse_comment(raw, author_user_id)
                if comment is not None:
                    all_comments.append(comment)

            # 翻页
            max_id = data.get("max_id", 0) or 0

        except Exception as e:
            logger.warning("[评论] 抓取评论异常: %s, post_id: %s", e, post_id)
            break

    if not all_comments:
        return []

    # 按点赞数降序，取前 max_count
    all_comments.sort(key=lambda c: c.like_count, reverse=True)
    result = all_comments[:max_count]

    logger.info("[评论] 获取到 %d 条热门评论, post_id: %s", len(result), post_id)
    return result
```

- [ ] **Step 2: 更新 Weibo handler 去掉 PC API 禁用的 author_user_id 入参**

Weibo handler 目前不传 `author_user_id`。在 `handlers.py:141` 添加 `author_user_id` 参数。由于 `WeiboPost` 有 `user_id`，需要在 detector 阶段拿到作者 user_id。

实际上 Weibo handler 中，`ctx.msg` 没有存储 user_id。最简单的做法：detector 阶段把 user_id 从 `fetch_user_posts` 的结果传递到 store 中。但 `MessageRecord` 目前没有 user_id 字段。

更简单的方案：detector 中传 `author_user_id=""` 或者从 config subscription 中读取。实际上 sub 配置里已经有 `user_id` 了。

`platforms/weibo/handlers.py:139-150` 修改：
```python
    # Fetch comment highlights
    try:
        # 从 config subscription 找对应用户的 user_id
        author_uid = ""
        for sub in ctx.config.weibo.subscriptions:
            # post_id 开头可能匹配 user_id, 但更可靠的方式是从 post 中还原
            pass  # 简化为不传 author_user_id，评论中只标记点赞排序
        highlights = await fetch_weibo_comment_highlights(
            post_id=post_id,
            config=ctx.config,
        )
```

实际：`fetch_weibo_comment_highlights` 的 `author_user_id` 是可选的，不传时一直 `is_author=False`，只按点赞排序，完全能满足当前需求。

- [ ] **Step 3: 更新 Weibo handler 的格式化函数使用统一 CommentHighlight**

`platforms/weibo/handlers.py:54-66` 更新为使用统一模型的字段：

```python
def _format_weibo_comment_highlights(highlights: list) -> str:
    """格式化微博评论亮点为 Markdown。"""
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_author", False)
        tag = " (作者)" if is_author else ""
        parts.append(f"- **{name}**{tag} (👍{like}):\n  {content}")
    return "\n".join(parts)
```

这个函数实际上不需要改了，字段名一致（`is_author` 保持不变）。

- [ ] **Step 4: 更新测试 mock**

`tests/test_weibo_comments.py`:
- Mock 的 API 返回格式从 mobile hotflow 改为 PC buildComments 格式
- 更新 URL 和 headers

```python
class TestFetchWeiboCommentHighlights:
    @pytest.mark.asyncio
    async def test_returns_highlights(self):
        cfg = MagicMock()
        cfg.weibo.auth.cookie = "SUB=fake"

        mock_resp = MagicMock()
        mock_resp.status = 200

        async def json_side(*, content_type=None) -> dict:
            return {
                "ok": 1,
                "data": [
                    {
                        "text_raw": "好评论",
                        "text": "好评论",
                        "user": {"screen_name": "用户A", "id": 1},
                        "like_count": 100,
                        "comments": [],  # PC API 的 sub-replies 字段
                    },
                ],
                "total_number": 1,
                "max_id": 0,
            }

        mock_resp.json = AsyncMock(side_effect=json_side)
        mock_session = MagicMock()
        mock_session.get = AsyncMock(return_value=mock_resp)

        with patch("platforms.weibo.comments.get_session", return_value=mock_session):
            results = await fetch_weibo_comment_highlights("post123", cfg)

        assert len(results) == 1
        assert results[0].content == "好评论"
        assert results[0].like_count == 100
```

另外 `text_raw` 是 PC API 的字段（纯净文本），`text` 是 HTML 版本，优先用 `text_raw`。

- [ ] **Step 5: 运行 Weibo 评论测试**

```bash
uv run pytest tests/test_weibo_comments.py -v --tb=short 2>&1 | tail -20
```
Expected: All ~12 tests pass.

---

### Task 4: 统一评论格式化函数

Weibo 和 XHS 的 handlers 各自定义了自己的 `_format_comment_highlights`。Bilibili 的格式化也 inline 在 handler 中。把它们统一到 `core/formatter.py`。

- [ ] **Step 1: 创建 `core/formatter.py`**

```python
"""跨平台评论格式化 — 将统一 CommentHighlight 列表格式化为 Markdown"""

from __future__ import annotations


def format_comment_highlights(highlights: list) -> str:
    """将评论亮点列表格式化为 Markdown 文本。

    兼容统一 CommentHighlight 模型的字段：
    - user_name, content, like_count
    - is_author → "UP主/作者" 标签
    - is_pinned → "置顶" 标签
    - reply_to + parent_content → 对话链路

    Args:
        highlights: CommentHighlight 列表

    Returns:
        Markdown 格式字符串
    """
    if not highlights:
        return ""
    parts: list[str] = []
    for h in highlights:
        name = getattr(h, "user_name", "匿名")
        content = getattr(h, "content", "")
        like = getattr(h, "like_count", 0)
        is_author = getattr(h, "is_author", False)
        is_pinned = getattr(h, "is_pinned", False)
        reply_to = getattr(h, "reply_to", "")
        parent_content = getattr(h, "parent_content", "")

        tags: list[str] = []
        if is_author:
            tags.append("作者")
        if is_pinned:
            tags.append("置顶")
        tag = f" ({', '.join(tags)})" if tags else ""

        if reply_to and parent_content:
            parts.append(
                f"- **{reply_to}**:\n"
                f"  > {parent_content}\n"
                f"  **{name}**{tag} (👍{like}):\n"
                f"  {content}"
            )
        else:
            parts.append(f"- **{name}**{tag} (👍{like}):\n  {content}")
    return "\n".join(parts)
```

- [ ] **Step 2: 更新 Bilibili handler 使用统一格式化**

`platforms/bilibili/handlers.py`:
- 第 77-107 行 `_format_comment_highlights` 替换为 from 导入
- 删除函数本体

```python
from core.formatter import format_comment_highlights

# 删除 _format_comment_highlights 函数定义
# 在 summarize_phase 中: 
#   ctx.comment_highlights = format_comment_highlights(highlights)
```

- [ ] **Step 3: 更新 Weibo handler 使用统一格式化**

`platforms/weibo/handlers.py`:
- 第 54-66 行 `_format_weibo_comment_highlights` 替换为 from 导入
- 注意 Weibo handler 的格式化没有 `reply_to`/`parent_content` 处理，统一格式化已支持

```python
from core.formatter import format_comment_highlights as _format_weibo_comment_highlights
```
或者直接改名、替换引用。

- [ ] **Step 4: 更新 XHS handler 使用统一格式化**

搜索 XHS handler 中评论格式化相关代码并替换。

- [ ] **Step 5: 运行所有测试**

```bash
uv run pytest -x --tb=short 2>&1 | tail -30
```
Expected: All tests pass.

---

### Task 5: Bilibili 评论模块 — is_up_owner → is_author 重命名

- [ ] **Step 1: 更新 `_build_highlight` 参数**

`platforms/bilibili/comments.py:30-50`:
```python
def _build_highlight(
    *,
    content: str,
    user_name: str,
    is_author: bool,  # 原名 is_up_owner
    like_count: int,
    is_pinned: bool = False,
    reply_to: str = "",
    parent_content: str = "",
) -> CommentHighlight | None:
    if not content:
        return None
    return CommentHighlight(
        content=content,
        user_name=user_name,
        is_author=is_author,
        like_count=like_count,
        is_pinned=is_pinned,
        reply_to=reply_to,
        parent_content=parent_content,
    )
```

- [ ] **Step 2: 更新所有调用处**

`platforms/bilibili/comments.py` 全文搜索 `is_up_owner=` 替换为 `is_author=`。

- [ ] **Step 3: 运行 Bilibili 相关测试**

```bash
uv run pytest -x --tb=short -k bili 2>&1 | tail -20
```
Expected: All passing.

---

### Task 6: 更新 XHS 评论使用统一模型

`platforms/xiaohongshu/comments.py` 已在 Task 1 中更新引用，验证 XHS handler 和新 `core/formatter.py` 是否能正常搭配。不需要额外改动。

- [ ] **Step 1: 验证 XHS 编译**

```bash
uv run pyright platforms/xiaohongshu/comments.py 2>&1
```
Expected: No type errors.

---

### Task 7: 最终验证

- [ ] **Step 1: Lint + Type check + Test**

```bash
uv run ruff check . && uv run pyright . && uv run pytest -x --tb=short 2>&1 | tail -30
```

Expected: All clean.

- [ ] **Step 2: 手动验证 Weibo 长文 + 评论集成**

```bash
uv run trawler check --platform weibo 2>&1 | head -40
```
Expected: Pipeline runs successfully, long text posts show expanded content.
