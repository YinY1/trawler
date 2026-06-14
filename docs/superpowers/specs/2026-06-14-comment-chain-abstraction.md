# 统一评论链抽象

## 状态

- **实现状态**: 待实施 (deferred)
- **优先级**: 当前迭代之后
- **依赖**: 当前 master 上的 core/comments.py 基础骨架已存在

## 问题

各平台评论获取逻辑是散装的：

| 平台 | 获取函数 | 位置 | 入口调用 |
|------|---------|------|---------|
| B站 | `fetch_comment_highlights(bvid, config)` | `platforms/bilibili/comments.py:64` | `summarize_phase` (wildcard `*`) |
| 小红书 | `fetch_xhs_comment_highlights(note_id, config, *, author_user_id, max_count)` | `platforms/xiaohongshu/comments.py:71` | ❌ 从未被调用 |
| 微博 | `fetch_weibo_comment_highlights(post_id, config, *, max_count, author_name)` | `platforms/weibo/comments.py:85` | 在 `weibo_download` (Phase.DOWNLOADED) 内联 |

没有统一的抽象层，handler 里需要 if/elif 判断平台。

## 目标

1. **统一入口** — handler 不再关心平台，一行调用
2. **共享排序策略** — 三层优先级在所有平台一致
3. **最小侵入** — 不重构现有稳定代码，只加 layer

## 设计

### 架构

```
core/comments.py
├─ register(platform)→decorator   ← 各平台装饰器注册获取器
├─ fetch_comment_highlights(      ← 统一入口
│     platform, content_id, config, **kwargs
│   ) → list[CommentHighlight]
└─ prioritize_highlights(         ← 共享排序策略
      highlights, max_count=5
    ) → list[CommentHighlight]
```

### 注册表模式

与 `PipelineEngine.register` 相同的装饰器模式。各平台在 `comments.py` 中：

```python
from core.comments import register

@register("xhs")
async def fetch_highlights(content_id: str, config: Config, **kwargs) -> list[CommentHighlight]:
    """统一入口包装，代理到现有实现。"""
    ...
```

`core/comments.py` 内的 `_COMMENT_MODULES` 映射负责延迟导入（导入时触发 `@register` 装饰器）。

### 共享排序策略 (`prioritize_highlights`)

```
优先级 1: is_pinned=True           → 置顶评论（平台支持时）
优先级 2: is_author=True, 非 pinned → 作者本人的评论
优先级 3: is_author=True + reply_to≠"" → 作者回复他人（带对话链路）
优先级 4: 其余按 like_count 降序    → 高赞补位
```

各优先级内部按 like_count 降序。取 top-N。

### 平台适配

| 平台 | is_author | is_pinned | reply_to/parent_content |
|------|-----------|-----------|------------------------|
| B站 | ✅ 已有 | ✅ 已有 | ✅ 已有 |
| 小红书 | ✅ 已有（需去掉 filter-out） | ❌ API 不支持 → 默认 False | ❌ API 不支持 → 默认 "" |
| 微博 | 需加 `author_user_id` 参数 | ❌ 待探索 | ❌ 待探索 |

对于平台不支持的 metadata 字段，相应优先级层为空，自然 fallthrough 到下一层。

### Handler 调用

改前（散装 if/elif）：
```python
if ctx.msg.platform == "bili" and ctx.msg.content_type == ContentType.VIDEO:
    highlights = await fetch_comment_highlights(bvid=bvid, config=ctx.config)
    ctx.comment_highlights = format_comment_highlights(highlights)
elif ctx.msg.platform == "xhs":
    highlights = await fetch_xhs_comment_highlights(note_id=note_id, ...)
    ctx.comment_highlights = format_comment_highlights(highlights)
```

改后（统一入口）：
```python
from core.comments import fetch_comment_highlights

highlights = await fetch_comment_highlights(ctx.msg.platform, content_id, ctx.config)
ctx.comment_highlights = format_comment_highlights(highlights)
```

适用于所有平台 + 所有 content_type。TEXT 类笔记（跳过 SUMMARIZED 阶段）同样在 DOWNLOADED handler 中调用，remove 条件分支。

### 现有函数保留

各平台原有的 `fetch_{platform}_comment_highlights` 签名不变，保持向后兼容（现有测试依赖它们）。

新加的 `fetch_highlights` 只是轻量 wrapper：

```python
@register("xhs")
async def fetch_highlights(content_id: str, config: Config, **kwargs) -> list[CommentHighlight]:
    return await fetch_xhs_comment_highlights(note_id=content_id, config=config, **kwargs)
```

### 不变的部分

- `CommentHighlight` 数据模型
- `format_comment_highlights()` 格式化函数（`core/formatter.py`）
- 各平台 comment API 调用逻辑

## 实施路径

### Phase 1: core/comments.py 骨架 + 注册 ✅ 已完成

- `core/comments.py` 已存在，包含 `register`/`fetch_comment_highlights`/`_load_platform`
- `_COMMENT_MODULES` 映射已配好三个平台

### Phase 2: 各平台注册 + prioritize_highlights

- `platforms/bilibili/comments.py` — 加 `@register("bili")` wrapper
- `platforms/xiaohongshu/comments.py` — 加 `@register("xhs")` wrapper；去掉 `if comment.is_author: continue` 过滤行
- `platforms/weibo/comments.py` — 加 `@register("weibo")` wrapper；加 `author_user_id` 参数
- `core/comments.py` — 实现 `prioritize_highlights()` 排序函数

### Phase 3: Handler 简化

- `platforms/bilibili/handlers.py` — `summarize_phase` 去掉 if/elif 用统一入口
- `platforms/xiaohongshu/handlers.py` — `xhs_download` 去掉条件分支用统一入口
- `platforms/weibo/handlers.py` — `weibo_download` 去掉条件分支用统一入口

### Phase 4: 测试

- 各平台现有 comment 集成测试不变（测试旧函数签名）
- 新增 `tests/test_core_comments.py` 测试 `fetch_comment_highlights` 分发 + `prioritize_highlights` 排序

## 不做的事

- 不重构 `core/notifier.py` 或推送逻辑
- 不改 `CommentHighlight` 数据模型
- 不为不支持 metadata 的平台硬塞数据
