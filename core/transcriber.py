"""faster-whisper 语音转写模块 - 将音视频文件转写为文本"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console

from shared.config import Config
from shared.protocols import TranscriptResult

console = Console()

# ── 全局模型缓存 ────────────────────────────────────────────────

_model_cache: dict[str, Any] = {}


def _get_model(config: Config) -> Any:
    """获取或加载 faster-whisper 模型

    使用全局缓存避免重复加载模型。首次调用时会从 Hugging Face 下载模型。
    采用 CPU int8 量化以降低内存占用，适合 2C4G 服务器。

    Args:
        config: 全局配置，transcribe.model 指定模型大小（base/small/medium/large-v3）

    Returns:
        faster_whisper.WhisperModel 实例
    """
    model_size = config.transcribe.model
    cache_key = f"fw-{model_size}"
    if cache_key not in _model_cache:
        from faster_whisper import WhisperModel

        console.log(f"[bold blue]正在加载 faster-whisper {model_size} 模型（首次可能需要下载）...[/]")
        _model_cache[cache_key] = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
        )
        console.log(f"[bold green]faster-whisper {model_size} 模型加载完成[/]")
    return _model_cache[cache_key]


# ── 音频预处理 ──────────────────────────────────────────────────


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


# ── 结果保存 ────────────────────────────────────────────────────


def _save_transcript(
    text: str,
    segments: list[dict[str, Any]],
    source_id: str,
    title: str,
    author: str,
    language: str,
    duration_seconds: float,
    output_dir: Path,
) -> tuple[Path, Path]:
    """保存转写结果到文本文件和 JSON 文件

    Args:
        text: 完整转写文本
        segments: 带时间戳的分段列表，每项包含 text/start/end 字段
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

    # 保存带时间戳分段的 JSON
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


# ── 主转写流程 ──────────────────────────────────────────────────


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
    2. 加载 faster-whisper 模型（全局缓存，仅首次加载，CPU int8）
    3. 执行语音转写（自动语言识别 + 分段时间戳）
    4. 保存 .txt 和 .json 格式的转写结果
    5. 返回结构化的 TranscriptResult

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

    temp_wav: Path | None = None

    try:
        # Step 1: FFmpeg 提取音频
        temp_wav = Path(tempfile.mktemp(suffix=".wav"))
        console.log("[dim]Step 1: 提取音频...[/]")
        _extract_audio(filepath, temp_wav)

        # 获取音频时长
        duration = _get_audio_duration(temp_wav)

        # Step 2 & 3: 加载模型并转写
        console.log("[dim]Step 2-3: 加载模型并转写...[/]")
        model = _get_model(config)
        language_hint = config.transcribe.language or None
        segments_iter, info = model.transcribe(
            str(temp_wav),
            language=language_hint,
            beam_size=5,
        )
        whisper_segments = list(segments_iter)

        # 从 faster-whisper 结果中提取文本和分段
        segment_data: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for seg in whisper_segments:
            segment_data.append({"text": seg.text, "start": round(seg.start, 2), "end": round(seg.end, 2)})
            text_parts.append(seg.text)

        text = "".join(text_parts)
        language = info.language

        if not text.strip():
            return TranscriptResult(
                success=False,
                source_id=source_id,
                title=title,
                duration_seconds=duration,
                error="转写结果为空",
            )

        # Step 4: 保存结果
        console.log("[dim]Step 4: 保存转写结果...[/]")
        output_dir = Path(config.transcribe.output_dir)
        txt_path, json_path = _save_transcript(
            text=text,
            segments=segment_data,
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


# ── 辅助接口 ────────────────────────────────────────────────────


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
