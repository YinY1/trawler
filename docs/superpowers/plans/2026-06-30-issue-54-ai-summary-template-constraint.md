# Issue #54 — AI 摘要模板约束 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重写 `_ANALYSIS_PROMPT_TEMPLATE`，在 `## 摘要` 段加硬约束（「1. 」「2. 」序号 + 字数下限 400 + 要点 3-8 条），让 LLM 不再过度压缩长视频/长文摘要。中庸方案：不强制 `**核心观点**` 子标题（向后兼容旧解析），但要求要点列表格式 + 字数硬下限。

**Architecture:**
1. **Prompt 改写**（`_ANALYSIS_PROMPT_TEMPLATE`）：在 `## 摘要` 段下方加「单条消息总字数不少于 400 字、不超过 1200 字」+ 「按 1. 2. 序号列 3-8 条要点，每条 30-100 字，必须含具体信息」。
2. **解析层不动**：`_SECTION_PATTERNS` 仍只匹配 `## 摘要` 行作为分隔符；字段内部允许 `**粗体**`、序号、换行。**不**去除 markdown 标记（决策 7：现有测试 `test_parse_preserves_bold_inside_summary` 锁定原样保留）。
3. **验收依赖服务端**：单元测试只能验证 prompt 字符串包含硬约束、长输出能被解析；实际摘要长度需要服务端跑 check 对照 log。

**依赖：** PR-1（issue #56）合入后才能开始 —— PR-1 放宽了 `_SECTION_PATTERNS` 兼容尾随冒号/加粗，PR-2 改完的 prompt 产出的输出格式更多样，需要 PR-1 的解析层兜底。

**Tech Stack:** Python 3.12, re, pytest。

---

## File Structure

| 文件 | 操作 | 责任 |
|---|---|---|
| `core/summarizer.py` | Modify | 改写 `_ANALYSIS_PROMPT_TEMPLATE`（line 26-56） |
| `tests/test_summarizer.py` | Modify | 新增 prompt 约束测试 + 长输出解析回归测试 |

---

## 前置条件

- [ ] **PR-1 已合入 master**（issue #56 的解析层放宽 + AnalysisResult.raw 字段 + handler warning）

Run: `git log --oneline master -5 | grep "#56"`

Expected: 看到 `fix: issue #56 - AI 摘要解析 silent empty 修复` 相关 commit。如未看到，等待 PR-1 合入并 rebase。

- [ ] **代码特征硬检测（避免 squash 改写 commit title 导致 git log 失效）**

PR-1 在 `core/summarizer.py` 留下两个可检测的代码特征：`_SECTION_PATTERNS["summary"].pattern` 兼容加粗标题（含 `\**`），`AnalysisResult` 新增 `raw` 字段。如 squash 改写 commit title 导致 `--grep` 失效，用代码特征作为 PR-1 合入的硬证据。

Run:
```bash
uv run python -c "from core.summarizer import _SECTION_PATTERNS, AnalysisResult; assert '\\**' in _SECTION_PATTERNS['summary'].pattern; assert hasattr(AnalysisResult(), 'raw')"
```

Expected: 无输出（断言通过，exit 0）。如 `ImportError` 或 `AssertionError`：PR-1 未合入或代码特征缺失，停止执行本 plan，等待 PR-1 合入并 rebase。

- [ ] **从 master 切新分支**

```bash
git checkout master
git pull origin master
git checkout -b feat/issue-54-ai-summary-template-constraint
```

---

## Task 1: 写 prompt 约束的失败测试

**Files:**
- Modify: `tests/test_summarizer.py`

- [ ] **Step 1: 在 TestParseMarkdownAnalysis 类之后追加 TestPromptTemplate 约束测试类**

在 `tests/test_summarizer.py` 末尾（line 458 之后）追加新测试类：

```python
class TestPromptTemplateConstraints:
    """Issue #54: _ANALYSIS_PROMPT_TEMPLATE 必须含字数下限 + 要点数量下限 + 序号格式硬约束，
    让 LLM 不再过度压缩长视频/长文摘要。"""

    def test_prompt_contains_word_count_lower_bound(self) -> None:
        """prompt 模板必须包含「400 字」字数下限约束。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "400" in _ANALYSIS_PROMPT_TEMPLATE, "prompt 必须含 400 字下限"
        assert "字" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_contains_word_count_upper_bound(self) -> None:
        """prompt 模板必须包含「1200 字」上限（避免 LLM 输出过长触发 max_tokens 截断）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "1200" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_contains_keypoints_count_lower_bound(self) -> None:
        """prompt 必须含「3-8 条要点」数量约束。

        断言强化（issue #54 review）：去掉宽松 OR 后半段（`"3" in ... and "8" in ...`，
        模板里 3 和 8 任意出处都会假 PASS），只接受紧邻的「3-8」字面约束。
        """
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "3-8" in _ANALYSIS_PROMPT_TEMPLATE, "prompt 必须含「3-8 条要点」数量约束"

    def test_prompt_requires_numbered_list_format(self) -> None:
        """prompt 必须要求用「1. 」「2. 」中文序号格式表达要点。

        断言强化（issue #54 review）：不能只看模板任意位置是否有 "1."，
        要定位到 `## 摘要` 段说明区（## 摘要 之后、## 一句话总结 之前），
        断言该子串同时含「1. 」和「3. 」序号约束。
        """
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        # 定位 `## 摘要\n` 之后到 `## 一句话总结` 之前的子串
        summary_start = _ANALYSIS_PROMPT_TEMPLATE.find("## 摘要\n")
        next_section = _ANALYSIS_PROMPT_TEMPLATE.find("## 一句话总结")
        assert summary_start != -1, "prompt 必须含 `## 摘要` 段标题"
        assert next_section != -1, "prompt 必须含 `## 一句话总结` 段标题"
        summary_block = _ANALYSIS_PROMPT_TEMPLATE[summary_start:next_section]
        assert "1. " in summary_block, "## 摘要 段必须含「1. 」序号约束"
        assert "3. " in summary_block, "## 摘要 段必须含「3. 」序号约束（要求至少 3 条要点）"

    def test_prompt_requires_concrete_info_per_point(self) -> None:
        """prompt 必须要求每条要点含具体信息（数据/案例/时间/论据），不只复述标题。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        # 至少要提到具体信息的某一种
        assert any(
            kw in _ANALYSIS_PROMPT_TEMPLATE
            for kw in ("数据", "案例", "时间", "论据", "人名", "引用")
        ), "prompt 必须要求每条要点含具体信息"

    def test_prompt_still_has_four_sections(self) -> None:
        """回归保护：prompt 仍然包含 4 个标准字段标题（解析层依赖）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        for section in ("## 摘要", "## 一句话总结", "## 关键词", "## 标签"):
            assert section in _ANALYSIS_PROMPT_TEMPLATE, f"prompt 必须保留 {section} 字段标题"

    def test_prompt_keeps_placeholder_format(self) -> None:
        """回归保护：prompt 模板必须保留 {title}/{author}/{text} 占位符供 str.format 调用。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        assert "{title}" in _ANALYSIS_PROMPT_TEMPLATE
        assert "{author}" in _ANALYSIS_PROMPT_TEMPLATE
        assert "{text}" in _ANALYSIS_PROMPT_TEMPLATE

    def test_prompt_format_succeeds(self) -> None:
        """prompt 模板能被 str.format 正常渲染（验证无 stray brace 导致 KeyError）。"""
        from core.summarizer import _ANALYSIS_PROMPT_TEMPLATE

        rendered = _ANALYSIS_PROMPT_TEMPLATE.format(title="T", author="A", text="正文")
        assert "T" in rendered
        assert "A" in rendered
        assert "正文" in rendered
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_summarizer.py::TestPromptTemplateConstraints -v`

Expected: `test_prompt_contains_word_count_lower_bound`、`test_prompt_contains_word_count_upper_bound`、`test_prompt_contains_keypoints_count_lower_bound`、`test_prompt_requires_concrete_info_per_point` FAIL（当前 prompt 不含「400」「1200」「3-8」字样）。其他 PASS（当前 prompt 已有序号、4 字段、占位符）。

---

## Task 2: 重写 _ANALYSIS_PROMPT_TEMPLATE

**Files:**
- Modify: `core/summarizer.py:26-56`

- [ ] **Step 1: 修改 prompt 模板**

修改 `core/summarizer.py:26-56`，将原有的 `_ANALYSIS_PROMPT_TEMPLATE`：

```python
_ANALYSIS_PROMPT_TEMPLATE = """\
你是内容分析助手。请阅读以下内容，严格按下面的 Markdown 格式输出分析结果，\
不要输出任何额外说明或前后缀。每个字段必须以指定标题开头（## 摘要 / ## 一句话总结 / \
## 关键词 / ## 标签）。如果某字段无法填写，输出该标题并留空内容。

重要：字段标题（## 摘要 等）仅作为分隔符，用于解析。字段内容必须是纯文本，\
禁止使用任何 Markdown 语法（不要使用 **粗体**、*斜体*、[链接](url)、`代码`、```代码块```、\
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

替换为（**注意保留行尾反斜杠续行 + 4 个标准字段标题 + 占位符格式**）：

```python
_ANALYSIS_PROMPT_TEMPLATE = """\
你是内容分析助手。请阅读以下内容，严格按下面的 Markdown 格式输出分析结果，\
不要输出任何额外说明或前后缀。每个字段必须以指定标题开头（## 摘要 / ## 一句话总结 / \
## 关键词 / ## 标签）。如果某字段无法填写，输出该标题并留空内容。

重要：字段标题（## 摘要 等）仅作为分隔符，用于解析。字段内容必须是纯文本，\
禁止使用任何 Markdown 语法（不要使用 **粗体**、*斜体*、[链接](url)、`代码`、```代码块```、\
> 引用 等标记）。摘要部分必须用「1. 」「2. 」这样的中文序号表达要点，\
不要使用「- 」开头的 markdown 列表。

输出格式（必须严格遵循）：

## 摘要
（详细总结，覆盖所有重要观点。字数下限 400 字、上限 1200 字。\
按「1. 」「2. 」「3. 」中文序号列出 3-8 条要点，按重要性排序；\
每条 30-100 字；每条必须含具体信息（数据、案例、时间、地点、人名、引用、论据），\
不要只复述标题。如视频/正文较长且信息密度高，应优先覆盖更多要点而非压缩每条字数。\
如内容确实不足 400 字（如短动态、短评论），按实际信息量输出但必须穷尽要点。）

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

**关键改动点：**
1. 第 4 段「重要：」段落：把「请用「1. 」「2. 」这样的中文序号**或自然语句**表达」改为「**必须**用「1. 」「2. 」这样的中文序号表达」（去掉「或自然语句」，强制序号）。
2. `## 摘要` 段下方说明：从「详细总结，覆盖所有重要观点；用自然段落或序号表达要点」改为完整硬约束（400 字下限 + 1200 字上限 + 3-8 条 + 30-100 字/条 + 具体信息要求 + 优先覆盖更多要点 + 短内容兜底）。

- [ ] **Step 2: 跑 Task 1 的测试确认通过**

Run: `uv run pytest tests/test_summarizer.py::TestPromptTemplateConstraints -v`

Expected: 8 个测试全部 PASS。

- [ ] **Step 3: 跑全量 summarizer 测试确认无回归**

Run: `uv run pytest tests/test_summarizer.py -v`

Expected: 全部 PASS（包括 PR-1 引入的 50 个测试 + 这里的 8 个新测试 = 58 个）。注意 `test_analyze_content_success_caches_on_result`（line 287）mock 的 AI 输出仍是简短格式（"1. 要点一"），prompt 改了但 mock 不变，解析层照常工作。

- [ ] **Step 4: Commit**

```bash
git add core/summarizer.py tests/test_summarizer.py
git commit -m "feat(summarizer): rewrite prompt template with hard 400-char lower bound and 3-8 numbered keypoints (issue #54)"
```

---

## Task 3: 长输出解析回归测试

**Files:**
- Modify: `tests/test_summarizer.py`

**目的：** 验证 PR-2 改完的 prompt 产出的「长摘要 + 序号要点」格式能被 PR-1 放宽后的解析层正确处理，不会因为内容长 / 多换行 / 含 `1.` 数字导致 `_extract_section` 误截断。

- [ ] **Step 1: 写长输出解析测试**

在 `tests/test_summarizer.py::TestParseMarkdownAnalysis` 类末尾（PR-1 已新增 5 个测试之后）追加：

```python
    def test_parse_long_summary_with_numbered_keypoints(self) -> None:
        """Issue #54 回归: prompt 改完后 LLM 输出 400+ 字 + 3-8 条序号要点，
        解析层必须完整保留，不被「## 关键词」提前截断或被「1. 2. 」序号干扰。"""
        # 构造一个 400+ 字、5 条要点的摘要（模拟新 prompt 输出）
        long_summary = (
            "1. 视频开篇作者引用了一组关键数据：2024 年中国短视频用户规模达到 9.8 亿，"
            "占总网民的 87.5%，相比 2022 年增长了 12 个百分点。\n"
            "2. 第二个论点围绕算法推荐机制展开，作者以抖音的协同过滤为例，"
            "解释了「信息茧房」效应如何在 6 个月内形成。\n"
            "3. 第三个案例是某三线城市 UP 主通过分析后台数据，"
            "在 3 个月内将完播率从 22% 提升到 47% 的实操路径。\n"
            "4. 时间线梳理：2023Q1 政策收紧 → 2023Q3 平台调整 → 2024Q2 创作者生态反弹，"
            "整个周期约 18 个月。\n"
            "5. 最后作者提出三个开放性问题，邀请观众在评论区讨论，"
            "并引用了《注意力经济》一书的观点作为收尾。"
        )
        assert len(long_summary) > 400  # 满足新 prompt 的字数下限

        raw = f"""## 摘要
{long_summary}

## 一句话总结
新 prompt 下产出的长摘要解析回归测试

## 关键词
短视频；算法推荐；信息茧房

## 标签
教程, 评测"""

        result = parse_markdown_analysis(raw)

        # 全部 5 条要点必须完整保留（不被截断）
        assert "9.8 亿" in result.summary
        assert "协同过滤" in result.summary
        assert "三线城市 UP 主" in result.summary
        assert "2023Q1" in result.summary
        assert "《注意力经济》" in result.summary
        # 序号格式保留
        assert "1." in result.summary
        assert "5." in result.summary
        # 总长度仍 > 400（未被截断）
        assert len(result.summary) > 400

    def test_parse_summary_with_bold_numbered_keypoints_pr54(self) -> None:
        """Issue #54 回归: LLM 偶尔会用「**1.** **要点标题**：内容」格式，
        解析层（PR-1 已放宽正则兼容加粗标题）必须原样保留字段内容。"""
        raw = """## 摘要
**1.** **核心观点**：作者认为算法推荐已经改变了内容创作的底层逻辑
**2.** **关键数据**：调研样本量 N=12000，置信度 95%

## 关键词
算法"""
        result = parse_markdown_analysis(raw)
        # 决策 7：字段内 **粗体** 原样保留（不剥离）
        assert "**核心观点**" in result.summary
        assert "**关键数据**" in result.summary
        assert "N=12000" in result.summary
```

- [ ] **Step 2: 跑测试确认通过**

Run: `uv run pytest tests/test_summarizer.py::TestParseMarkdownAnalysis::test_parse_long_summary_with_numbered_keypoints tests/test_summarizer.py::TestParseMarkdownAnalysis::test_parse_summary_with_bold_numbered_keypoints_pr54 -v`

Expected: 2 个测试 PASS。如 FAIL（例如 `1.` 被 `_parse_list_field` 误识别），需检查 `_extract_section` 的「下一节」判定逻辑 —— PR-1 已确认该逻辑只看行首 `^#{1,3}\s*\S`，数字 `1.` 不会触发，应该 PASS。

- [ ] **Step 3: 跑全量测试**

Run: `uv run pytest tests/test_summarizer.py -v`

Expected: 全部 PASS（PR-1 的 50 + Task 1 的 8 + Task 3 的 2 = 60 个）。

- [ ] **Step 4: Commit**

```bash
git add tests/test_summarizer.py
git commit -m "test(summarizer): regression test for long numbered-keypoints summary parsing (issue #54)"
```

---

## Task 4: 全量验证 + push

**Files:** 全部修改过的文件

- [ ] **Step 1: ruff lint**

Run: `uv run ruff check .`

Expected: 无新增 lint error。

- [ ] **Step 2: ruff format（如有格式问题）**

Run: `uv run ruff format core/summarizer.py tests/test_summarizer.py`

Expected: 文件已格式化或无变化。

- [ ] **Step 3: pyright 类型检查**

Run: `uv run pyright`

Expected: 无新增 type error（**注意：不要加 `.` 参数**）。

- [ ] **Step 4: 全量 pytest**

Run: `uv run pytest -x`

Expected: 全部 PASS。

- [ ] **Step 5: 检查 git 状态**

Run: `git status && git log --oneline -10`

Expected: 2 个 commit（Task 2、Task 3），工作区干净。

- [ ] **Step 6: Push 分支**

Run: `git push -u origin feat/issue-54-ai-summary-template-constraint`

Expected: 推送成功。

- [ ] **Step 7: 创建 PR**

```bash
gh pr create \
  --base master \
  --head feat/issue-54-ai-summary-template-constraint \
  --title "feat: issue #54 - AI 摘要模板约束（字数下限 400 + 3-8 序号要点）" \
  --body "## 改动内容

Issue #54：重写 \`_ANALYSIS_PROMPT_TEMPLATE\`，加硬约束防止 LLM 过度压缩。

## 改动
- \`_ANALYSIS_PROMPT_TEMPLATE\`（core/summarizer.py:26-56）：
  - \`## 摘要\` 段加「字数下限 400、上限 1200」
  - 「3-8 条要点，按 1. 2. 序号，每条 30-100 字，必须含具体信息」
  - 「优先覆盖更多要点而非压缩每条字数」「短内容按实际信息量穷尽要点」兜底
  - 「重要：」段把「或自然语句」改为「必须用序号」，强化约束

## 不改动
- \`_SECTION_PATTERNS\`（解析正则不变，仍只匹配 \`## 摘要\` 行作为分隔符）
- 字段内 \`**粗体**\` 不剥离（决策 7：现有测试 \`test_parse_preserves_bold_inside_summary\` 锁定原样保留）
- 不加 \`prompt_template_path\` 可配置项（YAGNI，issue 建议但当前无需求）

## 测试
- 8 个 prompt 约束测试（400/1200 字下限/上限、3-8 条、序号、具体信息、4 字段、占位符、format 渲染）
- 2 个长输出解析回归测试（400+ 字 + 5 条序号、加粗序号变体）

## 验收
- 单元测试：\`uv run pytest tests/test_summarizer.py\` 全部 PASS
- **手动验收**：服务端跑一次 \`trawler check --reset-phase summarized\`，对照 trawler.log 看新模板产出的摘要长度明显增加（>= 400 字）

## 依赖
- 依赖 PR-1（issue #56）的解析层放宽：新 prompt 输出格式更灵活，需要 PR-1 的 \`^#{1,3}\\s*\\**摘要\\**\\s*[:：]?\\s*\` 正则兜底

## 关联
- \`core/summarizer.py:26-56, 62-67\`（prompt 模板 + 解析正则）"
```

Expected: PR 创建成功，返回 PR URL。

- [ ] **Step 8: 等待 CI + Qodo review**

按全局 AGENTS.md PR review 轮询流程处理（每 3 分钟 `gh pr view <PR> --comments`，连续 2 次无新评论视为完成）。

---

## 验收清单

| 验收项 | 验证方式 | 预期结果 |
|---|---|---|
| prompt 含 400 字下限 | `test_prompt_contains_word_count_lower_bound` | PASS |
| prompt 含 1200 字上限 | `test_prompt_contains_word_count_upper_bound` | PASS |
| prompt 含 3-8 条要点 | `test_prompt_contains_keypoints_count_lower_bound` | PASS |
| prompt 强制序号 | `test_prompt_requires_numbered_list_format` | PASS |
| prompt 要求具体信息 | `test_prompt_requires_concrete_info_per_point` | PASS |
| prompt 保留 4 字段 | `test_prompt_still_has_four_sections` | PASS |
| prompt 占位符 | `test_prompt_keeps_placeholder_format` + `test_prompt_format_succeeds` | PASS |
| 长输出解析不截断 | `test_parse_long_summary_with_numbered_keypoints` | PASS |
| 加粗序号变体解析 | `test_parse_summary_with_bold_numbered_keypoints_pr54` | PASS |
| 粗体保留（不破坏） | `test_parse_preserves_bold_inside_summary`（原有） | PASS |
| 全量测试 | `uv run pytest -x` | 全部 PASS |
| 类型检查 | `uv run pyright` | 0 error |
| Lint | `uv run ruff check .` | 无新增 |

## 手动验收（服务端，PR 合入后）

```bash
# 容器内重跑长视频消息，对比摘要长度
trawler check --reset-phase summarized

# 关注 trawler.log 中「AI 内容分析成功」对应消息的 summary 字段
# 用 jq 取出 messages.json 中 summary 字段长度
jq '.messages | to_entries[] | {msg_id: .key, summary_len: (.value.summary | length)} | select(.summary_len > 0)' data/messages.json
# 预期：长视频消息的 summary_len >= 400
```
