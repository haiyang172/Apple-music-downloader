"""Validate a staged track and prepare the selected FFmpeg output."""

from base64 import b64encode
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time

from mutagen import File, MutagenError
from mutagen.aac import AAC
from mutagen.ac3 import AC3
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE, _WaveFile

from storage import clean_name


OUTPUTS = {
    "original": "保留原音轨",
    "flac": "FLAC",
    "mp3": "MP3",
    "aac": "AAC（M4A）",
    "wav": "WAV",
    "opus": "Opus",
}
MP3_BITRATES = (128, 192, 256, 320)
DEFAULT_MP3_BITRATE = 320

_NATIVE_CODECS = {"AAC": "mp4a.40.2", "ALAC": "alac", "ATMOS": "ec-3"}
_EXTENSIONS = {"original": ".m4a", "aac": ".m4a", "flac": ".flac", "mp3": ".mp3", "wav": ".wav", "opus": ".opus"}
_TEXT_TAGS = ("\xa9nam", "\xa9alb", "\xa9ART", "aART", "\xa9gen", "\xa9day", "\xa9cmt", "\xa9wrt", "\xa9lyr", "trkn", "disk", "covr")


class Cancelled(RuntimeError):
    pass


_READABLE_AUDIO = (MP4, AAC, MP3, FLAC, OggFLAC, OggOpus, OggVorbis, WAVE, AC3)
_STREAM_CODECS = {
    AAC: "AAC", FLAC: "FLAC", OggFLAC: "FLAC", OggOpus: "Opus", OggVorbis: "Vorbis",
}
_MP4_CODECS = {
    "mp4a.40.1": "AAC Main", "mp4a.40.2": "AAC-LC", "mp4a.40.3": "AAC-SSR", "mp4a.40.4": "AAC-LTP",
    "mp4a.40.23": "AAC-LD", "mp4a.40.39": "AAC-ELD", "mp4a.40.42": "xHE-AAC", "mp4a.69": "MPEG 音频",
    "mp4a.6b": "MPEG 音频", "mp4a.66": "AAC Main", "mp4a.67": "AAC-LC", "mp4a.68": "AAC-SSR",
    "alac": "ALAC", "flac": "FLAC", "opus": "Opus", "ac-3": "AC-3", "ec-3": "E-AC-3",
    "lpcm": "PCM", "sowt": "PCM", "twos": "PCM", "in24": "PCM", "in32": "PCM",
}
_WAVE_SUBTYPE_SUFFIX = bytes.fromhex("00001000800000aa00389b71")


def _positive(value, maximum):
    return round(value) if type(value) in (int, float) and 1 <= value <= maximum else None


def _mp4_codec(info):
    code = info.codec.strip().lower()
    description = (info.codec_description or "").upper()
    if code in ("mp4a.40.5", "mp4a.40.29") or "+SBR" in description or "+PS" in description:
        return "HE-AAC v2" if code == "mp4a.40.29" or "+PS" in description else "HE-AAC"
    return _MP4_CODECS.get(code, "MPEG-4 音频" if code.startswith("mp4a") else "MP4 音频")


def _wave_format(info, path):
    format_code, bits = info.audio_format, info.bits_per_sample
    if format_code == 0xFFFE:
        # Extensible WAVE can use more storage bits than the PCM signal uses.
        with path.open("rb") as stream:
            chunk = _WaveFile(stream)["fmt"]
            stream.seek(chunk.data_offset)
            fmt = stream.read(min(chunk.data_size, 40))
        if len(fmt) < 40 or fmt[28:40] != _WAVE_SUBTYPE_SUFFIX:
            return "WAV", None
        format_code, = struct.unpack_from("<I", fmt, 24)
        valid_bits, = struct.unpack_from("<H", fmt, 18)
        bits = valid_bits if 0 < valid_bits <= bits else None
    if format_code in (1, 3):
        return "WAV（PCM）", bits
    return "WAV", None


def _stream_details(track, path):
    kind, info = type(track), track.info
    if info is None:
        return None
    bitrate = getattr(info, "bitrate", None)
    bits = None
    if kind is MP4:
        codec = _mp4_codec(info)
        if codec in ("ALAC", "FLAC", "PCM"):
            bits = info.bits_per_sample
    elif kind is MP3:
        codec = {1: "MP1", 2: "MP2", 3: "MP3"}.get(info.layer, "MPEG 音频")
    elif kind is WAVE:
        codec, bits = _wave_format(info, path)
        if codec == "WAV":
            bitrate = None  # Mutagen's PCM rate formula does not apply to compressed WAV.
    elif kind is AC3:
        codec = "E-AC-3" if info.codec == "ec-3" else "AC-3"
    else:
        codec = _STREAM_CODECS[kind]
        if kind in (FLAC, OggFLAC):
            bits = info.bits_per_sample
    return {"codec": codec, "bitrate": _positive(bitrate, 1_000_000_000),
            "sample_rate": _positive(getattr(info, "sample_rate", None), 10_000_000),
            "channels": _positive(getattr(info, "channels", None), 64), "bits_per_sample": _positive(bits, 64)}


def audio_details(path):
    """Read available audio parameters from a regular file with Mutagen."""
    try:
        path = Path(path)
        if not path.is_file():
            return None
        track = File(path, options=_READABLE_AUDIO)
        return _stream_details(track, path) if track is not None else None
    except (MutagenError, OSError, ValueError, TypeError, KeyError, EOFError, RecursionError, struct.error):
        return None


def describe_audio(details):
    """Format the available parameters for display."""
    if not isinstance(details, dict):
        return ""
    codec = details.get("codec")
    codec = codec.strip() if isinstance(codec, str) else ""
    parts = [codec] if 0 < len(codec) <= 32 and codec.isprintable() else []
    if bitrate := _positive(details.get("bitrate"), 1_000_000_000):
        parts.append(f"{bitrate / 1000:.0f} kb/s")
    if sample_rate := _positive(details.get("sample_rate"), 10_000_000):
        parts.append(f"{sample_rate / 1000:g} kHz")
    if codec in ("ALAC", "FLAC", "PCM", "WAV（PCM）"):
        if bits := _positive(details.get("bits_per_sample"), 64):
            parts.append(f"{bits} 位")
    if channels := _positive(details.get("channels"), 64):
        parts.append({1: "单声道", 2: "双声道"}.get(channels, f"{channels} 声道"))
    return " · ".join(parts)


def _run(ffmpeg, arguments, stop, error):
    if stop.is_set():
        raise Cancelled("已取消下载。")
    try:
        process = subprocess.Popen(
            [str(ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-y", *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise RuntimeError("无法启动 FFmpeg，请检查安装组件。") from None
    with process:
        deadline = time.monotonic() + 300
        while True:
            if stop.is_set() or time.monotonic() >= deadline:
                if process.poll() is None:
                    process.kill()
                process.communicate()
                if stop.is_set():
                    raise Cancelled("已取消下载。")
                raise RuntimeError("FFmpeg 处理超时。")
            try:
                output, _ = process.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(error)
    return output


def _encoding(output, audio, quality, mp3_bitrate):
    if output == "original":
        return ["-c:a", "copy", "-movflags", "+faststart", "-f", "mp4"]
    bits = 16 if quality == "ALAC" and audio.bits_per_sample <= 16 else 24
    if output == "flac":
        return ["-c:a", "flac", "-sample_fmt", "s16"] if bits == 16 else [
            "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
        ]
    if output == "wav":
        return ["-c:a", f"pcm_s{bits}le", "-rf64", "auto"]
    channels = ["-ac", str(1 if audio.channels == 1 else 2)]
    if output == "mp3":
        return [*channels, "-c:a", "libmp3lame", "-b:a", f"{mp3_bitrate}k", "-ar", "44100"]
    if output == "opus":
        return [*channels, "-c:a", "libopus", "-b:a", "160k", "-ar", "48000"]
    return [*channels, "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "256k", "-ar", "44100", "-movflags", "+faststart"]


def _copy_tags(source, output, output_format):
    if not source.tags:
        return
    if output.suffix == ".m4a":
        audio = MP4(output)
        if audio.tags is None:
            audio.add_tags()
        tags = source.tags if output_format == "original" else {
            key: source.tags[key] for key in _TEXT_TAGS if key in source.tags
        }
        audio.tags.update(tags)
        audio.save()
        return
    covers = source.tags.get("covr", [])
    if not covers or output_format == "wav":
        return
    cover = covers[0]
    mime = "image/png" if cover.imageformat == 14 else "image/jpeg"
    if output_format == "mp3":
        try:
            tags = ID3(output)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(APIC(mime=mime, type=3, desc="Cover", data=cover))
        tags.save(output, v2_version=3)
    else:
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.data = cover
        if output_format == "flac":
            audio = FLAC(output)
            audio.add_picture(picture)
        else:
            audio = OggOpus(output)
            audio["metadata_block_picture"] = b64encode(picture.write()).decode("ascii")
        audio.save()


def _decode_duration(ffmpeg, path, stop):
    progress = _run(ffmpeg, [
        "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:a:0",
        "-progress", "pipe:1", "-f", "null", "-",
    ], stop, "输出音频无法完整解码。")
    values = []
    for line in progress.splitlines():
        field, _, value = line.partition(b"=")
        if field == b"out_time_us" and value.isdigit():
            values.append(int(value))
    if not values or values[-1] <= 0:
        raise RuntimeError("无法确认输出音频时长。")
    return values[-1] / 1_000_000


def prepare(ffmpeg, stage, artifact, quality, output_format, mp3_bitrate, stop):
    """Return the verified staged file, filename stem and input audio parameters."""
    if output_format not in OUTPUTS or quality not in _NATIVE_CODECS:
        raise ValueError("无法识别所选音质或输出格式。")
    if output_format == "mp3" and mp3_bitrate not in MP3_BITRATES:
        raise ValueError("请选择支持的 MP3 码率。")
    expected = artifact.get("duration")
    if not isinstance(expected, (int, float)) or not math.isfinite(expected) or expected <= 0:
        raise ValueError("无法核对曲目原始时长。")
    stage = Path(stage).resolve(strict=True)
    source = Path(artifact["path"]).resolve(strict=True)
    if not source.is_relative_to(stage) or not source.is_file() or source.suffix.lower() not in (".m4a", ".mp4"):
        raise ValueError("下载工具返回了无效的暂存文件。")
    try:
        original = MP4(source)
    except MutagenError:
        raise RuntimeError("无法识别下载的音频文件。") from None
    if original.info.codec != _NATIVE_CODECS[quality]:
        raise RuntimeError("下载的实际音轨与所选音质不符。")

    title = (original.get("\xa9nam") or [artifact.get("title") or source.stem])[0]
    artist = (original.get("\xa9ART") or [artifact.get("artist") or ""])[0]
    stem = clean_name(title, 90)
    if artist:
        stem += " - " + clean_name(artist, 52)

    extension = _EXTENSIONS[output_format]
    descriptor, name = tempfile.mkstemp(prefix="verified-", suffix=extension, dir=stage)
    os.close(descriptor)
    output = Path(name)
    try:
        _run(ffmpeg, [
            "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", str(source),
            "-map", "0:a:0", "-map_metadata", "0", "-map_chapters", "-1",
            *_encoding(output_format, original.info, quality, mp3_bitrate), str(output),
        ], stop, f"FFmpeg 无法生成 {OUTPUTS[output_format]} 音频。")
        _copy_tags(original, output, output_format)
        actual = _decode_duration(ffmpeg, output, stop)
        tolerance = max(0.35, min(2, expected * 0.01))
        if abs(actual - expected) > tolerance:
            raise RuntimeError(f"音轨时长为 {actual:.1f} 秒，应为 {expected:.1f} 秒。请检查该音质是否只提供试听片段。")
        return output, stem, _stream_details(original, source)
    except MutagenError:
        output.unlink(missing_ok=True)
        raise RuntimeError("无法保留音频标签。") from None
    except BaseException:
        output.unlink(missing_ok=True)
        raise
