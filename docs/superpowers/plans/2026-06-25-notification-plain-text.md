# 通知消息 Plain Text 改造

**日期**: 2026-06-25
**优先级**: P4
**分支**: `feat/notification-plain-text`（当前在 master，动手前先开分支）

## 目标

最终推送给用户的通知消息从 Markdown 改为纯文本（plain text），包括 AI 摘要部分。

## 调研发现

### 当前 Markdown 输出散落位置

| 文件 | 行号 | 内容 | 性质 |
|------|------|------|------|
| `core/notifiers/base.py` | 27-58 | `render_markdown()` — 通知消息唯一渲染入口，输出 `**xxx:**`、`---`、`[text](url)` | **核心改造点** |
| `core/formatter.py` | 14-52 | `format_comment_highlights()` — 评论亮点格式化为 `- **name** (👍N):\n  content` | **核心改造点** |
| `core/summarizer.py` | 26-51 | `_ANALYSIS_PROMPT_TEMPLATE` — prompt 要求 LLM 用 `## 摘要 / ## 关键词` 等 Markdown 标题输出 | **AI prompt 改造点** |
| `core/summarizer.py` | 57-132 | `_SECTION_PATTERNS` / `parse_markdown_analysis` — 解析依赖 `^#{1,3}\s*` 模式 | **需配套调整**（仅当 prompt 改格式时）|

### 架构关键事实（已验证）

1. **渲染唯一入口**：`GotifyNotifier.send()`（`gotify.py:39`）调用 `render_markdown()`；`TelegramNotifier` / `EmailNotifier` 是 stub（`NotImplementedError`），但启用后也将走同一渲染层。
2. **存储与渲染解耦**：`MessageStore`（`shared/message_store.py`）只存 `MessageRecord`（phase/title/author/dynamic_text/error），**不存储渲染后的 message body**。改造零影响 `messages.json`。
3. **Web UI 与通知渲染解耦**：`web/routes/dashboard.py` 仅消费 `MessageRecord`，模板（`web/templates/dashboard.html`）不渲染 summary / comment_highlights / markdown。grep `summary\|markdown\|\*\*` 在 `web/templates/*.html` 无命中。**改造不会误伤 web 展示**。
4. **EndpointConfig.kind**（`shared/config.py:138`）：已知值 `gotify` | `telegram` | `email`。Gotify 默认支持 markdown；Telegram 走 Bot API（需 `parse_mode`）；Email 是 stub。当前 Gotify 是唯一活跃通道，plain text 在 Gotify 端天然兼容。
5. **`NotificationContent.type`** 已有 `"content"` | `"dynamic"` | `"health_alert"`（`run_check.py:524`）三种，渲染层 `base.py:37` 只对 `"dynamic"` 走短模板，其他都走默认模板。

### 不需要改动的部分

- `shared/protocols.py`：`NotificationContent` / `CommentHighlight` 数据模型保持不变（结构化字段，与渲染解耦）
- `shared/message_store.py`：存储格式不变
- `platforms/*/handlers.py`：调用 `analyze_content()` / `format_comment_highlights()` 的签名不变；填入 `NotificationContent.summary` / `.comment_highlights` 的语义不变
- `core/notifiers/__init__.py`：fan-out 工厂不变
- `web/**`：不消费 markdown

---

## 改造方案

### 设计决策

**决策 1：渲染层是 plain text 化的唯一战场**
所有 endpoint 统一从 `render_markdown()` 取消息，改这一个函数即可让所有通道同时变 plain。函数可保留旧名（外部 API 名字 `render_markdown` 是事实接口，AGENTS.md 规定不改 error message / 外部接口文本），但内部输出 plain text；或新增 `render_text()` 并让 `render_markdown` 作为兼容别名。**推荐：原地改 `render_markdown` 的实现，不改函数名**（最小改动原则）。

**决策 2：AI prompt 是否改格式？**
- 当前 prompt 用 `## 摘要` 等标题作为解析分隔符，解析层 `_SECTION_PATTERNS` 用正则匹配 `#{1,3}`。
- **风险**：若改 prompt 让 LLM 输出纯文本（无 `##` 标题），解析层会全部失效，summary/keywords/tags 全部为空。
- **方案**：prompt 仍保留结构化标记供解析使用，但在 prompt 中**额外要求 LLM 不要在「字段内容」里使用 markdown 语法**（`**粗体**`、`- 列表`、`[链接](url)`、反引号等）。解析出的 `summary` 字段本身就是纯文本，渲染层再格式化为 plain。
- **解析层不动**。`parse_markdown_analysis` 继续工作。

**决策 3：评论格式化 plain 化**
`format_comment_highlights()` 输出从 `- **name** (👍N):\n  content` 改为 `• name (👍N): content`（单行 plain）。

**决策 4：不同 endpoint 一致性**
所有 endpoint 共享 `render_markdown()`，plain text 化后所有通道一致。Gotify 默认接受 plain text（content type 由客户端 markdown 渲染，无 markdown 标记则原样显示）；Telegram（未来实现）默认 `parse_mode` 留空即 plain。无需 per-endpoint 分支。

---

## 文件清单

| 文件 | 改动 | 估计行数 |
|------|------|----------|
| `core/notifiers/base.py` | 改：`render_markdown()` 输出 plain text（去 `**` / `---` / `[text](url)`，改 `**key:**` 为 `key:`） | 改 ~15 行 |
| `core/formatter.py` | 改：`format_comment_highlights()` 输出 plain（`• name (👍N): content`，单行）+ docstring 更新 | 改 ~6 行 |
| `core/summarizer.py` | 改：`_ANALYSIS_PROMPT_TEMPLATE` 增加一条「禁止在内容中使用 markdown 语法」的约束 | 改 ~3 行 |
| `tests/test_notifier_base.py` | 改：6 个 `render_markdown` 测试断言（去 `**`、`---`、`[text](url)`） | 改 ~12 行 |
| `tests/test_gotify_notifier.py` | 改：`test_send_success` 中 `title` 断言保留（title 不变），无 message body 断言（无需改）；仅注释更新 | 改 0~1 行 |
| `tests/test_summarizer.py` | 改：测试 fixture 的 `ai_output` 文本里仍保留 `## 标题`（解析用），但 summary 字段内容去掉 `- ` 列表前缀，以验证纯文本；新增 1 个用例：LLM 返回带 `**bold**` 时 summary 应保留原样（plain 容忍）| 改 ~4 行，新增 ~15 行 |

总计：约 **改 40 行，新增 15 行**，无新增文件，无删除文件。

---

## 任务分解

### Task 1: TDD — 更新 `render_markdown` 测试（红）

**文件**: `tests/test_notifier_base.py`

当前 6 个 `render_markdown` 测试需要更新断言，从 markdown 改为 plain。

**改动示例**：

`test_render_bili_video`（line 45-59）：
```python
# 改前
assert "UP主:** UP" in msg
# 改后（去 **）
assert "UP主: UP" in msg
```

`test_render_dynamic_short_format`（line 81-93）：
```python
# 改前：不显式断言 markdown
# 改后：增加断言 plain
assert "**" not in msg
assert "---" not in msg
```

`test_render_comment_highlights`（line 96-106）：
```python
# 改后：断言无 markdown 标记
assert "**" not in msg
assert "精选评论" in msg
```

**新增测试**（追加在文件末尾）：
```python
def test_render_output_is_plain_text_no_markdown():
    """渲染结果不含任何 markdown 标记（**、---、[text](url)、> 引用）。"""
    c = NotificationContent(
        platform="bili",
        source_id="BV1xx",
        title="t",
        author="UP",
        summary="s",
        keywords=["k1"],
        comment_highlights="评论",
    )
    _, msg = render_markdown(c)
    for token in ("**", "---", "##", "> ", "]("):
        assert token not in msg, f"渲染结果含 markdown 标记: {token!r}"
```

**验证**：`uv run pytest tests/test_notifier_base.py -x` → 应红（断言失败）。

---

### Task 2: 改造 `render_markdown()`（绿）

**文件**: `core/notifiers/base.py`

**改前**（line 37-57）：
```python
if content.type == "dynamic":
    parts: list[str] = [f"**{style['author_label']}:** {content.author}"]
    if url:
        parts.append(f"**链接:** [{content.source_id}]({url})")
    parts.extend(["", "---", "", content.summary or content.title])
    return f"📢 {content.author} 的动态", "\n".join(parts)

parts = [
    f"**{style['author_label']}:** {content.author}",
    f"**链接:** [{content.source_id}]({url})" if url else "",
    f"**关键词:** {keywords_str}",
    "",
    "---",
    "",
    "**详情:**",
    content.summary,
]
if content.comment_highlights:
    parts.extend(["", "**评论区补充:**", content.comment_highlights])
return f"{style['emoji']} {content.title}", "\n".join(parts)
```

**改后**：
```python
if content.type == "dynamic":
    parts: list[str] = [f"{style['author_label']}: {content.author}"]
    if url:
        parts.append(f"链接: {content.source_id} {url}")
    parts.extend(["", content.summary or content.title])
    return f"📢 {content.author} 的动态", "\n".join(parts)

parts = [
    f"{style['author_label']}: {content.author}",
    f"链接: {content.source_id} {url}" if url else "",
    f"关键词: {keywords_str}",
    "",
    f"详情:",
    content.summary,
]
if content.comment_highlights:
    parts.extend(["", "评论区补充:", content.comment_highlights])
return f"{style['emoji']} {content.title}", "\n".join(parts)
```

要点：
- 去掉所有 `**...**` 粗体
- 去掉 `---` 分隔线（改为空行）
- 链接 `[text](url)` → `text url`（plain，可点链接由推送端识别 URL 自动 linkify）
- 字段名后冒号保留（`作者:` / `链接:` / `关键词:` / `详情:` / `评论区补充:`）

**验证**：`uv run pytest tests/test_notifier_base.py -x` → 全绿。

---

### Task 3: TDD — 更新 `format_comment_highlights` 测试（红）

**文件**: 当前没有独立 `tests/test_formatter.py`（grep 显示 `format_comment_highlights` 仅被 `test_notifier_base.py:102` 间接测到，传入字符串 `"精选评论"`）。需要新增 `tests/test_formatter.py` 覆盖 plain 输出。

**新增文件**: `tests/test_formatter.py`
```python
"""Tests for core/formatter.py — format_comment_highlights plain text 输出。"""

from __future__ import annotations

from core.formatter import format_comment_highlights
from shared.protocols import CommentHighlight


def test_empty_returns_empty_string():
    assert format_comment_highlights([]) == ""


def test_single_highlight_plain_format():
    h = CommentHighlight(content="好视频", user_name="张三", is_author=False, like_count=5)
    out = format_comment_highlights([h])
    assert "张三" in out
    assert "好视频" in out
    assert "5" in out
    # plain text：无 markdown
    assert "**" not in out
    assert "\n  " not in out  # 不再使用续行缩进
    assert "- " not in out  # 不使用 markdown 列表


def test_author_and_pinned_tags():
    h = CommentHighlight(
        content="置顶评论",
        user_name="UP",
        is_author=True,
        is_pinned=True,
        like_count=10,
    )
    out = format_comment_highlights([h])
    assert "作者" in out
    assert "置顶" in out


def test_reply_chain_compact():
    h = CommentHighlight(
        content="回复内容",
        user_name="B",
        is_author=False,
        like_count=1,
        reply_to="A",
        parent_content="原话",
    )
    out = format_comment_highlights([h])
    assert "A" in out
    assert "原话" in out
    assert "B" in out
    assert "回复内容" in out
```

**验证**：`uv run pytest tests/test_formatter.py -x` → 红。

---

### Task 4: 改造 `format_comment_highlights()`（绿）

**文件**: `core/formatter.py`

**改前**（line 14, 29-52）：
```python
def format_comment_highlights(highlights: list[CommentHighlight]) -> str:
    """将评论亮点列表格式化为 Markdown 文本。"""
    ...
    if reply_to and parent_content:
        parts.append(f"- **{reply_to}**:\n  > {parent_content}\n  **{name}**{tag} (👍{like}):\n  {content}")
    else:
        parts.append(f"- **{name}**{tag} (👍{like}):\n  {content}")
    return "\n".join(parts)
```

**改后**：
```python
def format_comment_highlights(highlights: list[CommentHighlight]) -> str:
    """将评论亮点列表格式化为纯文本（plain text）。

    输出无 markdown 语法，每条评论单行，便于推送端原样显示。
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
                f"  ↳ {reply_to}: {parent_content}\n{name}{tag} (👍{like}): {content}"
            )
        else:
            parts.append(f"• {name}{tag} (👍{like}): {content}")
    return "\n".join(parts)
```

要点：
- `- **name**` → `• name`（plain 项目符号）
- 多行 markdown 缩进 `\n  ` 改为单行 `: ` 连接
- 回复链路用 `↳` 视觉前缀代替 markdown `>` 引用
- 文件顶部 docstring（line 1）「格式化为 Markdown」→ 「格式化为纯文本」

文件顶部 docstring（line 1-7）：
```python
# 改前
"""跨平台评论格式化 — 将统一 CommentHighlight 列表格式化为 Markdown
...
"""

# 改后
"""跨平台评论格式化 — 将统一 CommentHighlight 列表格式化为纯文本 (plain text)
...
"""
```

**验证**：`uv run pytest tests/test_formatter.py -x` → 绿。

---

### Task 5: 改造 AI 摘要 prompt

**文件**: `core/summarizer.py`

**目标**：让 LLM 输出的「字段内容」是纯文本（无 `**bold**` / `[link](url)` / 反引号），同时保留 `## 标题` 供解析层切分字段。

**改前**（line 26-51，`_ANALYSIS_PROMPT_TEMPLATE`）：保留四个 `## 标题`，但需在 prompt 中追加约束。

**改后**（在 prompt 模板末尾的「输出格式」段落下追加约束）：
```python
_ANALYSIS_PROMPT_TEMPLATE = """\
你是内容分析助手。请阅读以下内容，严格按下面的 Markdown 格式输出分析结果，\
不要输出任何额外说明或前后缀。每个字段必须以指定标题开头（## 摘要 / ## 一句话总结 / \
## 关键词 / ## 标签）。如果某字段无法填写，输出该标题并留空内容。

重要：字段标题（## 摘要 等）仅作为分隔符，用于解析。字段内容必须是纯文本，\
禁止使用任何 Markdown 语法（不要使用 **粗体**、*斜体*、[链接](url)、\`代码\`、\`\`\`代码块\`\`\`、\
> 引用 等标记）。摘要部分如需列举要点，请用「1. 」「2. 」这样的中文序号或自然语句表达，\
不要使用「- 」开头的 markdown 列表。

输出格式（必须严格遵循）：

## 摘要
（详细总结，覆盖所有重要观点；用自然段落或「1. 」「2. 」序号表达要点，不要用 markdown 列表）

## 一句话总结
（单句概括，不超过 40 字）

## 关键词
（3-5 个关键词，用中文分号「；」分隔，只输出关键词本身）

## 标签
（0-3 个内容类型标签，如 教程、评测、Vlog，用逗号「，」分隔；若无则留空）

---

待分析内容：

标题：{title}
作者：{author}
正文：{text}"""
```

**解析层不变**：`_SECTION_PATTERNS` 仍匹配 `^#{1,3}\s*`（prompt 仍要求 `## 标题`），`parse_markdown_analysis` 不动。

**验证**：
1. `uv run pytest tests/test_summarizer.py -x` → 全绿（解析层未动）。
2. 手动跑一次（见验证步骤）确认 LLM 返回的 summary 字段是纯文本。

---

### Task 6: 更新 `test_summarizer.py` 测试 fixture

**文件**: `tests/test_summarizer.py`

`test_parse_well_formed`（line 81-99）和 `test_analyze_content_success_caches_on_result`（line 145-179）中的 `ai_output` fixture 当前用了 `- 第一点` markdown 列表前缀。需更新为符合新 prompt 约定的纯文本格式，验证解析仍工作。

**改前**（line 82-94）：
```python
raw = """## 摘要
- 第一点
- 第二点

## 一句话总结
...
```

**改后**：
```python
raw = """## 摘要
1. 第一点
2. 第二点

## 一句话总结
...
```

`_extract_section` 解析时不关心列表前缀（只按 `## 标题` 切分），所以断言 `"第一点" in result.summary` 仍然成立。

**新增用例**（追加到 `TestParseMarkdownAnalysis` 类）：
```python
def test_parse_preserves_bold_inside_summary(self) -> None:
    """LLM 偶尔不遵守 plain 约束返回 **bold**，解析层应容忍并原样保留。
    渲染层（plain text）将原样透传，不再尝试去除 markdown。"""
    raw = """## 摘要
这是 **粗体** 测试

## 关键词
A"""
    result = parse_markdown_analysis(raw)
    assert "**粗体**" in result.summary  # 原样保留，渲染时 plain 端只是显示字面量
```

> **设计权衡**：本 plan 选择「LLM 偶尔违规时原样透传」而非「渲染层再做一次 markdown strip」。理由：plain text 通道里 `**` 只会作为字面字符显示，无害；做 strip 反而可能误伤用户内容里本就出现的 `**`。风险与不确定项见下文。

---

## 验证步骤

按顺序执行，每步必须确认输出再继续。

### Step 1: 静态检查
```bash
uv run ruff check .      # 无新增 lint 问题
uv run ruff format .     # 格式化
uv run pyright .         # 无新增 type error
```

### Step 2: 单元测试
```bash
uv run pytest -x         # 全部通过（fail fast）
# 重点关注：
uv run pytest tests/test_notifier_base.py -v
uv run pytest tests/test_formatter.py -v
uv run pytest tests/test_summarizer.py -v
uv run pytest tests/test_gotify_notifier.py -v
```

### Step 3: 手动触发一次真实推送

> 需要：本地配置好的 gotify endpoint + 一条新视频触发摘要生成。

```bash
# 方式 A：删掉某条已 PUSHED 消息，重置到 DISCOVERED，让 pipeline 重跑
# 方式 B：直接调一个小的脚本调 render_markdown + GotifyNotifier.send
```

预期 gotify 收到的消息：
- 无 `**`、`---`、`[text](url)`
- `作者:` / `链接:` / `关键词:` / `详情:` / `评论区补充:` 字段清晰
- 摘要是自然段落或 `1. 2. 3.` 序号，无 `- ` 列表
- 评论是 `• name (👍N): content` 单行格式

### Step 4: 确认 web 端无回归
```bash
uv run python run_web.py
# 浏览器打开 dashboard，确认消息列表正常显示（不受改造影响）
```

---

## 兼容性考虑

### 不同 endpoint kind 一致性

| Kind | 状态 | Plain text 表现 |
|------|------|-----------------|
| `gotify` | 已实现，唯一活跃 | ✅ 天然兼容（无 markdown 标记 → 客户端原样显示） |
| `telegram` | stub (`NotImplementedError`) | 未来实现时只需**不设** `parse_mode`（或设为空字符串），Bot API 即按 plain 处理 |
| `email` | stub | 未来实现时 plain text 直接作为邮件正文 |

**结论**：所有 endpoint 共享 `render_markdown()`，plain 化后通道行为天然一致。**无需 per-endpoint 分支**。

### 向后兼容

- `messages.json` 格式不变（不含渲染结果）→ 老数据无迁移问题
- `NotificationContent` / `CommentHighlight` 数据模型不变 → 调用方（platforms/handlers.py）签名不变
- `render_markdown` 函数名保留（外部接口）→ import 不破

---

## 风险与不确定项

### 风险 1：LLM 不遵守 plain 约束（中等风险）

LLM 可能仍偶尔在 summary 字段返回 `**bold**` 或 `- list`。

**缓解**：
- prompt 已用强约束措辞（「禁止」「不要使用」）。
- 解析层与渲染层都不做 strip，原样透传；plain text 通道里 `**` 仅作为字面字符显示，对用户阅读无害。
- 若上线后用户反馈仍见 markdown 痕迹，可作为 follow-up 在 `render_markdown()` 增加可选的 `strip_markdown()` 后处理（本次不做，避免过度设计）。

### 风险 2：手动验证依赖外部 LLM（低风险）

Step 3 手动触发推送依赖真实 LLM API 和 gotify 配置；CI 环境可能无法验证。

**缓解**：
- 单元测试已覆盖解析层与渲染层逻辑（mock LLM 输出）。
- prompt 改造的影响面局限于「字段内容字符」，解析正确性由现有测试保护。
- 真实 LLM 行为需要部署后人工抽样验证（不在本 plan 自动化范围）。

### 风险 3：链接 plain 化后可点击性（低风险）

原 `[source_id](url)` 在 gotify 客户端会被识别为 markdown 链接（可点）；改为 `source_id url`（url 裸露）后，依赖客户端的 URL auto-linkify。

**缓解**：
- Gotify Android / iOS / Web 客户端普遍支持 URL 自动识别。
- 裸 URL 更通用（兼容所有 plain text 渲染端），符合「plain text」改造目标。
- 若未来某 endpoint 不支持 auto-linkify，可考虑 per-endpoint 处理（本 plan 不做）。

### 不确定项

- **email notifier 实现细节**：当前是 stub。未来实现时若用户希望 HTML 邮件，可能需要二次渲染层。本 plan 不解决，留给 email notifier 实现 PR。
- **历史推送消息**：已推送的老消息无法回收重发，用户看到的是老 markdown 格式。无回填方案，符合预期（P4 改造只影响未来推送）。

---

## 执行顺序（TDD 节奏）

1. **Task 1**（红）→ **Task 2**（绿）：render_markdown
2. **Task 3**（红）→ **Task 4**（绿）：format_comment_highlights
3. **Task 5** + **Task 6**：prompt + summarizer 测试（无红绿节奏，prompt 是配置改动，测试是同步更新）
4. **验证步骤 1-4**

每个 Task 完成后单独 `uv run pytest -x` 确认，不攒到最后。
