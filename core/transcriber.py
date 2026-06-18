"""faster-whisper 语音转写模块 - 将音视频文件转写为文本"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from shared.config import Config
from shared.protocols import TranscriptResult

try:
    from faster_whisper import WhisperModel  # pyright: ignore[reportMissingImports]
except ImportError:
    WhisperModel = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════

_model_cache: dict[str, Any] = {}


def _get_model(config: Config) -> Any:
    """获取或加载 faster-whisper 模型"""
    if WhisperModel is None:
        raise ImportError("transcribe dependencies not installed. Run: uv pip install -e '.[transcribe]'")
    model_size = config.transcribe.model
    cache_key = f"fw-{model_size}"
    if cache_key not in _model_cache:
        logger.info("正在加载 faster-whisper %s 模型（首次可能需要下载）...", model_size)
        _model_cache[cache_key] = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
        )
        logger.info("faster-whisper %s 模型加载完成", model_size)
    return _model_cache[cache_key]


# ── 音频提取 ──


def _extract_audio(filepath: Path, output_path: Path) -> None:
    """使用 FFmpeg 提取音频并转换为 16kHz 单声道 WAV"""
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
    logger.debug("执行 FFmpeg: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 音频提取失败 (返回码 {result.returncode}): {result.stderr}")


def _get_audio_duration(wav_path: Path) -> float:
    """获取 WAV 文件时长"""
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
    except subprocess.SubprocessError, ValueError:
        pass
    return 0.0


# ── 结果保存 ──


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
    """保存转写结果到文本文件和 JSON 文件"""
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_path = output_dir / f"{source_id}.txt"
    json_path = output_dir / f"{source_id}.json"

    txt_path.write_text(text, encoding="utf-8")

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


# ═══════════════════════════════════════════════════════════


def transcribe_file(
    filepath: Path,
    config: Config,
    source_id: str,
    title: str,
    author: str,
) -> TranscriptResult:
    """将音视频文件转写为文本"""
    logger.info("开始转写: %s (%s)", title, source_id)

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
        import tempfile as _tempfile

        fd, tmp_path = _tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        temp_wav = Path(tmp_path)
        logger.debug("Step 1: 提取音频...")
        _extract_audio(filepath, temp_wav)

        duration = _get_audio_duration(temp_wav)

        # Step 2 & 3: 加载模型并转写
        logger.debug("Step 2-3: 加载模型并转写...")
        model = _get_model(config)
        language_hint = config.transcribe.language or None
        segments_iter, info = model.transcribe(
            str(temp_wav),
            language=language_hint,
            beam_size=5,
        )
        whisper_segments = list(segments_iter)

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
        logger.debug("Step 4: 保存转写结果...")
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

        logger.info("转写完成: %s (时长 %.1fs, 文本 %s 字)", title, duration, len(text))

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
        logger.error("转写失败: %s - %s", title, e)
        return TranscriptResult(
            success=False,
            source_id=source_id,
            title=title,
            error=str(e),
        )

    finally:
        if temp_wav and temp_wav.exists():
            try:
                temp_wav.unlink()
            except OSError:
                pass


# ── 辅助接口 ──


def cleanup_media(filepath: Path, source_id: str) -> None:
    """清理原始媒体文件"""
    try:
        if filepath.exists():
            filepath.unlink()
            logger.debug("已清理媒体文件: %s (%s)", source_id, filepath.name)
    except OSError as e:
        logger.warning("清理媒体文件失败: %s - %s", source_id, e)


async def transcribe_file_async(
    filepath: Path,
    config: Config,
    source_id: str,
    title: str,
    author: str,
) -> TranscriptResult:
    """transcribe_file 的异步包装"""
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: transcribe_file(filepath, config, source_id, title, author),
    )
