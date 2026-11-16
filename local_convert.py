"""Convert selected local media with the bundled FFmpeg."""

from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import time

from audio import MP3_BITRATES
from engine import Cancelled, NO_WINDOW
from storage import destination_for, publish, volume_identity


_AUDIO_INPUTS = {
    ".aac": "aac", ".aif": "aiff", ".aiff": "aiff", ".ape": "ape", ".flac": "flac",
    ".m4a": "mov", ".mp3": "mp3", ".oga": "ogg", ".ogg": "ogg", ".opus": "ogg",
    ".wav": "wav", ".wma": "asf", ".wv": "wv",
}
_VIDEO_INPUTS = {
    ".avi": "avi", ".flv": "flv", ".m4v": "mov", ".mkv": "matroska", ".mov": "mov",
    ".mp4": "mov", ".mpeg": "mpeg", ".mpg": "mpeg", ".mts": "mpegts",
    ".m2ts": "mpegts", ".ts": "mpegts", ".webm": "matroska", ".wmv": "asf",
}
_AUDIO_OUTPUTS = {
    "mp3": ("-c:a", "libmp3lame", "-ac", "2"),
    "m4a": ("-c:a", "aac", "-profile:a", "aac_low", "-b:a", "256k", "-movflags", "+faststart"),
    "flac": ("-c:a", "flac"),
    "wav": ("-c:a", "pcm_s24le", "-rf64", "auto"),
    "opus": ("-c:a", "libopus", "-b:a", "160k", "-ar", "48000", "-ac", "2"),
}
_H264 = ("-c:v", "libopenh264", "-b:v", "4M", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "256k")
_VIDEO_OUTPUTS = {
    "mp4": (*_H264, "-movflags", "+faststart"),
    "mkv": _H264,
    "webm": ("-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-deadline", "good",
             "-cpu-used", "3", "-pix_fmt", "yuv420p", "-c:a", "libopus", "-b:a", "160k"),
}


def formats_for(path):
    """Return the candidate output keys for an input file's extension."""
    suffix = Path(path).suffix.lower()
    if suffix in _VIDEO_INPUTS:
        return set(_AUDIO_OUTPUTS) | set(_VIDEO_OUTPUTS)
    return set(_AUDIO_OUTPUTS) if suffix in _AUDIO_INPUTS else set()


def _local_file(path):
    source = Path(path)
    if (not source.is_absolute() or source.drive.startswith("\\\\") or ".." in source.parts
            or ":" in source.name):
        raise ValueError("请选择本机的音频或视频文件。")
    destination_for(source.parent)
    info = source.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    if (not stat.S_ISREG(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or info.st_size == 0):
        raise ValueError("无法读取文件，请选择非空的普通音频或视频文件。")
    return source


def _ffmpeg(ffmpeg, arguments, stop):
    if stop.is_set():
        raise Cancelled("已取消转换。")
    try:
        process = subprocess.Popen(
            [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-n", *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except OSError:
        raise RuntimeError("无法启动 FFmpeg，请检查安装组件。") from None
    with process:
        deadline = time.monotonic() + 4 * 60 * 60
        try:
            while process.poll() is None:
                if stop.wait(0.15):
                    raise Cancelled("已取消转换。")
                if time.monotonic() >= deadline:
                    raise RuntimeError("FFmpeg 转换超时。")
        finally:
            if process.poll() is None:
                process.kill()
    if stop.is_set():
        raise Cancelled("已取消转换。")
    if process.returncode:
        raise RuntimeError("FFmpeg 无法转换该文件，请检查是否有可读的音轨或视频轨。")


def convert_file(path, format_key, ffmpeg, destination, stop, *, mp3_bitrate=320, expected_volume=None):
    """Convert one file into an existing destination and publish without overwriting."""
    source = _local_file(path)
    if format_key not in formats_for(source):
        raise ValueError("这个文件不支持所选输出格式。")
    if format_key == "mp3" and mp3_bitrate not in MP3_BITRATES:
        raise ValueError("请选择支持的 MP3 码率。")
    destination = destination_for(destination)
    if expected_volume is None:
        expected_volume = volume_identity(destination.parent)
    suffix = source.suffix.lower()
    demuxer = _AUDIO_INPUTS.get(suffix) or _VIDEO_INPUTS[suffix]
    # Only open these single-file input formats. Keep the MOV demuxer's external data references disabled.
    input_options = ("-enable_drefs", "0", "-use_absolute_path", "0") if demuxer == "mov" else ()
    if format_key in _VIDEO_OUTPUTS:
        # Capital V excludes embedded album art from the video stream selection.
        arguments = ("-map", "0:V:0", "-map", "0:a:0?", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                     *_VIDEO_OUTPUTS[format_key])
    else:
        arguments = ("-map", "0:a:0", *_AUDIO_OUTPUTS[format_key])
        if format_key == "mp3":
            arguments += ("-b:a", f"{mp3_bitrate}k")
    extension = "." + format_key
    stem = source.stem + " [converted]" if source.parent == destination and suffix == extension else source.stem
    with TemporaryDirectory(prefix="amdl-convert-") as directory:
        output = Path(directory) / ("converted" + extension)
        _ffmpeg(ffmpeg, ["-protocol_whitelist", "file", "-f", demuxer, *input_options, "-i", str(source),
                         *arguments, "-map_metadata", "0", "-map_chapters", "-1", str(output)], stop)
        if not output.is_file() or not output.stat().st_size:
            raise RuntimeError("转换未生成可保存的文件。")
        return publish(output, destination, stem, None, expected_volume=expected_volume)
