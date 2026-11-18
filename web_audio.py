"""Prepare downloaded music for publication in the selected audio format."""

import math
from pathlib import Path
import tempfile

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

import audio
from engine import Cancelled
from local_convert import _AUDIO_INPUTS, _AUDIO_OUTPUTS, _ffmpeg, _local_file
from music_tagging import copy_front_cover
from storage import destination_for


_SOURCES = {".mp3": MP3, ".flac": FLAC, ".m4a": MP4}


def _source_audio(source):
    container = _SOURCES.get(source.suffix.lower())
    if container is None:
        raise ValueError("下载的原音轨格式应为 MP3、FLAC 或 M4A。")
    try:
        track = container(source)
    except (MutagenError, ValueError):
        raise RuntimeError("无法读取下载音轨的实际编码。") from None
    if (not math.isfinite(track.info.length) or track.info.length <= 0
            or not 1 <= track.info.channels <= 64):
        raise RuntimeError("下载音轨的时长或声道信息无效。")
    if isinstance(track, MP3) and track.info.layer != 3:
        raise RuntimeError("下载音轨的实际编码不是 MP3。")
    if isinstance(track, MP4) and not (track.info.codec.startswith("mp4a.40.") or track.info.codec == "alac"):
        raise RuntimeError("下载的 M4A 音轨不是受支持的 AAC 或 ALAC。")
    return track


def prepare_web_audio(source, stage, ffmpeg, output_format, mp3_bitrate, stop):
    """Prepare a verified provider download while preserving its original file."""
    if output_format not in audio.OUTPUTS:
        raise ValueError("无法识别所选输出格式。")
    if output_format == "mp3" and mp3_bitrate not in audio.MP3_BITRATES:
        raise ValueError("请选择支持的 MP3 码率。")
    if stop.is_set():
        raise Cancelled("已取消转换。")
    source = _local_file(Path(source))
    stage = destination_for(stage)
    if not source.is_relative_to(stage):
        raise ValueError("音轨不在本次下载的暂存目录内。")
    original = _source_audio(source)
    demuxer = _AUDIO_INPUTS[source.suffix.lower()]
    mov_options = ["-enable_drefs", "0", "-use_absolute_path", "0"] if demuxer == "mov" else []
    input_options = ["-protocol_whitelist", "file", "-f", demuxer, *mov_options, "-i", str(source)]
    if output_format == "original":
        # The provider has already decoded and checked this complete download.
        return source

    format_key = "m4a" if output_format == "aac" else output_format
    encoding = list(_AUDIO_OUTPUTS[format_key])
    if output_format == "mp3":
        encoding.extend(["-b:a", f"{mp3_bitrate}k"])
    elif output_format == "aac":
        encoding.extend(["-ac", str(1 if original.info.channels == 1 else 2)])
    working = Path(tempfile.mkdtemp(prefix="audio-output-", dir=stage))
    output = working / ("converted." + format_key)
    try:
        _ffmpeg(ffmpeg, [*input_options, "-map", "0:a:0", "-map_metadata", "0", "-map_chapters", "-1",
                         *encoding, str(output)], stop)
        if not output.is_file() or not output.stat().st_size or audio.audio_details(output) is None:
            raise RuntimeError("转换未生成可读取的完整音轨。")
        try:
            actual = audio._decode_duration(ffmpeg, output, stop)
        except audio.Cancelled as error:
            raise Cancelled(str(error)) from None
        expected = original.info.length
        tolerance = max(0.35, min(2, expected * 0.01))
        if abs(actual - expected) > tolerance:
            raise RuntimeError("输出音轨时长与原音轨不一致。")
        copy_front_cover(source, output)
        return output
    except BaseException:
        output.unlink(missing_ok=True)
        working.rmdir()
        raise
