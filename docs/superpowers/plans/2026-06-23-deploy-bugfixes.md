# Deploy Bugfixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 4 production bugs blocking reliable cron operation: (1) XHS/Weibo token renewal false-success, (2) overlapping n-gram keyword/summary extraction + silent AI fallback, (3) cross-process PhaseContext loss + silent transcribe-failure-as-success, (4) broken cron command + missing health alerts.

**Architecture:** Each bug is an independent PR (Tasks 1-4). All fixes stay within existing module boundaries and contracts. Bug 2 is the largest (replaces naive n-gram fallback with structured markdown prompt + strict empty-on-failure semantics). Bugs 1/3/4 are surgical. NotificationContent/PhaseContext field contracts are preserved — only filling logic and validation gates change.

**Tech Stack:** Python 3.14, asyncio, pytest (asyncio_mode=auto), uv-managed deps, Click CLI, httpx/aiohttp. No new dependencies introduced.

---

## Project Conventions (apply to ALL tasks)

- Every module file starts with `from __future__ import annotations`
- Type hints required on all function signatures (leaf return types optional per AGENTS.md)
- `ruff check .` + `ruff format .` must pass (import order: stdlib → 3rd-party → local, rule I)
- `pyright .` must pass (strict mode for core/shared/platforms/run_check.py)
- `pytest -x` must pass (asyncio_mode=auto — `async def test_*` works without `@pytest.mark.asyncio`)
- Do NOT change existing error message text, emoji prefixes, or Rich color tags (they are external interfaces)
- New logging uses `logging.getLogger(__name__)` or `logging.getLogger("trawler.<sub>")`
- Branch per PR: `fix/<bug-short-name>`

## File Structure Overview

| Bug | Files Created | Files Modified |
|-----|---------------|----------------|
| 1 | `tests/test_token_renewal_validation.py` | `shared/auth/scheduler.py` |
| 2 | (none) | `core/summarizer.py`, `tests/test_summarizer.py` |
| 3 | (none) | `core/engine.py`, `platforms/bilibili/handlers.py`, `tests/test_engine.py` |
| 4 | (none) | `cron_run.sh`, `run_check.py` |

---

## Task 1: Fix XHS/Weibo Token Renewal False-Success (Bug 1)

**Problem:** `shared/auth/scheduler.py:check_and_renew_tokens` uses `new_tokens.obtained_at > tokens.obtained_at` as the "really refreshed" signal. But `XhsAuthenticator.refresh_tokens` (`platforms/xiaohongshu/auth.py:208-214`) and `WeiboAuthenticator.refresh_tokens` (`platforms/weibo/auth.py:285-291`) unconditionally set `obtained_at=now` even when the server returned no new cookies — so the scheduler writes back unchanged tokens and reports success. Bilibili is unaffected (it returns the original `tokens` object on no-op).

**Fix (user decision: Option B):** After `refresh_tokens` succeeds, call `authenticator.validate_tokens(new_tokens)` as a gate. If validation fails, do NOT write to config, log a warning, and return `RenewalResult(platform, "expired", ...)`. The validate-then-write boundary lives in `check_and_renew_tokens` (the single orchestration point) so both the cron path and the manual `token refresh` path are covered by the same gate.

**Files:**
- Modify: `shared/auth/scheduler.py:62-113` (the `check_and_renew_tokens` function body)
- Test: `tests/test_token_renewal_validation.py` (new file)

**Key constraint:** `BaseAuthenticator.validate_tokens` is already abstract (`shared/auth/base.py:72-73`) and implemented by all 3 platforms (bilibili/xhs/weibo). It returns `bool`. So no interface change needed.

### Task 1, Step 1: Write the failing test

Create `tests/test_token_renewal_validation.py`:

```python
"""Tests for shared.auth.scheduler — validate_tokens gate on renewal.

Covers Bug 1: XHS/Weibo refresh_tokens unconditionally set obtained_at=now
even when no real refresh happened; scheduler must validate before writing
config.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from shared.auth.base import PlatformTokens
from shared.auth.scheduler import check_and_renew_tokens
from shared.config import Config


def _make_config(platform: str) -> Config:
    """Build a minimal Config with a near-expiry token for the platform."""
    config = Config()
    now = time.time()
    if platform == "xhs":
        config.xiaohongshu.auth.cookie = "a1=x; webId=y"
        config.xiaohongshu.auth.expires_at = now + 2 * 86400  # within force_before
        config.xiaohongshu.enabled = True
    elif platform == "weibo":
        config.weibo.auth.cookie = "SUB=abc"
        config.weibo.auth.expires_at = now + 2 * 86400
        config.weibo.enabled = True
    elif platform == "bilibili":
        config.bilibili.auth.sessdata = "s"
        config.bilibili.auth.bili_jct = "j"
        config.bilibili.auth.expires_at = now + 2 * 86400
    return config


@pytest.mark.asyncio
async def test_xhs_renewal_validated_success_does_write(tmp_path):
    """When validate_tokens returns True after refresh, config is written."""
    config = _make_config("xhs")
    config_path = str(tmp_path / "config.toml")

    fake_new = PlatformTokens(
        platform="xhs",
        cookies={"a1": "x_new", "webId": "y_new"},
        obtained_at=time.time(),
        expires_at=time.time() + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.scheduler.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(return_value=True)

        result = await check_and_renew_tokens("xhs", config, config_path)

    assert result.action == "renewed"
    mock_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_xhs_renewal_validation_failure_skips_write(tmp_path):
    """Bug 1 regression: refresh returns tokens but validate_tokens=False →
    do NOT write config, return action='expired'."""
    config = _make_config("xhs")
    config_path = str(tmp_path / "config.toml")

    # XhsAuthenticator.refresh_tokens sets obtained_at=now unconditionally;
    # simulate that the server returned no new cookies → validate fails.
    fake_new = PlatformTokens(
        platform="xhs",
        cookies={"a1": "x"},  # unchanged
        obtained_at=time.time(),  # bumped (the bug)
        expires_at=time.time() + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.scheduler.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(return_value=False)

        result = await check_and_renew_tokens("xhs", config, config_path)

    assert result.action == "expired"
    mock_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_weibo_renewal_validation_failure_skips_write(tmp_path):
    """Same regression coverage for weibo."""
    config = _make_config("weibo")
    config_path = str(tmp_path / "config.toml")

    fake_new = PlatformTokens(
        platform="weibo",
        cookies={"SUB": "abc"},
        obtained_at=time.time(),
        expires_at=time.time() + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.scheduler.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(return_value=False)

        result = await check_and_renew_tokens("weibo", config, config_path)

    assert result.action == "expired"
    mock_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_tokens_exception_treated_as_failure(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """If validate_tokens raises, treat as validation failure (skip write,
    return 'expired') rather than crashing the whole check. The warning
    must be logged so operators can see the failure in production logs."""
    config = _make_config("xhs")
    config_path = str(tmp_path / "config.toml")

    fake_new = PlatformTokens(
        platform="xhs",
        cookies={"a1": "x"},
        obtained_at=time.time(),
        expires_at=time.time() + 7 * 86400,
    )

    with (
        patch("shared.auth.scheduler._get_authenticator_for_platform") as mock_auth,
        patch("shared.auth.scheduler.update_auth_section", new_callable=AsyncMock) as mock_write,
    ):
        auth = mock_auth.return_value
        auth.refresh_tokens = AsyncMock(return_value=fake_new)
        auth.validate_tokens = AsyncMock(side_effect=RuntimeError("network down"))

        with caplog.at_level("WARNING", logger="shared.auth.scheduler"):
            result = await check_and_renew_tokens("xhs", config, config_path)

    assert result.action == "expired"
    mock_write.assert_not_awaited()
    # MINOR-9: warning must be recorded so the failure is visible in logs
    assert any("校验异常" in r.message or "校验失败" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails (pre-fix behavior)**

Run: `uv run pytest tests/test_token_renewal_validation.py -v`

Expected: `test_xhs_renewal_validation_failure_skips_write` and `test_weibo_renewal_validation_failure_skips_write` FAIL because the current scheduler writes config whenever `obtained_at` advanced (the bug). `test_xhs_renewal_validated_success_does_write` may fail too because `validate_tokens` isn't called yet (the mock isn't exercised — but the assertion is on `update_auth_section` being awaited, which currently happens, so it should pass; that's fine).

- [ ] **Step 3: Implement the validate_tokens gate in scheduler**

Modify `shared/auth/scheduler.py` — replace lines 95-113 (the `try: new_tokens = await authenticator.refresh_tokens(...)` block through the `except` clause) with:

```python
    try:
        new_tokens = await authenticator.refresh_tokens(tokens)

        # 检查是否真的刷新了（obtained_at 更新说明 refresh_tokens 返回了新 tokens）
        if new_tokens.obtained_at <= tokens.obtained_at:
            logger.info("%s token 无需续期 (refresh_tokens 返回原始 tokens)", platform)
            return RenewalResult(platform, "skipped", f"{platform}: token 无需续期")

        # Bug 1 fix: XHS/Weibo refresh_tokens 无条件 bump obtained_at，
        # 必须用 validate_tokens 二次校验服务端是否真正接受了新 cookie，
        # 避免把无效凭证写回 config 造成"乐观成功"。
        try:
            is_valid = await authenticator.validate_tokens(new_tokens)
        except Exception as ve:
            logger.warning(
                "%s token 续期后校验异常: %s — 视为续期失败，不写回 config", platform, ve
            )
            return RenewalResult(
                platform, "expired", f"{platform}: token 续期后校验异常 ({ve})"
            )

        if not is_valid:
            logger.warning(
                "%s token 续期后 validate_tokens=False — 服务端未接受新 cookie，不写回 config",
                platform,
            )
            return RenewalResult(
                platform, "expired", f"{platform}: token 续期后校验失败"
            )

        from shared.auth import update_auth_section

        await _update_last_refresh_at(platform, config, new_tokens.obtained_at, config_path)
        auth_dict = _tokens_to_auth_dict(platform, new_tokens, authenticator)
        await update_auth_section(platform, auth_dict, config_path=config_path)
        _update_config_memory(platform, config, new_tokens, authenticator)
        logger.info("%s token 续期成功", platform)
        return RenewalResult(platform, "renewed", f"{platform}: token 续期成功")
    except Exception as e:
        logger.warning("%s token 续期失败: %s", platform, e)
        return RenewalResult(platform, "expired", f"{platform}: token 续期失败 ({e})")
```

Notes on this implementation:
- The old `if new_tokens.obtained_at > tokens.obtained_at:` branch is split: first the early-out for true no-ops (bilibili path), then the new validate gate.
- The validate gate runs for ALL platforms (bilibili included) — bilibili's `validate_tokens` calls `cred.check_valid()` which is cheap and adds defense-in-depth; it does not regress bilibili because bilibili only reaches here when `check_refresh()` already returned `need=True`.
- Exception during validate → treated as failure (skip write, return expired) so a flaky network doesn't corrupt config.

- [ ] **Step 4: Run tests to verify the gate works**

Run: `uv run pytest tests/test_token_renewal_validation.py -v`

Expected: All 4 tests PASS.

- [ ] **Step 5: Run regression on existing scheduler + pipeline tests**

Run: `uv run pytest tests/test_scheduler.py tests/test_pipeline_concurrent.py -v`

Expected: All PASS. `test_pipeline_concurrent.py` monkeypatches `check_and_renew_tokens` to a no-op so it's unaffected. `test_scheduler.py` only tests the pure `should_renew` function.

- [ ] **Step 6: Lint + type check + full test suite**

Run: `uv run ruff check shared/auth/scheduler.py tests/test_token_renewal_validation.py && uv run ruff format shared/auth/scheduler.py tests/test_token_renewal_validation.py && uv run pyright shared/auth/scheduler.py && uv run pytest -x`

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git checkout -b fix/token-renewal-validation-gate
git add shared/auth/scheduler.py tests/test_token_renewal_validation.py
git commit -m "fix(auth): gate token renewal writeback on validate_tokens

XHS/Weibo refresh_tokens unconditionally bump obtained_at even when the
server returned no new cookies, causing the scheduler to write unchanged
tokens back to config and report false success. Add validate_tokens gate
after refresh: if validation fails (or raises), skip config write and
return RenewalResult(action='expired')."
```

- [ ] **Step 8: Push + PR**

```bash
git push -u origin fix/token-renewal-validation-gate
gh pr create --title "fix(auth): gate token renewal writeback on validate_tokens" --body "Fixes Bug 1. After refresh_tokens succeeds, validate the new tokens against the platform before writing back to config. XHS/Weibo were reporting false renewal success because their refresh_tokens bumps obtained_at unconditionally." --base master
```

Wait 5-15 min for Qodo review, then follow the PR-review polling workflow from AGENTS.md.

---

## Task 2: Restructure AI Summary/Keywords Extraction (Bug 2)

**Problem:** `core/summarizer.py` has three issues:
1. `_extract_keywords_local` (lines 444-488) uses naive character n-grams → heavily overlapping keywords ("创作者", "作者", "创作").
2. `LocalFallbackProvider._extract_summary` (lines 225-259) uses the same n-gram density scoring — mediocre quality and DRY-violating.
3. `extract_keywords` line 438 logs AI failure at `debug` level (invisible in production logs) → silent degradation.

**Fix (user decision — full restructure):** Give the AI a single structured markdown template so it outputs all artifacts (summary + keywords + one-line summary + tags) in one call. Replace `generate_summary` + `extract_keywords` with a single `analyze_content` function. Keep the two old public function names as thin wrappers so `platforms/bilibili/handlers.py` callers don't need to change. Delete the n-gram fallback entirely; on AI failure, log at `warning` and return explicit empty values (`""` summary, `[]` keywords).

**Contract preservation:** `NotificationContent.summary` and `.keywords` fields are unchanged. `summarize_phase` handler (`platforms/bilibili/handlers.py:170-227`) keeps calling `generate_summary` and `extract_keywords` — but now both pull from a single cached AI call on the `PhaseContext`.

**Files:**
- Modify: `core/summarizer.py` (full rewrite of the analysis section; keep `OpenAIProvider`, `create_provider`, drop `LocalFallbackProvider`)
- Modify: `tests/test_summarizer.py` (replace `TestLocalFallbackProvider`; keep `TestCreateProvider`; add template-parse + failure-mode tests)
- Modify: `platforms/bilibili/handlers.py:170-227` (`summarize_phase`) to cache the AI result on `ctx` instead of calling AI twice

### Template + Prompt Design

The markdown template the AI must fill (shown to it inside the prompt):

```
## 摘要
{分点详细总结，覆盖所有重要观点}

## 一句话总结
{单句概括，≤40 字}

## 关键词
{3-5 个关键词，中文分号；分隔}

## 标签
{0-3 个内容类型标签，如 教程/评测/Vlog，逗号分隔}
```

The full prompt template (replaces both `_SUMMARY_PROMPT_TEMPLATE` and `_KEYWORDS_PROMPT_TEMPLATE`):

```
你是内容分析助手。请阅读以下内容，严格按下面的 Markdown 格式输出分析结果，
不要输出任何额外说明或前后缀。每个字段必须以指定标题开头（## 摘要 / ## 一句话总结 /
## 关键词 / ## 标签）。如果某字段无法填写，输出该标题并留空内容。

输出格式（必须严格遵循）：

## 摘要
（分点详细总结，覆盖所有重要观点，使用「- 」开头列出要点）

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
正文：{text}
```

Parsing must be robust to: (a) the AI wrapping output in ```markdown ... ``` fences, (b) a missing section (return `""` / `[]` for it), (c) extra blank lines.

### Task 2, Step 1: Write failing tests for the new parser + analyze_content

**MAJOR-7 critical:** `tests/test_summarizer.py` line 7 currently reads `from core.summarizer import LocalFallbackProvider, OpenAIProvider, create_provider`. Once Task 2 Step 3 deletes `LocalFallbackProvider` from `core/summarizer.py`, this line raises `ImportError` and the whole test module fails to collect. The line MUST be replaced (not just appended to). The existing `TestCreateProvider` class still uses `AnalysisConfig` (it constructs `AnalysisConfig(...)` directly), so `AnalysisConfig` must stay imported.

Replace the entire import block at the top of `tests/test_summarizer.py` (current lines 1-8) with:

```python
"""Tests for core.summarizer — provider factory, parser, and analyze_content."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.summarizer import (
    OpenAIProvider,
    analyze_content,
    create_provider,
    parse_markdown_analysis,
)
from shared.config import AnalysisConfig, Config
```

Notes on this import block:
- `LocalFallbackProvider` is removed (class deleted in Step 3).
- `AnalysisConfig` is kept because `TestCreateProvider` (lines 14-68, unchanged) still constructs `AnalysisConfig(...)` instances directly.
- `Config` is added because the new `TestAnalyzeContent` / `TestLegacyWrappersReturnEmptyOnFailure` classes drive behavior through `config.analysis.*` (the new public entrypoint `analyze_content` takes a `Config`, not an `AnalysisConfig`).
- `time`, `AsyncMock`, `patch` are used by the new async tests.
- `parse_markdown_analysis` and `analyze_content` are the new public API under test.
- `pytest` stays for `@pytest.mark.asyncio` and `pytest.raises` / `pytest.LogCaptureFixture`.

Then replace the `TestLocalFallbackProvider` class (current lines 71-83) with the new classes below. Keep `TestCreateProvider` unchanged.

```python
class TestParseMarkdownAnalysis:
    """Tests for parse_markdown_analysis — robust parsing of AI output."""

    def test_parse_well_formed(self) -> None:
        raw = """## 摘要
- 第一点
- 第二点

## 一句话总结
这是个测试视频

## 关键词
Python；异步；测试

## 标签
教程, 评测"""
        result = parse_markdown_analysis(raw)
        assert "第一点" in result.summary
        assert "第二点" in result.summary
        assert result.one_line_summary == "这是个测试视频"
        assert result.keywords == ["Python", "异步", "测试"]
        assert result.tags == ["教程", "评测"]

    def test_parse_strips_markdown_code_fence(self) -> None:
        raw = """```markdown
## 摘要
内容

## 一句话总结
一句话

## 关键词
A；B

## 标签
```
```"""
        result = parse_markdown_analysis(raw)
        assert "内容" in result.summary
        assert result.keywords == ["A", "B"]
        assert result.tags == []

    def test_parse_missing_section_returns_empty(self) -> None:
        raw = """## 摘要
只有摘要

## 关键词
A"""
        result = parse_markdown_analysis(raw)
        assert "只有摘要" in result.summary
        assert result.one_line_summary == ""
        assert result.keywords == ["A"]
        assert result.tags == []

    def test_parse_keywords_with_mixed_separators(self) -> None:
        raw = """## 摘要
x

## 关键词
A；B,C；D
"""
        result = parse_markdown_analysis(raw)
        assert result.keywords == ["A", "B", "C", "D"]


class TestAnalyzeContent:
    """Tests for analyze_content — AI orchestration + failure semantics."""

    @pytest.mark.asyncio
    async def test_analyze_content_success_caches_on_result(self) -> None:
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"
        config.analysis.api_key = "k"

        ai_output = """## 摘要
- 要点一

## 一句话总结
一句话

## 关键词
A；B

## 标签
教程"""

        with patch("core.summarizer.create_provider") as mock_cp:
            provider = mock_cp.return_value
            provider.generate = AsyncMock(return_value=ai_output)

            result = await analyze_content(
                source_id="bili:BV1",
                title="T",
                author="A",
                text="正文内容",
                config=config,
            )

        assert "要点一" in result.summary
        assert result.keywords == ["A", "B"]
        assert result.is_ai is True

    @pytest.mark.asyncio
    async def test_analyze_content_ai_failure_returns_empty_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        with patch("core.summarizer.create_provider") as mock_cp:
            provider = mock_cp.return_value
            provider.generate = AsyncMock(side_effect=RuntimeError("API timeout"))

            with caplog.at_level("WARNING", logger="core.summarizer"):
                result = await analyze_content(
                    source_id="bili:BV1",
                    title="T",
                    author="A",
                    text="正文",
                    config=config,
                )

        assert result.summary == ""
        assert result.keywords == []
        assert result.is_ai is False
        # Bug 2 requirement: failure logged at WARNING (not DEBUG)
        assert any("AI 分析失败" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_analyze_content_disabled_ai_returns_empty(self) -> None:
        config = Config()
        config.analysis.enabled = False

        result = await analyze_content(
            source_id="x", title="t", author="a", text="txt", config=config
        )
        assert result.summary == ""
        assert result.keywords == []
        assert result.is_ai is False

    @pytest.mark.asyncio
    async def test_analyze_content_empty_text_returns_empty(self) -> None:
        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        result = await analyze_content(
            source_id="x", title="t", author="a", text="", config=config
        )
        assert result.summary == ""
        assert result.is_ai is False


class TestLegacyWrappersReturnEmptyOnFailure:
    """generate_summary and extract_keywords must keep their old signatures
    but now delegate to analyze_content. On AI failure they return empty
    values (not n-gram fallback)."""

    @pytest.mark.asyncio
    async def test_generate_summary_failure_returns_empty(self) -> None:
        from core.summarizer import generate_summary

        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        with patch("core.summarizer.create_provider") as mock_cp:
            mock_cp.return_value.generate = AsyncMock(side_effect=RuntimeError("fail"))
            summary, source, is_ai = await generate_summary(
                source_id="x", title="t", author="a", text="正文", config=config
            )
        assert summary == ""
        assert is_ai is False

    @pytest.mark.asyncio
    async def test_extract_keywords_failure_returns_empty_list(self) -> None:
        from core.summarizer import extract_keywords

        config = Config()
        config.analysis.enabled = True
        config.analysis.provider = "openai"
        config.analysis.api_base = "https://example.com/v1"

        with patch("core.summarizer.create_provider") as mock_cp:
            mock_cp.return_value.generate = AsyncMock(side_effect=RuntimeError("fail"))
            kws = await extract_keywords(text="正文", title="t", author="a", config=config)
        assert kws == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_summarizer.py -v`

Expected: ImportError on `analyze_content` / `parse_markdown_analysis`; `TestLocalFallbackProvider` is gone so those tests don't exist. Many failures — this is expected (pre-implementation).

- [ ] **Step 3: Rewrite `core/summarizer.py`**

Replace the entire file content with the implementation below. **Keep `OpenAIProvider` (lines 36-105) unchanged** — it's reused. Delete `LocalFallbackProvider` and the n-gram helpers. The new file:

```python
"""AI 内容分析模块 — 一次性输出摘要/关键词/标签/一句话总结。

设计要点（Bug 2 重构）：
- 单次 AI 调用产出全部结构化字段，用 Markdown 模板约束输出格式。
- 解析层鲁棒：容忍 ```markdown fence、字段缺失、混合分隔符。
- AI 失败时返回明确的空值（summary='' / keywords=[]）并记 WARNING，
  不再静默降级到本地 n-gram（旧实现质量差且掩盖故障）。
- ``generate_summary`` / ``extract_keywords`` 作为薄包装保留旧签名，
  内部委托给 ``analyze_content``，避免调用方大改。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from shared.config import AnalysisConfig, Config
from shared.constants import LLM_API_TIMEOUT
from shared.protocols import LLMProvider

logger = logging.getLogger(__name__)

# ── Prompt 模板 ──────────────────────────────────────────────────

_ANALYSIS_PROMPT_TEMPLATE = """\
你是内容分析助手。请阅读以下内容，严格按下面的 Markdown 格式输出分析结果，\
不要输出任何额外说明或前后缀。每个字段必须以指定标题开头（## 摘要 / ## 一句话总结 / \
## 关键词 / ## 标签）。如果某字段无法填写，输出该标题并留空内容。

输出格式（必须严格遵循）：

## 摘要
（分点详细总结，覆盖所有重要观点，使用「- 」开头列出要点）

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


# ── 解析层 ───────────────────────────────────────────────────────

# 字段标题 → 输出 key。AI 偶尔会用 # 单井号或全角 ＃，正则统一兼容。
_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"^#{1,3}\s*摘要\s*$", re.MULTILINE),
    "one_line_summary": re.compile(r"^#{1,3}\s*一句话总结\s*$", re.MULTILINE),
    "keywords": re.compile(r"^#{1,3}\s*关键词\s*$", re.MULTILINE),
    "tags": re.compile(r"^#{1,3}\s*标签\s*$", re.MULTILINE),
}


@dataclass
class AnalysisResult:
    """``analyze_content`` 的结构化结果。"""

    summary: str = ""
    one_line_summary: str = ""
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    is_ai: bool = False
    source: str = "none"  # provider name | "none" | "empty"


def _strip_code_fence(text: str) -> str:
    """剥离包裹整段输出的 ```markdown ... ``` 代码围栏。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        # 去掉首行（可能含语言标识）和末行 ```
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def _extract_section(text: str, pattern: re.Pattern[str]) -> str:
    """提取某个 ## 标题下的内容，直到下一个 ## 标题或文本结束。"""
    match = pattern.search(text)
    if match is None:
        return ""
    start = match.end()
    # 下一节以行首 1-3 个 # 开头
    next_section = re.search(r"^#{1,3}\s*\S", text[start:], re.MULTILINE)
    if next_section is None:
        body = text[start:]
    else:
        body = text[start : start + next_section.start()]
    return body.strip()


def _parse_list_field(body: str) -> list[str]:
    """解析分号/逗号分隔的列表字段，过滤空项。

    兼容中文分号「；」、英文分号「;」、中英文逗号「,，」、换行。
    MINOR-8: 分隔符都是固定字面量（无正则元字符），无需 ``re.escape``。
    """
    if not body:
        return []
    parts = re.split(r"[；;,，\n]+", body)
    return [p.strip() for p in parts if p.strip()]


def parse_markdown_analysis(raw: str) -> AnalysisResult:
    """将 AI 输出的 Markdown 解析为 ``AnalysisResult``。

    鲁棒性：
    - 自动剥离 ```markdown fence
    - 缺失字段填空值
    - 关键词/标签用混合分隔符拆分
    """
    text = _strip_code_fence(raw)
    return AnalysisResult(
        summary=_extract_section(text, _SECTION_PATTERNS["summary"]),
        one_line_summary=_extract_section(text, _SECTION_PATTERNS["one_line_summary"]),
        keywords=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["keywords"]))[:5],
        tags=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["tags"]))[:3],
    )


# ── OpenAI 兼容 Provider（保持原样） ─────────────────────────────


class OpenAIProvider:
    """OpenAI 兼容 API 提供商。

    支持任何 OpenAI 兼容的 API 端点（如 OpenAI、DeepSeek、本地 Ollama 等）。
    使用 requests 库直接调用 API，避免额外依赖。
    """

    def __init__(
        self,
        api_base: str,
        api_key: str = "",
        model_name: str = "gpt-4o-mini",
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name

    async def generate(self, prompt: str) -> str:
        import httpx

        url = f"{self.api_base}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2048,
        }

        logger.debug("调用 OpenAI 兼容 API (model=%s)...", self.model_name)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url, json=payload, headers=headers, timeout=LLM_API_TIMEOUT
                )
        except httpx.TimeoutException:
            raise RuntimeError(f"OpenAI API 调用超时 ({LLM_API_TIMEOUT}s)")
        except httpx.ConnectError:
            raise RuntimeError(f"无法连接到 API: {self.api_base}")

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise RuntimeError(
                f"API 返回错误 ({response.status_code}): {response.text[:200]}"
            )

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"解析 API 响应失败: {e}")


# ── 公共接口 ─────────────────────────────────────────────────────


def create_provider(config: AnalysisConfig) -> LLMProvider:
    """根据配置创建 LLM 提供商。

    Raises:
        ValueError: 不支持的 provider 类型
    """
    provider_name = config.provider.lower().strip()

    if provider_name == "openai":
        if not config.api_base:
            raise ValueError("OpenAI provider 需要配置 api_base")
        return OpenAIProvider(
            api_base=config.api_base,
            api_key=config.api_key,
            model_name=config.model_name or "gpt-4o-mini",
        )
    elif provider_name == "ollama":
        return OpenAIProvider(
            api_base=config.api_base or "http://localhost:11434/v1",
            api_key=config.api_key or "ollama",
            model_name=config.model_name or "qwen2.5:7b",
        )
    else:
        raise ValueError(f"不支持的 provider: {config.provider}")


async def analyze_content(
    source_id: str,
    title: str,
    author: str,
    text: str,
    config: Config,
) -> AnalysisResult:
    """一次性产出摘要/关键词/标签/一句话总结（Bug 2 重构入口）。

    AI 失败时返回空字段（summary='' / keywords=[]）并记 WARNING，
    不再静默降级到本地 n-gram。

    Args:
        source_id: 来源标识（仅用于日志）
        title/author/text: 待分析内容
        config: 全局配置

    Returns:
        AnalysisResult（失败时 is_ai=False，字段为空）
    """
    if not config.analysis.enabled:
        logger.debug("AI 分析已禁用，返回空结果: %s", source_id)
        return AnalysisResult(source="none")

    if not text.strip():
        return AnalysisResult(source="empty")

    try:
        provider = create_provider(config.analysis)
        prompt = _ANALYSIS_PROMPT_TEMPLATE.format(title=title, author=author, text=text)
        raw = await provider.generate(prompt)
        result = parse_markdown_analysis(raw)
        result.is_ai = True
        result.source = config.analysis.provider
        logger.info("AI 内容分析成功: %s", source_id)
        return result
    except Exception as e:
        # Bug 2: WARNING（非 DEBUG），让运维可见
        logger.warning("AI 内容分析失败 (%s): %s，返回空结果", source_id, e)
        return AnalysisResult(source="none")


# ── 旧签名薄包装（保持 handlers 调用方不变） ─────────────────────


async def generate_summary(
    source_id: str,
    title: str,
    author: str,
    text: str,
    config: Config,
) -> tuple[str, str, bool]:
    """旧签名包装：返回 (summary, source, is_ai)。

    内部委托 ``analyze_content``，失败时 summary='' / is_ai=False。
    保留签名以兼容 ``platforms/bilibili/handlers.py:summarize_phase``。
    """
    result = await analyze_content(source_id, title, author, text, config)
    return result.summary, result.source, result.is_ai


async def extract_keywords(
    text: str,
    title: str,
    author: str,
    config: Config | None = None,
) -> list[str]:
    """旧签名包装：返回关键词列表。

    内部委托 ``analyze_content``，失败时返回 []。
    保留签名以兼容 ``platforms/bilibili/handlers.py:summarize_phase``。

    注意：调用方若已先调 ``generate_summary``，本函数会再发一次 AI 请求。
    推荐新代码直接调 ``analyze_content`` 复用结果（见 Task 2 Step 5
    对 summarize_phase 的改造）。
    """
    if config is None or not config.analysis.enabled:
        return []
    result = await analyze_content("keywords", title, author, text, config)
    return result.keywords
```

- [ ] **Step 4: Run the new tests to verify parser + analyze_content**

Run: `uv run pytest tests/test_summarizer.py -v`

Expected: All tests in `TestParseMarkdownAnalysis`, `TestAnalyzeContent`, `TestLegacyWrappersReturnEmptyOnFailure`, and the existing `TestCreateProvider` PASS.

- [ ] **Step 5: Optimize `summarize_phase` to make a single AI call**

Currently `platforms/bilibili/handlers.py:170-227` calls `generate_summary` then `extract_keywords` — two AI calls. Replace with a single `analyze_content` call cached on the `PhaseContext`. This halves AI cost and guarantees summary/keywords come from the same pass.

Modify `platforms/bilibili/handlers.py`:

1. Update the import (line 15):

```python
from core.summarizer import analyze_content
```

2. Replace the `try: summary_text, ...` and `try: ctx.keywords = ...` blocks (lines 203-225) with:

```python
    text_to_summarize = ctx.transcript_text or ctx.content_text
    # 如果消息附带动态内容（动态-视频去重场景），拼到摘要输入文本前面，
    # 让 LLM 在摘要时一并考虑 UP 主在动态里的补充说明。
    if ctx.msg.dynamic_text:
        text_to_summarize = f"【动态内容】{ctx.msg.dynamic_text}\n\n{text_to_summarize}"

    try:
        analysis = await analyze_content(
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
            text=text_to_summarize,
            config=ctx.config,
        )
        ctx.summary_text = analysis.summary
        ctx.keywords = analysis.keywords
    except Exception as exc:
        # analyze_content 内部已吞异常并返回空结果；这里兜底防极端情况
        logger.error("✗ 摘要/关键词生成失败: %s", exc)
        logger.exception("Analysis failed for %s", source_id)
```

Note: `analyze_content` already catches its own AI exceptions and returns empty `AnalysisResult`, so the outer `try` here is purely defensive (e.g. a bug in the parsing layer). `ctx.keywords` defaults to `[]` already (`PhaseContext.keywords: list[str] = field(default_factory=list)`) so no further init needed.

- [ ] **Step 6: Run handler-affected tests + regression**

Run: `uv run pytest tests/test_summarizer.py tests/test_platform_handlers.py tests/test_engine.py tests/test_pipeline_concurrent.py -v`

Expected: All PASS. `test_platform_handlers.py` only checks decorator registration (unchanged). `test_pipeline_concurrent.py` monkeypatches `check_and_renew_tokens` and the AI is mocked/absent in those tests.

- [ ] **Step 7: Lint + type check + full suite**

Run: `uv run ruff check core/summarizer.py platforms/bilibili/handlers.py tests/test_summarizer.py && uv run ruff format core/summarizer.py platforms/bilibili/handlers.py tests/test_summarizer.py && uv run pyright core/summarizer.py && uv run pytest -x`

Expected: clean. Note: pyright is configured `exclude = ["tests"]` so test files aren't type-checked.

- [ ] **Step 8: Commit**

```bash
git checkout -b refactor/summarizer-structured-markdown
git add core/summarizer.py platforms/bilibili/handlers.py tests/test_summarizer.py
git commit -m "refactor(summarizer): single AI call with markdown template

Replace overlapping n-gram keyword/summary fallback with a single AI call
that returns structured markdown (summary / one_line_summary / keywords /
tags). Parser tolerates code fences, missing sections, and mixed
separators. AI failure now logs WARNING and returns empty values instead
of silently degrading to low-quality n-gram output. summarize_phase makes
one AI call instead of two."
```

- [ ] **Step 9: Push + PR**

```bash
git push -u origin refactor/summarizer-structured-markdown
gh pr create --title "refactor(summarizer): single AI call with structured markdown template" --body "Fixes Bug 2. Replaces naive n-gram keyword/summary fallback (overlapping output, silent debug-level degradation) with a single AI call producing structured markdown. Parser is robust to fences/missing fields. AI failure logs WARNING and returns empty values. summarize_phase now makes one AI call instead of two." --base master
```

Follow PR-review polling workflow.

---

## Task 3: Cross-Process State Recovery + Eliminate Silent Transcribe Success (Bug 3)

**Problem:** Two coupled issues:
1. **Cross-process loss:** `shared/message_store.py` persists only `MessageRecord` fields (title/author/phase/error/...). `PhaseContext` artifacts (`downloaded_filepath`, `summary_text`, `keywords`) live only in memory. When cron runs `check` in a fresh process and finds a `VIDEO` message at `phase=DOWNLOADED` (written by a previous run that crashed before `save()` of the next phase), the new `PhaseContext` has `downloaded_filepath=None` → transcribe phase silently no-ops.
2. **Silent success:** `platforms/bilibili/handlers.py:131-164` `transcribe_phase` returns `True` even when `filepath is None` (line 138-140: logs a warning, returns True) — masking the missing-file state as success, so the message advances to SUMMARIZED with empty transcript and pushes a low-quality notification.

**Fix (user decisions: 3=C auto-recover, 4=B eliminate silent success):**
- In `PipelineEngine.process_message`, when starting a `VIDEO` message whose next phase is `TRANSCRIBED`/`SUMMARIZED`/`PUSHED` but `ctx.downloaded_filepath is None`, auto-rewind to `Phase.DISCOVERED` (re-download). This is a recovery gate, not a persist-change — `PhaseContext` stays ephemeral; the gate just notices stale phase and re-runs the pipeline from download.
- In `transcribe_phase`, when `filepath is None` at entry, set `ctx.error = "downloaded_filepath missing"` and `return False` — the message stays at its current phase with an error recorded (visible in dashboard), no silent push. When `transcribe_file_async` raises a real exception, log WARNING and **continue the flow on `content_text`** (return True) — this is the existing graceful-degradation behavior, preserved.

**Files:**
- Modify: `core/engine.py:89-129` (`process_message`)
- Modify: `platforms/bilibili/handlers.py:131-164` (`transcribe_phase`)
- Modify: `tests/test_engine.py` (add auto-rewind tests)

### Task 3, Step 1: Write failing test for the auto-rewind recovery

Append to `tests/test_engine.py` (after `test_run_platform_detect_and_process`):

```python
@pytest.mark.asyncio
async def test_process_message_video_missing_filepath_rewinds_to_discovered(
    config: Config, store: MessageStore
) -> None:
    """Bug 3 fix: a VIDEO message stuck at DOWNLOADED with no filepath
    (cross-process state loss) should auto-rewind to DISCOVERED and re-run
    the full download phase."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.DOWNLOADED)
    async def dl(ctx: PhaseContext) -> bool:
        calls.append("downloaded")
        ctx.downloaded_filepath = Path("/tmp/fake_video.mp4")  # simulate real download
        return True

    @PipelineEngine.register("bili", Phase.TRANSCRIBED)
    async def tr(ctx: PhaseContext) -> bool:
        calls.append("transcribed")
        # Filepath is now set by re-download
        assert ctx.downloaded_filepath is not None
        return True

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        calls.append("summarized")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    # Seed a message stuck at DOWNLOADED (phase persisted, filepath lost)
    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.DOWNLOADED)
    msg = store.get_message("bili:BV1")
    assert msg is not None

    # New PhaseContext starts with downloaded_filepath=None (the bug scenario)
    await PipelineEngine.process_message(msg, config, store)

    # Auto-rewind should have re-run DOWNLOADED then proceeded
    assert "downloaded" in calls
    assert calls == ["downloaded", "transcribed", "summarized", "pushed"]
    updated = store.get_message("bili:BV1")
    assert updated is not None
    assert updated.phase == Phase.PUSHED
```

**BLOCKER-1 decision — no separate "no-rewind" test:** an earlier draft of this plan included a `test_process_message_video_with_filepath_does_not_rewind` sanity test. It was removed because it contradicts the implementation: the new rewind gate condition is `content_type == VIDEO AND phase != DISCOVERED AND ctx.downloaded_filepath is None`, and a freshly-constructed `PhaseContext` in a new test process always starts with `downloaded_filepath = None` (the field's dataclass default). There is no public hook to pre-populate `ctx.downloaded_filepath` before `process_message` constructs its own `ctx` internally, so any "filepath already set" scenario would require monkeypatching the engine's internal `PhaseContext(...)` call — brittle and not worth the coupling. The no-rewind path is implicitly covered by the existing `test_process_message_resume_from_mid_phase` (test_engine.py:150-175), which seeds a message at `phase=DOWNLOADED` and asserts only downstream phases run. See Step 3 for the required update to that existing test.

**Pre-existing test regression (must fix in Step 3):** the existing `test_process_message_resume_from_mid_phase` (test_engine.py:150-175) sets up exactly the gate-trigger scenario — `ContentType.VIDEO` at `phase=DOWNLOADED` with no filepath — but asserts `calls == ["transcribed"]`. Once the rewind gate lands, the gate will fire and `downloaded` will be appended to `calls`, breaking that assertion. The fix is in Step 3: switch that test to a `TEXT` message (TEXT content has no DOWNLOADED→TRANSCRIBED phases and never trips the VIDEO-only gate), preserving its original intent ("resume from current phase, do not repeat completed phases") without colliding with the new recovery semantics.

- [ ] **Step 2: Run tests to verify the rewind test fails**

Run: `uv run pytest tests/test_engine.py::test_process_message_video_missing_filepath_rewinds_to_discovered -v`

Expected: FAIL — `calls == ["transcribed", "summarized", "pushed"]` (no `downloaded`) because current `process_message` doesn't rewind; the `transcribed` handler asserts `ctx.downloaded_filepath is not None` and fails. (The deleted no-rewind sanity test no longer runs.)

- [ ] **Step 3: Implement the auto-rewind gate in `process_message`**

Modify `core/engine.py`. **BLOCKER-2: two import edits required before the body change**, otherwise ruff F811 (redefinition of unused `MessageRecord`) fires because the function-body code below previously declared `from shared.protocols import MessageRecord` locally at line 101.

**3a. Promote imports to module top-level (engine.py line 22):**

Current line 22:
```python
from shared.protocols import PHASE_FLOW, Phase, PhaseContext
```

Replace with:
```python
from shared.protocols import PHASE_FLOW, ContentType, MessageRecord, Phase, PhaseContext
```

This adds `ContentType` (used by the new gate) and `MessageRecord` (previously imported locally) to the top-level import. Alphabetical order within the import is preserved (C < M < P, with `PHASE_FLOW` first as the all-caps name — matches existing convention).

**3b. Delete the function-local import (engine.py line 101):**

Delete this line from inside `process_message`:
```python
        from shared.protocols import MessageRecord
```

The function body now uses the top-level `MessageRecord` directly. With both edits, ruff sees exactly one binding for `MessageRecord` and `ContentType` — no F811.

**3c. Replace the `process_message` body (lines 102-129, i.e. starting right after the signature/docstring):**

```python
        assert isinstance(msg, MessageRecord), f"expected MessageRecord, got {type(msg)}"
        ctx = PhaseContext(msg=msg, config=config)
        phases = PHASE_FLOW[msg.content_type]

        # Bug 3 fix: cross-process state recovery. MessageStore only persists
        # MessageRecord fields, so a VIDEO message that crashed after marking
        # DOWNLOADED but before the next save() loses ctx.downloaded_filepath
        # in the next cron process. If a VIDEO message resumes at DOWNLOADED or
        # later without a filepath, rewind to DISCOVERED so the download phase
        # re-runs and produces the filepath again.
        if (
            msg.content_type == ContentType.VIDEO
            and msg.phase != Phase.DISCOVERED
            and ctx.downloaded_filepath is None
        ):
            logger.warning(
                "▶ %s:%s 处于 %s 阶段但 downloaded_filepath 缺失（跨进程状态丢失），"
                "回退到 DISCOVERED 重新下载",
                msg.platform,
                msg.msg_id,
                msg.phase.name,
            )
            msg.phase = Phase.DISCOVERED
            ctx.msg.phase = Phase.DISCOVERED
            store.mark_phase(msg.msg_id, Phase.DISCOVERED)
            store.save()

        start_idx = phases.index(msg.phase)
        logger.info("▶ 处理消息 %s:%s (%s)", msg.platform, msg.msg_id, msg.title)
        for next_phase in phases[start_idx + 1 :]:
            handler = cls._handlers.get((msg.platform, next_phase))
            if handler is None:
                handler = cls._handlers.get(("*", next_phase))
            if handler is None:
                logger.error("No handler for %s / %s — stopping", msg.platform, next_phase)
                ctx.error = f"missing handler: {msg.platform}/{next_phase.name}"
                store.mark_error(msg.msg_id, ctx.error)
                store.save()
                break

            success = await handler(ctx)
            if not success:
                store.mark_error(msg.msg_id, ctx.error)
                store.save()
                break

            msg.phase = next_phase
            store.mark_phase(msg.msg_id, next_phase)
            logger.info("%s:%s → %s ✓", msg.platform, msg.msg_id, next_phase.name)
            store.save()
```

**3d. Update the pre-existing `test_process_message_resume_from_mid_phase` (tests/test_engine.py:150-175) to avoid colliding with the new gate.**

This existing test currently uses `ContentType.VIDEO` at `Phase.DOWNLOADED` with no filepath and asserts `calls == ["transcribed"]` — exactly the scenario the new gate now rewinds on. Without this update, the new gate makes the assertion fail (`downloaded` would also appear). The test's original intent ("resume from current phase, don't repeat completed phases") is preserved by switching to `ContentType.TEXT`, whose phase flow does not include DOWNLOADED/TRANSCRIBED and is never touched by the VIDEO-only gate.

Replace its body (`tests/test_engine.py:150-175`) with:

```python
@pytest.mark.asyncio
async def test_process_message_resume_from_mid_phase(config: Config, store: MessageStore) -> None:
    """Should resume from current phase, not repeat completed phases.

    Uses TEXT content: TEXT phase flow excludes DOWNLOADED/TRANSCRIBED, so the
    Bug-3 VIDEO-only rewind gate never fires here and this test keeps verifying
    the pure resume semantics."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    calls: list[str] = []

    @PipelineEngine.register("bili", Phase.SUMMARIZED)
    async def sm(ctx: PhaseContext) -> bool:
        calls.append("summarized")
        return True

    @PipelineEngine.register("bili", Phase.PUSHED)
    async def ps(ctx: PhaseContext) -> bool:
        calls.append("pushed")
        return True

    msg = store.add_new("bili:BV1", "bili", ContentType.TEXT, 2000000000, "Test", "Author")
    assert msg is not None
    store.mark_phase("bili:BV1", Phase.SUMMARIZED)
    msg = store.get_message("bili:BV1")
    assert msg is not None
    assert msg.phase == Phase.SUMMARIZED

    await PipelineEngine.process_message(msg, config, store)
    assert calls == ["pushed"]  # only pushed, summarized is not repeated
```

This keeps the assertion semantics ("only the next phase runs, completed phases are not repeated") while sidestepping the VIDEO rewind gate.

- [ ] **Step 4: Run rewind tests to verify the gate**

Run: `uv run pytest tests/test_engine.py -v`

Expected: the new rewind test PASSES; the updated `test_process_message_resume_from_mid_phase` PASSES (now TEXT-based, gate doesn't fire); all other existing engine tests PASS.

- [ ] **Step 5: Write failing test for transcribe_phase silent-success fix**

Create or append to a handler test. The cleanest location is a new section in `tests/test_engine.py` since `transcribe_phase` uses the `@register("*", Phase.TRANSCRIBED)` decorator and we can test via the engine. Append:

```python
@pytest.mark.asyncio
async def test_transcribe_phase_missing_filepath_returns_false_with_error(
    config: Config, store: MessageStore, tmp_path: Path
) -> None:
    """Bug 3 fix: transcribe_phase with filepath=None must set ctx.error and
    return False (no silent success → no empty push)."""
    PipelineEngine._handlers = {}
    PipelineEngine._detectors = {}

    # Import the real transcribe_phase (it registers itself via decorator)
    import platforms.bilibili.handlers  # noqa: F401

    # Find the registered "*" / TRANSCRIBED handler
    handler = PipelineEngine._handlers.get(("*", Phase.TRANSCRIBED))
    assert handler is not None, "transcribe_phase should be registered"

    msg = store.add_new("bili:BV1", "bili", ContentType.VIDEO, 2000000000, "T", "A")
    assert msg is not None
    ctx = PhaseContext(msg=msg, config=config)
    ctx.downloaded_filepath = None  # the bug scenario

    result = await handler(ctx)

    assert result is False
    assert "downloaded_filepath missing" in ctx.error
```

- [ ] **Step 6: Run the test to verify it fails**

Run: `uv run pytest tests/test_engine.py::test_transcribe_phase_missing_filepath_returns_false_with_error -v`

Expected: FAIL — current `transcribe_phase` returns `True` and leaves `ctx.error=""`.

- [ ] **Step 7: Implement the silent-success fix in `transcribe_phase`**

Modify `platforms/bilibili/handlers.py` — replace lines 131-164 (the `transcribe_phase` function body):

```python
@PipelineEngine.register("*", Phase.TRANSCRIBED)
async def transcribe_phase(ctx: PhaseContext) -> bool:
    """视频转写（跨平台共用 handler）。

    Bug 3 fix:
    - ``filepath`` 缺失时不再静默 return True，而是 ``ctx.error='downloaded_filepath missing'``
      并 return False，让消息停留在当前阶段并暴露在 dashboard 上，避免
      空 transcript 推送低质量通知。``process_message`` 的 rewind 网关通常
      会先一步重新下载，这里只是兜底。
    - ``transcribe_file_async`` 真异常时记 WARNING 并降级用 ``content_text``
      继续流程（return True），保持既有的优雅降级语义。
    """
    if ctx.msg.content_type != ContentType.VIDEO:
        return True

    filepath = ctx.downloaded_filepath
    if filepath is None or not filepath.exists():
        ctx.error = "downloaded_filepath missing"
        logger.warning("⚠️  %s — 转写阶段无可用媒体文件", ctx.error)
        return False

    source_id = ctx.msg.msg_id
    logger.info("📝 转写 %s...", source_id)

    try:
        transcript = await transcribe_file_async(
            filepath=filepath,
            config=ctx.config,
            source_id=source_id,
            title=ctx.msg.title,
            author=ctx.msg.author,
        )
        if transcript.success:
            ctx.transcript_text = transcript.text
            logger.info("✓ 转写完成")
        else:
            logger.warning("⚠️  转写未成功: %s — 降级用 content_text 继续流程", transcript.error)
    except ImportError:
        logger.info("⏭  转写依赖未安装，跳过（降级用 content_text）")
    except Exception as exc:
        # 真异常：记 warning，不阻塞流程（return True），下游用 content_text
        logger.warning("⚠️  转写失败: %s — 降级用 content_text 继续流程", exc)
        logger.warning("Transcribe failed for %s: %s", source_id, exc)

    return True
```

Note: the previous `logger.error` + `logger.exception` for transcribe exceptions is downgraded to `logger.warning` because the flow continues (returning True) — `error`/`exception` level implies the pipeline stopped, which is misleading. This is a log-level correction, not an error-message text change.

- [ ] **Step 8: Run transcribe + engine + pipeline tests**

Run: `uv run pytest tests/test_engine.py tests/test_pipeline_concurrent.py tests/test_pipeline_e2e.py -v`

Expected: All PASS. `test_pipeline_e2e.py` (if it exercises real transcribe) should still pass because the graceful-degradation path is preserved for real exceptions.

- [ ] **Step 9: Lint + type check + full suite**

Run: `uv run ruff check core/engine.py platforms/bilibili/handlers.py tests/test_engine.py && uv run ruff format core/engine.py platforms/bilibili/handlers.py tests/test_engine.py && uv run pyright core/engine.py platforms/bilibili/handlers.py && uv run pytest -x`

Expected: clean.

- [ ] **Step 10: Commit**

```bash
git checkout -b fix/cross-process-state-recovery-silent-transcribe
git add core/engine.py platforms/bilibili/handlers.py tests/test_engine.py
git commit -m "fix(engine): recover cross-process state loss, surface transcribe failure

process_message now rewinds VIDEO messages stuck at DOWNLOADED+ to
DISCOVERED when ctx.downloaded_filepath is missing (cross-process state
loss after a crash between mark_phase and save). transcribe_phase no
longer silently returns True when filepath is missing — it sets
ctx.error='downloaded_filepath missing' and returns False so the message
stays visible in the dashboard instead of pushing an empty-transcript
notification. Real transcribe exceptions still degrade gracefully to
content_text (return True)."
```

- [ ] **Step 11: Push + PR**

```bash
git push -u origin fix/cross-process-state-recovery-silent-transcribe
gh pr create --title "fix(engine): recover cross-process state loss + surface transcribe failure" --body "Fixes Bug 3. Two coupled fixes: (1) process_message auto-rewinds VIDEO messages at DOWNLOADED+ with missing downloaded_filepath back to DISCOVERED (MessageStore doesn't persist PhaseContext artifacts); (2) transcribe_phase sets ctx.error and returns False on missing filepath instead of silently succeeding and pushing empty-transcript notifications. Real transcribe exceptions still degrade to content_text." --base master
```

Follow PR-review polling workflow.

---

## Task 4: Fix Cron Command + Add Health Alert (Bug 4)

**Problem:**
1. `cron_run.sh:10` is `uv run python run_check.py --platform all` — but `run_check.py`'s root is a Click `group` (`cli`), and `check` is a subcommand. Running the script with `--platform` directly raises `Error: No such option: --platform`. Cron has been silently failing (or the error is going to a log nobody reads).
2. `run_check.py:check` (lines 504-513) catches `Exception` and just `sys.exit(1)` — no notification to operators. If cron breaks (config error, network outage, all platforms down), nobody knows until they manually check the log.

**Fix (user decision: Option B):**
1. Fix `cron_run.sh` to `uv run python run_check.py check --platform all`.
2. In `run_check.py:check`'s `except Exception` block, send a health-alert `NotificationContent` to all configured endpoints via `send_to_subscription` before exiting. Reuse the existing notifier fan-out so this works for gotify/telegram/email automatically.

**Health alert content:** A `NotificationContent` with `platform="system"`, `type="health_alert"`, `title="Trawler 检查失败"`, `summary=<exception text>`. The render layer (`core/notifiers/base.py:_PLATFORM_STYLE`) falls back to `{"emoji": "📣", "author_label": "作者"}` for unknown platforms — acceptable, but we'll set `author="Trawler"` explicitly.

**Files:**
- Modify: `cron_run.sh:10`
- Modify: `run_check.py:504-513` (the `except Exception` block in `check`)
- Modify: `tests/test_cli.py` (add health-alert test)

### Task 4, Step 1: Write failing test for the health alert

Inspect `tests/test_cli.py` first to match its style. Then append a test using Click's `CliRunner`.

**MAJOR-4/5/6 design notes:** the original draft wrote a TOML file to disk *and* mocked `load_config` — those two are mutually exclusive (the mock return value wins, so the on-disk file is dead weight and misleads readers). The implementation's health-alert branch only fires when `config.endpoints` is non-empty (see Step 3: `if all_endpoint_names:`). A bare `Config()` has `endpoints=[]`, so `mock_send.assert_called_once()` would fail. The test must populate `cfg.endpoints` with at least one enabled `EndpointConfig` inside the mocked `load_config` return value.

```python
def test_check_failure_sends_health_alert(tmp_path, monkeypatch):
    """Bug 4 fix: when check raises, a health alert is sent to all
    configured endpoints before sys.exit(1)."""
    from unittest.mock import AsyncMock, patch

    from click.testing import CliRunner

    from run_check import cli
    from shared.config import Config, EndpointConfig
    from shared.protocols import SendResult

    runner = CliRunner()

    sent_contents = []

    async def fake_send(config, platform, endpoint_names, content):
        sent_contents.append(content)
        return [SendResult(endpoint_name=n, success=True) for n in endpoint_names]

    with (
        patch("run_check.run_check_once", new_callable=AsyncMock) as mock_run,
        patch("run_check.send_to_subscription", side_effect=fake_send) as mock_send,
        patch("run_check.load_config") as mock_load,
    ):
        # MAJOR-4/5/6: build the Config in-memory and return it from the mock.
        # No TOML file is written — load_config is mocked, so writing one would
        # be contradictory dead code.
        cfg = Config()
        cfg.endpoints = [
            EndpointConfig(name="ops", url="https://g", token="t", enabled=True),
        ]
        mock_load.return_value = cfg
        # Make run_check_once raise to trigger the except branch
        mock_run.side_effect = RuntimeError("simulated check failure")

        result = runner.invoke(
            cli,
            ["check", "--platform", "all", "--config", str(tmp_path / "unused.toml")],
        )

    assert result.exit_code == 1
    # Health alert should have been sent exactly once (cfg.endpoints non-empty)
    mock_send.assert_called_once()
    assert len(sent_contents) == 1
    alert = sent_contents[0]
    assert alert.platform == "system"
    assert alert.type == "health_alert"
    assert "simulated check failure" in alert.summary
```

Note: this test requires `run_check.py` to import `send_to_subscription` at module level (so it can be patched at `run_check.send_to_subscription`). See Step 3 for the import addition. The `--config` argument is still passed so Click's option validation doesn't complain, but its value is unused because `load_config` is mocked.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_check_failure_sends_health_alert -v`

Expected: FAIL — `AttributeError: module 'run_check' has no attribute 'send_to_subscription'` or the alert is never sent (current behavior just exits 1).

- [ ] **Step 3: Add the import and health-alert logic to `run_check.py`**

**3a. BLOCKER-3: fix the top-of-file imports (alphabetical, ruff I).**

Current relevant import lines in `run_check.py`:
```python
from core.pipeline import run_check_once                       # line 18
from core.subscription_cli import add_subscription, ...         # line 19
...
from shared.config import Config, load_config                   # line 22
```

There is currently NO `from core.notifiers import ...` and NO `from shared.protocols import ...`. The test patches `run_check.send_to_subscription` at module level, so the binding must exist at module scope (not imported locally inside `check`).

Replace line 18 with two lines (alphabetical: `notifiers` < `pipeline`):
```python
from core.notifiers import send_to_subscription
from core.pipeline import run_check_once
```

Then add a new line for `NotificationContent`. `shared.protocols` sorts after `shared.config`, so it goes immediately after line 22:
```python
from shared.config import Config, load_config
from shared.protocols import NotificationContent
```

Final import order (relevant slice, after edit):
```python
from core.notifiers import send_to_subscription
from core.pipeline import run_check_once
from core.subscription_cli import add_subscription, list_subscriptions, remove_subscription, search_by_name
from shared.auth import QRExpiredError, get_authenticator, update_auth_section
from shared.auth.base import PlatformTokens
from shared.config import Config, load_config
from shared.protocols import NotificationContent
```

All three local imports (`core.*`) come before all `shared.*` imports, and within each group the alphabetical order holds (n < p < s; auth < auth.base < config < protocols). ruff I passes.

3b. Replace the `except Exception as exc:` block (lines 509-513) with:

```python
    except Exception as exc:
        console.print(f"[red]✗ 运行出错: {exc}[/]")
        if verbose:
            console.print_exception()
        # Bug 4 fix: cron 失败时推送健康告警到所有配置的 endpoints，
        # 让运维在 cron 静默失败时也能收到通知
        try:
            health_alert = NotificationContent(
                platform="system",
                source_id="health",
                title="Trawler 检查失败",
                author="Trawler",
                summary=f"check 命令执行失败: {exc}",
                type="health_alert",
            )
            # 发到所有已配置且 enabled 的 endpoints（platform 传空字符串避免过滤）
            all_endpoint_names = [ep.name for ep in config.endpoints if ep.enabled]
            if all_endpoint_names:
                logger.warning("🚨 推送健康告警到 %d 个 endpoint", len(all_endpoint_names))
                # check 是同步 Click 命令，run_check_once 的事件循环已随异常退出，
                # 这里开一个新的 asyncio.run 推送告警
                asyncio.run(
                    send_to_subscription(config, "system", all_endpoint_names, health_alert)
                )
        except Exception as alert_exc:
            logger.error("推送健康告警失败: %s", alert_exc)
        sys.exit(1)
```

3c. The `except KeyboardInterrupt` block above it (lines 506-508) stays unchanged — user-initiated Ctrl-C should NOT trigger a health alert.

The test in Step 1 patches `run_check.send_to_subscription` at the module level; since the implementation calls `send_to_subscription` via the module attribute (not a direct `from ... import` into a local name), the patch takes effect correctly. `asyncio.run(mock_async_fn(...))` works because the patched `fake_send` is itself a coroutine function.

### Task 4, Step 4: Fix `cron_run.sh`

Edit `cron_run.sh` line 10:

```bash
uv run python run_check.py check --platform all
```

The full corrected `cron_run.sh`:

```bash
#!/usr/bin/env bash
# Trawler Cron 调度脚本
# 用法: */3 * * * * /path/to/trawler/cron_run.sh >> /path/to/trawler/cron.log 2>&1

set -euo pipefail
cd "$(dirname "$0")"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Trawler check start ==="

uv run python run_check.py check --platform all

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Trawler check end ==="
```

- [ ] **Step 5: Run the health-alert test**

Run: `uv run pytest tests/test_cli.py::test_check_failure_sends_health_alert -v`

Expected: PASS.

- [ ] **Step 6: Smoke-test the fixed cron command locally**

Run: `uv run python run_check.py check --platform all --help 2>&1 | head -5`

Expected: prints the `check` subcommand help (no "No such option" error). Then optionally:

Run: `bash cron_run.sh 2>&1 | head -20` (only if you have a working config — otherwise it will fail at config load, which still proves the `check` subcommand is now invoked correctly).

Expected: no `Error: No such option: --platform`.

- [ ] **Step 7: Run full test suite + lint + type check**

Run: `uv run ruff check run_check.py tests/test_cli.py && uv run ruff format run_check.py tests/test_cli.py && uv run pyright run_check.py && uv run pytest -x`

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git checkout -b fix/cron-command-and-health-alert
git add cron_run.sh run_check.py tests/test_cli.py
git commit -m "fix(cli): correct cron command and add health alert on check failure

cron_run.sh invoked 'run_check.py --platform all' but the Click group
requires a subcommand — fix to 'run_check.py check --platform all'. On
check command exception, push a NotificationContent(platform='system',
type='health_alert') to all configured endpoints so operators get
notified of silent cron failures instead of discovering them in logs."
```

- [ ] **Step 9: Push + PR**

```bash
git push -u origin fix/cron-command-and-health-alert
gh pr create --title "fix(cli): correct cron command + add health alert on check failure" --body "Fixes Bug 4. (1) cron_run.sh used the wrong invocation (missing 'check' subcommand) — fixed. (2) check command's except branch now sends a health-alert NotificationContent to all configured endpoints before sys.exit(1), so silent cron failures surface to operators." --base master
```

Follow PR-review polling workflow.

---

## Cross-Task Verification

After all 4 PRs merge, run a final integration smoke test:

- [ ] **Final: Run full suite + lint + type check on master**

```bash
git checkout master && git pull
uv run ruff check . && uv run ruff format --check . && uv run pyright . && uv run pytest -x
```

Expected: all green.

- [ ] **Final: Manual cron smoke test**

Run `bash cron_run.sh` against a real config with at least one endpoint configured. Verify:
1. No "No such option" error (Bug 4 fixed).
2. If auth is expired, scheduler logs the warning and does NOT silently write back (Bug 1 fixed).
3. Summary/keywords in the pushed notification come from a single AI pass (Bug 2 fixed — observable via logs showing one "AI 内容分析成功" line per message).
4. A VIDEO message that previously crashed mid-pipeline re-downloads instead of pushing an empty notification (Bug 3 fixed — observable in dashboard phase transitions).

---

## Risk & Rollback Notes

- **Bug 1:** If `validate_tokens` is flaky on a platform (e.g. XHS rate-limits the probe), renewals will start reporting `expired` more often. Mitigation: the existing `validate_tokens` implementations already swallow network exceptions and return `False`, and we treat exception-as-failure. If false negatives spike, relax to "validation exception = allow writeback" by changing the `except` branch in Step 3 to return `RenewalResult(platform, "renewed", ...)` instead of `expired`. Rollback: revert the single scheduler.py commit.
- **Bug 2:** Removing the n-gram fallback means that when AI is down, notifications ship with empty summary/keywords. This is intentional (operator sees the problem) but if it's too noisy, add a `AnalysisConfig.allow_empty_on_failure: bool = True` flag and have `summarize_phase` skip the push when both summary and keywords are empty. Rollback: revert summarizer.py + handlers.py commits.
- **Bug 3:** The rewind gate could cause re-downloads if a legitimate future code path ever sets `downloaded_filepath` lazily inside the transcribe handler. Mitigation: the gate only triggers for `VIDEO` content at `phase != DISCOVERED` with `filepath is None` — the only way to reach transcribe with a None filepath today is the cross-process-loss path. Rollback: revert engine.py + handlers.py commits.
- **Bug 4:** Health alerts will fire on every check failure — if cron runs every 3 minutes and check is broken, operators get a flood. Mitigation: the existing `set -euo pipefail` + cron `>> cron.log 2>&1` means the alert only fires once per cron invocation (not per retry). If flooding becomes an issue, add a simple file-based 30-min cooldown in the except block. Rollback: revert run_check.py + cron_run.sh commits.
