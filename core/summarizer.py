"""AI 摘要生成模块 - 支持多种 LLM 提供商和本地降级"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter

from shared.config import AnalysisConfig, Config
from shared.constants import CODEBUDDY_TIMEOUT, LLM_API_TIMEOUT
from shared.protocols import LLMProvider

logger = logging.getLogger(__name__)

# ── 摘要 Prompt 模板 ─────────────────────────────────────────────

_SUMMARY_PROMPT_TEMPLATE = """\
请总结以下内容的核心观点和关键信息。
内容尽量详尽，把重要观点都覆盖到，请分点列出。
只输出总结内容，不要额外的说明。

标题：{title}
作者：{author}
正文：{text}"""

_KEYWORDS_PROMPT_TEMPLATE = """\
请从以下内容中提取 3-5 个关键词，用中文分号（；）分隔，只输出关键词，不要额外说明。

标题：{title}
作者：{author}
正文：{text}"""


# ── CodeBuddy Provider ───────────────────────────────────────────


class CodeBuddyProvider:
    """CodeBuddy CLI 提供商

    通过 subprocess 调用 codebuddy 命令行工具进行文本生成。
    使用 glm-5.1-ioa 模型。
    """

    def __init__(self, model: str = "glm-5.1-ioa") -> None:
        """初始化 CodeBuddy 提供商

        Args:
            model: 使用的模型名称
        """
        self.model = model

    async def generate(self, prompt: str) -> str:
        """调用 codebuddy CLI 生成文本

        Args:
            prompt: 输入提示文本

        Returns:
            模型生成的文本内容

        Raises:
            RuntimeError: codebuddy 执行失败时抛出
        """
        logger.debug("调用 CodeBuddy (model=%s)...", self.model)
        cmd = ["codebuddy", "--model", self.model, prompt]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError("codebuddy 命令未找到，请确保已安装")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=CODEBUDDY_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"CodeBuddy 调用超时 ({CODEBUDDY_TIMEOUT}s)")

        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        if proc.returncode != 0:
            raise RuntimeError(f"CodeBuddy 调用失败 (返回码 {proc.returncode}): {stderr}")

        output = stdout.strip()
        if not output:
            raise RuntimeError("CodeBuddy 返回空结果")

        return output


# ── OpenAI 兼容 Provider ─────────────────────────────────────────


class OpenAIProvider:
    """OpenAI 兼容 API 提供商

    支持任何 OpenAI 兼容的 API 端点（如 OpenAI、DeepSeek、本地 Ollama 等）。
    使用 requests 库直接调用 API，避免额外依赖。
    """

    def __init__(
        self,
        api_base: str,
        api_key: str = "",
        model_name: str = "gpt-4o-mini",
    ) -> None:
        """初始化 OpenAI 兼容提供商

        Args:
            api_base: API 基础地址（如 https://api.openai.com/v1）
            api_key: API 密钥
            model_name: 模型名称
        """
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name

    async def generate(self, prompt: str) -> str:
        """调用 OpenAI 兼容 API 生成文本

        Args:
            prompt: 输入提示文本

        Returns:
            模型生成的文本内容

        Raises:
            RuntimeError: API 调用失败时抛出
        """
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
            raise RuntimeError(f"API 返回错误 ({response.status_code}): {response.text[:200]}")

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"解析 API 响应失败: {e}")


# ── 本地降级 Provider ────────────────────────────────────────────


class LocalFallbackProvider:
    """本地提取式摘要提供商

    不依赖任何外部服务，使用 TF（词频）思路进行提取式摘要：
    1. 按标点符号分句
    2. 计算高频关键词
    3. 对句子按关键词密度打分
    4. 取 top N 高分句子拼接为摘要
    """

    # 中文停用词（高频但无实际意义的词）
    _STOP_WORDS: set[str] = {
        "的",
        "了",
        "是",
        "在",
        "我",
        "有",
        "和",
        "就",
        "不",
        "人",
        "都",
        "一",
        "一个",
        "上",
        "也",
        "很",
        "到",
        "说",
        "要",
        "去",
        "你",
        "会",
        "着",
        "没有",
        "看",
        "好",
        "自己",
        "这",
        "他",
        "她",
        "它",
        "们",
        "那",
        "这个",
        "那个",
        "什么",
        "怎么",
        "如何",
        "可以",
        "但是",
        "因为",
        "所以",
        "如果",
        "虽然",
        "而且",
        "或者",
        "以及",
        "还",
        "把",
        "被",
        "让",
        "给",
        "从",
        "对",
        "比",
        "跟",
        "与",
        "为",
        "等",
        "能",
        "才",
        "更",
        "最",
        "已经",
        "可能",
        "应该",
        "需要",
        "这些",
        "那些",
        "之",
        "其",
        "此",
        "该",
        "每",
        "各",
    }

    # 分句标点
    _SENTENCE_DELIMITERS = re.compile(r"[。！？；\n]+")

    def generate(self, prompt: str) -> str:
        """本地提取式摘要生成

        从输入文本中提取关键句子组成摘要。

        Args:
            prompt: 输入文本（通常包含标题、作者和正文）

        Returns:
            提取式摘要文本
        """
        # 从 prompt 中提取正文部分
        text = prompt
        body_match = re.search(r"正文[：:]\s*(.*)", prompt, re.DOTALL)
        if body_match:
            text = body_match.group(1).strip()

        if not text.strip():
            return "（内容为空，无法生成摘要）"

        return self._extract_summary(text)

    def _extract_summary(self, text: str, top_n: int = 8) -> str:
        """基于词频的提取式摘要

        Args:
            text: 输入文本
            top_n: 提取的句子数量

        Returns:
            摘要文本
        """
        # 分句
        sentences = self._split_sentences(text)
        if not sentences:
            return text[:500]

        # 计算词频
        word_freq = self._compute_word_freq(text)

        # 句子打分
        scored_sentences: list[tuple[int, str, float]] = []
        for idx, sentence in enumerate(sentences):
            score = self._score_sentence(sentence, word_freq)
            # 对靠前的句子给予轻微加权
            position_bonus = 1.0 + 0.1 * (1.0 - idx / len(sentences))
            scored_sentences.append((idx, sentence, score * position_bonus))

        # 按 score 降序排列，取 top_n
        scored_sentences.sort(key=lambda x: x[2], reverse=True)
        top_sentences = sorted(scored_sentences[:top_n], key=lambda x: x[0])

        summary = "。".join(s[1] for s in top_sentences)
        if not summary.endswith(("。", "！", "？", "；")):
            summary += "。"

        return summary

    def _split_sentences(self, text: str) -> list[str]:
        """按标点分句

        Args:
            text: 输入文本

        Returns:
            句子列表
        """
        parts = self._SENTENCE_DELIMITERS.split(text)
        return [p.strip() for p in parts if len(p.strip()) >= 5]

    def _compute_word_freq(self, text: str) -> Counter[str]:
        """计算词频（简单字符级，过滤停用词）

        使用滑动窗口提取 2-4 字词组，并统计频率。

        Args:
            text: 输入文本

        Returns:
            词频 Counter
        """
        # 清理文本
        clean = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbf]", "", text)

        # 提取 2-4 字的中文词组
        ngrams: list[str] = []
        for n in (2, 3, 4):
            for i in range(len(clean) - n + 1):
                gram = clean[i : i + n]
                # 跳过包含停用词的 gram
                if any(w in gram for w in ("的", "了", "是", "在", "我", "和")):
                    continue
                ngrams.append(gram)

        return Counter(ngrams)

    def _score_sentence(self, sentence: str, word_freq: Counter[str]) -> float:
        """计算句子的关键词得分

        Args:
            sentence: 待评分句子
            word_freq: 全文词频

        Returns:
            得分值
        """
        if not word_freq:
            return 0.0

        # 取 top 50 高频词
        top_words = {w for w, _ in word_freq.most_common(50)}
        score = 0.0
        for word in top_words:
            if word in sentence:
                score += word_freq[word]

        # 归一化：除以句子长度避免长句天然优势
        return score / max(len(sentence), 1)


# ── 公共接口 ─────────────────────────────────────────────────────


def _create_provider(config: AnalysisConfig) -> LLMProvider:
    """根据配置创建 LLM 提供商

    Args:
        config: AI 分析配置

    Returns:
        对应的 LLMProvider 实例

    Raises:
        ValueError: 不支持的 provider 类型
    """
    provider_name = config.provider.lower().strip()

    if provider_name == "codebuddy":
        return CodeBuddyProvider()
    elif provider_name == "openai":
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


async def generate_summary(
    source_id: str,
    title: str,
    author: str,
    text: str,
    config: Config,
) -> tuple[str, str, bool]:
    """生成内容摘要（含 AI 降级机制）

    根据配置选择 LLM 提供商生成摘要。如果 AI 生成失败，
    自动降级到本地提取式摘要。

    Args:
        source_id: 来源标识（bvid 或 note_id）
        title: 内容标题
        author: 内容作者
        text: 待摘要的文本
        config: 全局配置对象

    Returns:
        (摘要文本, 来源标识, 是否AI生成) 三元组
    """
    if not config.analysis.enabled:
        logger.debug("AI 分析已禁用，使用本地摘要")
        fallback = LocalFallbackProvider()
        prompt = _SUMMARY_PROMPT_TEMPLATE.format(title=title, author=author, text=text)
        return fallback.generate(prompt), "local", False

    if not text.strip():
        return "（内容为空）", "none", False

    # 尝试 AI 生成
    try:
        provider = _create_provider(config.analysis)
        prompt = _SUMMARY_PROMPT_TEMPLATE.format(title=title, author=author, text=text)
        summary = await provider.generate(prompt)
        logger.info("AI 摘要生成成功: %s", source_id)
        return summary, config.analysis.provider, True

    except Exception as e:
        logger.warning("AI 摘要生成失败 (%s): %s，降级到本地摘要", config.analysis.provider, e)

    # 降级到本地摘要
    fallback = LocalFallbackProvider()
    prompt = _SUMMARY_PROMPT_TEMPLATE.format(title=title, author=author, text=text)
    summary = fallback.generate(prompt)
    return summary, "local-fallback", False


async def extract_keywords(
    text: str,
    title: str,
    author: str,
    config: Config | None = None,
) -> list[str]:
    """从文本中提取关键词

    优先使用 LLM 提取关键词，失败时降级到基于词频的本地提取。

    Args:
        text: 正文文本
        title: 内容标题
        author: 内容作者
        config: 全局配置对象（可选，用于 AI 提取）

    Returns:
        3-5 个关键词列表
    """
    # 尝试 AI 提取
    if config and config.analysis.enabled:
        try:
            provider = _create_provider(config.analysis)
            prompt = _KEYWORDS_PROMPT_TEMPLATE.format(title=title, author=author, text=text)
            result = await provider.generate(prompt)
            # 解析关键词（支持中英文分号、逗号、换行分隔）
            keywords = re.split(r"[；;，,\n]+", result)
            keywords = [k.strip() for k in keywords if k.strip()]
            if keywords:
                return keywords[:5]
        except Exception as e:
            logger.debug("AI 关键词提取失败: %s，使用本地提取", e)

    # 本地 TF 提取
    return _extract_keywords_local(text, title)


def _extract_keywords_local(text: str, title: str, top_n: int = 5) -> list[str]:
    """基于词频的本地关键词提取

    使用简单的 n-gram 频率统计提取关键词。

    Args:
        text: 正文文本
        title: 标题（标题中的词额外加权）
        top_n: 返回的关键词数量

    Returns:
        关键词列表
    """
    # 标题中的词加权：将标题重复拼接到文本中
    enhanced_text = f"{title} {title} {title} {text}"

    # 清理文本
    clean = re.sub(r"[^\u4e00-\u9fff\u3400-\u4dbf]", "", enhanced_text)

    # 停用词
    stop_chars = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一"}

    # 提取 2-4 字 n-gram
    ngram_count: Counter[str] = Counter()
    for n in (2, 3, 4):
        for i in range(len(clean) - n + 1):
            gram = clean[i : i + n]
            if not any(c in stop_chars for c in gram):
                ngram_count[gram] += 1

    # 去除被更长 n-gram 包含的短 n-gram（如果频率相同）
    candidates: list[str] = []
    for word, count in ngram_count.most_common(30):
        # 检查是否已被更长的词包含
        dominated = False
        for existing in candidates:
            if word in existing and ngram_count.get(existing, 0) >= count * 0.7:
                dominated = True
                break
        if not dominated:
            candidates.append(word)
        if len(candidates) >= top_n:
            break

    return candidates[:top_n]
