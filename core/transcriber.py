"""SenseVoice 语音转写模块 - 将音视频文件转写为文本"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from shared.config import Config
from shared.protocols import TranscriptResult

console = Console()

# ── 全局模型缓存 ────────────────────────────────────────────────

_model_cache: dict[str, Any] = {}


def _get_pipeline() -> Any:
    """获取或加载 SenseVoice 语音识别模型 pipeline

    使用全局缓存避免重复加载模型。首次调用时会从 ModelScope 下载模型。

    Returns:
        modelscope pipeline 对象
    """
    cache_key = "sensevoice"
    if cache_key not in _model_cache:
        console.log("[bold blue]正在加载 SenseVoiceSmall 模型（首次加载可能需要下载）...[/]")
        from modelscope.pipelines import pipeline
        from modelscope.utils.constant import Tasks

        _model_cache[cache_key] = pipeline(Tasks.auto_speech_recognition, model="iic/SenseVoiceSmall")
        console.log("[bold green]SenseVoiceSmall 模型加载完成[/]")
    return _model_cache[cache_key]


def _extract_audio(filepath: Path, output_path: Path) -> None:
    """使用 FFmpeg 提取音频并转换为 16kHz 单声道 WAV

    Args:
        filepath: 输入音视频文件路径
        output_path: 输出 WAV 文件路径

    Raises:
        RuntimeError: FFmpeg 执行失败时抛出
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(filepath),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(output_path),
        "-y",
    ]
    console.log(f"[dim]执行 FFmpeg: {' '.join(cmd)}[/]")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 音频提取失败 (返回码 {result.returncode}): {result.stderr}")


def _get_audio_duration(wav_path: Path) -> float:
    """获取 WAV 文件时长

    使用 FFmpeg 获取音频时长信息。

    Args:
        wav_path: WAV 文件路径

    Returns:
        音频时长（秒）
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(wav_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        pass
    return 0.0


def _traditional_to_simplified(text: str) -> str:
    """将繁体中文转换为简体中文

    Args:
        text: 可能包含繁体的中文文本

    Returns:
        转换后的简体中文文本
    """
    try:
        from opencc import OpenCC

        cc = OpenCC("t2s")
        return cc.convert(text)
    except ImportError:
        try:
            from opencc_python_reimplemented import OpenCC

            cc = OpenCC("t2s")
            return cc.convert(text)
        except ImportError:
            console.log("[yellow]opencc 未安装，跳过繁简转换[/]")
            return text


def _save_transcript(
    text: str,
    source_id: str,
    title: str,
    author: str,
    language: str,
    duration_seconds: float,
    output_dir: Path,
) -> tuple[Path, Path]:
    """保存转写结果到文本文件和 JSON 文件

    Args:
        text: 转写文本
        source_id: 来源标识
        title: 标题
        author: 作者
        language: 语言代码
        duration_seconds: 音频时长
        output_dir: 输出目录

    Returns:
        (txt文件路径, json文件路径) 元组
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_path = output_dir / f"{source_id}.txt"
    json_path = output_dir / f"{source_id}.json"

    # 保存纯文本
    txt_path.write_text(text, encoding="utf-8")

    # 构建带时间戳分段的 JSON 结构
    segments = _split_into_segments(text, duration_seconds)
    json_data = {
        "source_id": source_id,
        "title": title,
        "author": author,
        "language": language,
        "duration_seconds": duration_seconds,
        "text": text,
        "segments": segments,
    }
    json_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")

    return txt_path, json_path


def _split_into_segments(text: str, duration_seconds: float, min_segment_chars: int = 50) -> list[dict[str, Any]]:
    """将文本按标点分段并估算时间戳

    Args:
        text: 完整转写文本
        duration_seconds: 音频总时长
        min_segment_chars: 最小分段字符数

    Returns:
        包含时间戳分段的列表
    """
    if not text or duration_seconds <= 0:
        return []

    # 按句号、问号、感叹号分段
    raw_segments = re.split(r"([。！？；\n])", text)
    segments: list[dict[str, Any]] = []
    current = ""

    for part in raw_segments:
        current += part
        if len(current) >= min_segment_chars or part in "。！？；\n":
            stripped = current.strip()
            if stripped:
                segments.append({"text": stripped})
            current = ""

    # 处理剩余文本
    if current.strip():
        segments.append({"text": current.strip()})

    # 估算时间戳（均匀分配）
    total_chars = sum(len(s["text"]) for s in segments)
    if total_chars == 0:
        return segments

    elapsed = 0.0
    for seg in segments:
        seg_duration = (len(seg["text"]) / total_chars) * duration_seconds
        seg["start"] = round(elapsed, 2)
        seg["end"] = round(elapsed + seg_duration, 2)
        elapsed += seg_duration

    return segments


def transcribe_file(
    filepath: Path,
    config: Config,
    source_id: str,
    title: str,
    author: str,
) -> TranscriptResult:
    """将音视频文件转写为文本

    完整的转写流程：
    1. 使用 FFmpeg 提取音频并转换为 16kHz 单声道 WAV
    2. 加载 SenseVoiceSmall 模型（全局缓存，仅首次加载）
    3. 执行语音转写（自动语言识别）
    4. 繁体中文转简体中文（通过 opencc）
    5. 保存 .txt 和 .json 格式的转写结果
    6. 返回结构化的 TranscriptResult

    Args:
        filepath: 输入音视频文件路径
        config: 全局配置对象
        source_id: 来源标识（如 bvid 或 note_id）
        title: 内容标题
        author: 内容作者

    Returns:
        TranscriptResult 包含转写结果或错误信息
    """
    console.log(f"[bold blue]开始转写: {title} ({source_id})[/]")

    if not filepath.exists():
        return TranscriptResult(
            success=False,
            source_id=source_id,
            title=title,
            error=f"文件不存在: {filepath}",
        )

    temp_wav: Optional[Path] = None

    try:
        # Step 1: FFmpeg 提取音频
        temp_wav = Path(tempfile.mktemp(suffix=".wav"))
        console.log("[dim]Step 1: 提取音频...[/]")
        _extract_audio(filepath, temp_wav)

        # 获取音频时长
        duration = _get_audio_duration(temp_wav)

        # Step 2 & 3: 加载模型并转写
        console.log("[dim]Step 2-3: 加载模型并转写...[/]")
        asr_pipeline = _get_pipeline()
        result = asr_pipeline(str(temp_wav))

        # 提取文本
        if isinstance(result, dict):
            text = result.get("text", "")
            language = result.get("language", "zh")
        elif isinstance(result, str):
            text = result
            language = "zh"
        else:
            text = str(result)
            language = "zh"

        if not text.strip():
            return TranscriptResult(
                success=False,
                source_id=source_id,
                title=title,
                duration_seconds=duration,
                error="转写结果为空",
            )

        # Step 4: 繁体转简体
        console.log("[dim]Step 4: 繁体转简体...[/]")
        text = _traditional_to_simplified(text)

        # Step 5: 保存结果
        console.log("[dim]Step 5: 保存转写结果...[/]")
        output_dir = Path(config.transcribe.output_dir)
        txt_path, json_path = _save_transcript(
            text=text,
            source_id=source_id,
            title=title,
            author=author,
            language=language,
            duration_seconds=duration,
            output_dir=output_dir,
        )

        console.log(f"[bold green]转写完成: {title} (时长 {duration:.1f}s, 文本 {len(text)} 字)[/]")

        return TranscriptResult(
            success=True,
            source_id=source_id,
            title=title,
            transcript_path=txt_path,
            json_path=json_path,
            text=text,
            language=language,
            duration_seconds=duration,
        )

    except Exception as e:
        console.log(f"[bold red]转写失败: {title} - {e}[/]")
        return TranscriptResult(
            success=False,
            source_id=source_id,
            title=title,
            error=str(e),
        )

    finally:
        # 清理临时 WAV 文件
        if temp_wav and temp_wav.exists():
            try:
                temp_wav.unlink()
            except OSError:
                pass


def cleanup_media(filepath: Path, source_id: str) -> None:
    """清理原始媒体文件

    转写完成后删除不再需要的原始音视频文件以节省磁盘空间。

    Args:
        filepath: 原始媒体文件路径
        source_id: 来源标识（用于日志）
    """
    try:
        if filepath.exists():
            filepath.unlink()
            console.log(f"[dim]已清理媒体文件: {source_id} ({filepath.name})[/]")
    except OSError as e:
        console.log(f"[yellow]清理媒体文件失败: {source_id} - {e}[/]")


async def transcribe_file_async(
    filepath: Path,
    config: Config,
    source_id: str,
    title: str,
    author: str,
) -> TranscriptResult:
    """transcribe_file 的异步包装，避免阻塞事件循环。

    将同步的 FFmpeg 转换和模型推理放到线程池中执行。
    """
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,  # 使用默认线程池
        lambda: transcribe_file(filepath, config, source_id, title, author),
    )
