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

from shared.config import AnalysisConfig, Config, LLMProviderConfig
from shared.constants import LLM_API_TIMEOUT
from shared.protocols import LLMProvider

logger = logging.getLogger(__name__)

# ── Prompt 模板 ──────────────────────────────────────────────────

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


# ── 解析层 ───────────────────────────────────────────────────────

# Issue #56 场景 B: 放宽标题格式 —— 允许行尾 [:：] 和加粗 **...**。
# 常见 LLM 不严格输出：'## 摘要：' / '## 摘要:' / '## **摘要**' / '## **摘要**：'。
# one_line_summary 同时接受 '## 总结' 同义词（向后兼容旧 prompt 的「## 一句话总结」）。
_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "summary": re.compile(r"^#{1,3}\s*\**摘要\**\s*[:：]?\s*$", re.MULTILINE),
    "one_line_summary": re.compile(r"^#{1,3}\s*\**(一句话总结|总结)\**\s*[:：]?\s*$", re.MULTILINE),
    "keywords": re.compile(r"^#{1,3}\s*\**关键词\**\s*[:：]?\s*$", re.MULTILINE),
    "tags": re.compile(r"^#{1,3}\s*\**标签\**\s*[:：]?\s*$", re.MULTILINE),
}


@dataclass
class AnalysisResult:
    """``analyze_content`` 的结构化结果。"""

    summary: str = ""
    one_line_summary: str = ""
    keywords: list[str] = field(default_factory=lambda: [])
    tags: list[str] = field(default_factory=lambda: [])
    is_ai: bool = False
    source: str = "none"  # provider name | "none" | "empty"
    failed: bool = False  # True 表示 fallback 链全部失败（与 source="empty" 区分）
    # Issue #56: 原始 LLM 响应文本，用于排查 silent empty（解析为空但 HTTP 200 的情况）。
    # parse_markdown_analysis 解析时填入原始 LLM 响应；analyze_content 失败/禁用分支
    # 不构造该字段，保持默认空字符串。
    raw: str = ""


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
    - Issue #56: 保留原始 raw 文本到 ``result.raw``，供 handler 在解析为空时观测。
    """
    text = _strip_code_fence(raw)
    return AnalysisResult(
        summary=_extract_section(text, _SECTION_PATTERNS["summary"]),
        one_line_summary=_extract_section(text, _SECTION_PATTERNS["one_line_summary"]),
        keywords=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["keywords"]))[:5],
        tags=_parse_list_field(_extract_section(text, _SECTION_PATTERNS["tags"]))[:3],
        raw=raw,
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
                response = await client.post(url, json=payload, headers=headers, timeout=LLM_API_TIMEOUT)
        except httpx.TimeoutException:
            raise RuntimeError(f"OpenAI API 调用超时 ({LLM_API_TIMEOUT}s)")
        except httpx.ConnectError:
            raise RuntimeError(f"无法连接到 API: {self.api_base}")

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise RuntimeError(f"API 返回错误 ({response.status_code}): {response.text[:200]}")

        data = response.json()
        try:
            message = data["choices"][0]["message"]
            # Issue #56 场景 A: reasoning 模型（如 deepseek-v4-flash）通过 exusiai 网关时，
            # 长文本场景 content="" / content=null，答案错放到 reasoning_content 字段。
            # content 非空时优先用 content（保持原有行为），content 为空时 fallback 到 reasoning_content。
            # 不拼接两者：reasoning_content 通常是思考链，包含大量中间推理，
            # 与最终答案混在一起会破坏解析。
            content = message.get("content") or ""
            # Issue #56: whitespace-only content（'   ' / '\n'）是 truthy 字符串，
            # 仅判 falsy 会漏过，最终 .strip() == "" 再次 silent empty。
            # 改为判 .strip()，让空白 content 同样 fallback 到 reasoning_content。
            if not content.strip():
                reasoning = message.get("reasoning_content") or ""
                if reasoning:
                    logger.debug(
                        "OpenAI content 为空，fallback 到 reasoning_content (model=%s, len=%d)",
                        self.model_name,
                        len(reasoning),
                    )
                    content = reasoning
            # Issue #56: 记录 raw response 截断（%.500s 是 printf-style 截断到 500 字符，
            # logging 标准用法，避免 % 字段顺序问题）。仅 INFO 看不到，运维设置
            # LOG_LEVEL=DEBUG 时可见。
            logger.debug(
                "LLM raw response (model=%s, content_len=%d): %.500s",
                self.model_name,
                len(content),
                content,
            )
            return content.strip()
        except (KeyError, IndexError, AttributeError, TypeError) as e:
            raise RuntimeError(f"解析 API 响应失败: {e}")


# ── 公共接口 ─────────────────────────────────────────────────────


class FallbackChainProvider:
    """按序尝试多个 provider，前一个失败（异常）才 fallback 到下一个。

    实现 ``LLMProvider`` Protocol（鸭子类型，无需显式继承）。

    设计要点：
    - 所有失败类型（401 / 超时 / 网络错 / 5xx / parse 错）都触发 fallback。
      不区分「永久失败」vs「临时失败」（见 plan F2），永久失败的 provider
      反复重试由 ``MessageRecord.retry_count`` 上限兜底。
    - 每个 provider 失败时记 WARNING（运维可见），所有失败后抛 RuntimeError
      让上层 ``analyze_content`` 标记 ``failed=True``。
    - 空 providers 列表在构造时抛 ``ValueError``。
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("providers 列表不能为空")
        self._providers = providers

    async def generate(self, prompt: str) -> str:
        errors: list[str] = []
        for idx, provider in enumerate(self._providers, start=1):
            try:
                result = await provider.generate(prompt)
                if idx > 1:
                    logger.info("✓ fallback 到第 %d 个 provider 成功", idx)
                return result
            except Exception as e:
                msg = f"provider #{idx} 失败: {e}"
                errors.append(msg)
                logger.warning("⚠️  %s", msg)
        raise RuntimeError(f"所有 provider 失败 ({len(errors)} 个): {' | '.join(errors)}")


def _build_single_provider(p_cfg: AnalysisConfig | LLMProviderConfig) -> LLMProvider:
    """根据单个 provider 配置构建 OpenAIProvider（内部辅助）。

    p_cfg 可以是 AnalysisConfig（主 provider 走旧字段）或 LLMProviderConfig（备用）。
    两者字段名一致（provider/api_base/api_key/model_name），鸭子类型兼容。
    """
    provider_name = p_cfg.provider.lower().strip()

    if provider_name == "openai":
        if not p_cfg.api_base:
            raise ValueError("OpenAI provider 需要配置 api_base")
        return OpenAIProvider(
            api_base=p_cfg.api_base,
            api_key=p_cfg.api_key,
            model_name=p_cfg.model_name or "gpt-4o-mini",
        )
    elif provider_name == "ollama":
        return OpenAIProvider(
            api_base=p_cfg.api_base or "http://localhost:11434/v1",
            api_key=p_cfg.api_key or "ollama",
            model_name=p_cfg.model_name or "qwen2.5:7b",
        )
    else:
        raise ValueError(f"不支持的 provider: {p_cfg.provider}")


def create_provider(config: AnalysisConfig) -> LLMProvider:
    """根据配置构建 provider 链。

    - 单 provider 配置（无 extra_providers）→ 长度为 1 的链
    - 多 provider 配置 → 主 provider + extra_providers 按序组成的链
    - ``enabled=False`` 或主 provider 未配置且 extras 为空 → 抛 ValueError

    兼容性：返回类型仍是 ``LLMProvider``（``FallbackChainProvider`` 实现此协议），
    调用方代码（``analyze_content`` / ``_probe_provider``）不需要改。

    Raises:
        ValueError: 不支持的 provider 类型，或链为空
    """
    chain = config.providers_chain
    if not chain:
        raise ValueError("AI 分析未启用或未配置 provider")
    providers = [_build_single_provider(p) for p in chain]
    return FallbackChainProvider(providers=providers)


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
        # fallback 链全失败（或单 provider 失败）
        logger.warning("AI 内容分析失败 (%s): %s，返回空结果", source_id, e)
        return AnalysisResult(source="none", failed=True)


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
