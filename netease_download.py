"""Save complete native tracks offered by NetEase Music to the current listener."""

from hashlib import md5
from http.client import HTTPException
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import audio
from engine import Cancelled


_SONG = re.compile(r"https://music\.163\.com/song\?id=([1-9][0-9]{0,19})")
_CDN = re.compile(r"m[0-9]{1,6}\.music\.126\.net")
_UNAVAILABLE = "网易云音乐未提供这首歌的公开完整音轨。"
_ACCOUNT_UNAVAILABLE = "网易云音乐未向当前账号提供这首歌的完整 MP3。"
_NATIVE_UNAVAILABLE = "网易云音乐未向当前账号提供所选音质的完整音轨。"
_NETWORK_ERROR = "读取网易云音乐音轨失败，请检查网络或稍后重试。"
_MP3_MIME = {"audio/mpeg", "audio/mp3"}
_MAX_BYTES = 128 * 1024 ** 2
_MAX_NATIVE_BYTES = 1024 * 1024 ** 2
_CHUNK = 128 * 1024
_LEVELS = {"128": "standard", "192": "higher", "320": "exhigh", "flac": "lossless", "hires": "hires"}
_QUALITY_LABELS = {"standard": "标准", "higher": "较高", "exhigh": "极高", "lossless": "无损", "hires": "Hi-Res"}
_FORMATS = {
    "mp3": ("mp3", _MP3_MIME, (None, "mp3")),
    "flac": ("flac", {"audio/flac", "audio/x-flac", "application/octet-stream"}, (None, "flac")),
    "m4a": ("mov", {"audio/mp4", "audio/x-m4a", "audio/m4a", "application/octet-stream"}, (None, "aac", "m4a")),
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消下载。")


def _open(url, accept):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": accept, "Accept-Encoding": "identity"})
    return build_opener(_NoRedirect()).open(request, timeout=10)


def _https_cdn(value, media_type="mp3"):
    if media_type not in _FORMATS:
        raise RuntimeError(_UNAVAILABLE)
    if not isinstance(value, str) or not 0 < len(value) <= 8192 or any(
            not 33 <= ord(char) <= 126 or char == "\\" for char in value):
        raise RuntimeError(_UNAVAILABLE)
    try:
        target = urlsplit(value)
    except ValueError:
        raise RuntimeError(_UNAVAILABLE) from None
    if (target.scheme not in ("https", "http") or not _CDN.fullmatch(target.netloc)
            or not target.path.startswith("/") or not target.path.lower().endswith("." + media_type) or target.fragment):
        raise RuntimeError(_UNAVAILABLE)
    # Legacy redirects advertise HTTP. The same signed media locator also supports TLS.
    return target._replace(scheme="https").geturl()


def _legacy_account_source(session, identifier, stop):
    from netease_auth import netease_api_request

    song_id = int(identifier)
    reply = netease_api_request(session, "/api/song/enhance/player/url", {
        "id": song_id, "ids": f"[{song_id}]", "br": 320000,
    })
    if reply.get("code") in (301, 401):
        raise RuntimeError("网易云音乐登录已失效，请重新扫码。")
    items = reply.get("data")
    if type(reply.get("code")) is not int or reply["code"] != 200 or not isinstance(items, list) or len(items) != 1:
        raise RuntimeError(_ACCOUNT_UNAVAILABLE)
    item = items[0]
    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] != song_id:
        raise RuntimeError(_ACCOUNT_UNAVAILABLE)
    if item.get("freeTrialInfo") is not None:
        raise RuntimeError("网易云音乐仅向当前账号提供试听片段。")
    if (type(item.get("code")) is not int or item["code"] != 200 or item.get("type") != "mp3"
            or item.get("encodeType") not in (None, "mp3")):
        raise RuntimeError(_ACCOUNT_UNAVAILABLE)
    size, checksum = item.get("size"), item.get("md5")
    if (type(size) is not int or not 3 <= size <= _MAX_BYTES or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-fA-F]{32}", checksum)):
        raise RuntimeError("网易云音乐没有返回可验证的音轨文件信息。")
    try:
        location = _https_cdn(item.get("url"))
    except RuntimeError:
        raise RuntimeError(_ACCOUNT_UNAVAILABLE) from None

    _check_stop(stop)
    params = {"c": json.dumps([{"id": song_id}], separators=(",", ":"))}
    details = netease_api_request(session, "/api/v3/song/detail", params)
    if details.get("code") in (301, 401):
        raise RuntimeError("网易云音乐登录已失效，请重新扫码。")
    songs = details.get("songs")
    if type(details.get("code")) is not int or details["code"] != 200 or not isinstance(songs, list) or len(songs) != 1:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    song = songs[0]
    duration_ms = (song.get("dt") if isinstance(song, dict) and type(song.get("id")) is int
                   and song["id"] == song_id else None)
    if type(duration_ms) is not int or not 0 < duration_ms <= 86_400_000:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    bitrate = item.get("br")
    if type(bitrate) is not int or not 8_000 <= bitrate <= 1_000_000_000:
        bitrate = None
    return location, duration_ms / 1000, size, checksum.lower(), "mp3", None, bitrate


def _account_source(session, identifier, stop):
    """Keep the original private MP3 selection result for existing callers."""
    return _legacy_account_source(session, identifier, stop)[:4]


def _quality_source(session, identifier, stop, source_quality):
    from netease_auth import netease_api_request

    song_id = int(identifier)
    # MusicUnlock's highest native request uses the existing bitrate route.
    # A denied/trial response is final; never replace it with a different source or session.
    if source_quality == "auto":
        level = None
        reply = netease_api_request(session, "/api/song/enhance/player/url", {
            "id": song_id, "ids": f"[{song_id}]", "br": 999000,
        })
    else:
        level = _LEVELS[source_quality]
        reply = netease_api_request(session, "/api/song/enhance/player/url/v1", {
            "ids": f"[{song_id}]", "level": level, "encodeType": "flac",
        })
    if reply.get("code") in (301, 401):
        raise RuntimeError("网易云音乐登录已失效，请重新扫码。")
    items = reply.get("data")
    if type(reply.get("code")) is not int or reply["code"] != 200 or not isinstance(items, list) or len(items) != 1:
        raise RuntimeError(_NATIVE_UNAVAILABLE)
    item = items[0]
    if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] != song_id:
        raise RuntimeError(_NATIVE_UNAVAILABLE)
    if type(item.get("code")) is int and item["code"] in (403, 404, 410, 451):
        raise RuntimeError("该曲目当前不可用，可能受版权、会员或地区限制。")
    if item.get("freeTrialInfo") is not None:
        raise RuntimeError("网易云音乐仅向当前账号提供试听片段。")
    media_type, actual_level = item.get("type"), item.get("level")
    # The legacy auto route reports encodeType "mp3" even for FLAC, so only the
    # explicit v1 route carries a meaningful encodeType to verify.
    if (type(item.get("code")) is not int or item["code"] != 200
            or not isinstance(media_type, str) or media_type not in _FORMATS
            or (source_quality != "auto" and item.get("encodeType") not in _FORMATS[media_type][2])):
        raise RuntimeError(_NATIVE_UNAVAILABLE)
    if source_quality == "auto":
        if not isinstance(actual_level, str) or actual_level not in _QUALITY_LABELS:
            actual_level = "lossless" if media_type == "flac" else None
            if media_type == "mp3":
                actual_level = next((candidate for rate, candidate in ((128000, "standard"), (192000, "higher"),
                                                                       (320000, "exhigh"))
                                     if _matching_bitrate(item.get("br"), rate)), None)
    else:
        if not isinstance(actual_level, str) or actual_level not in _QUALITY_LABELS:
            raise RuntimeError(_NATIVE_UNAVAILABLE)
        if actual_level != level:
            raise RuntimeError(f"网易云音乐实际提供{_QUALITY_LABELS[actual_level]}音质，与所选音质不符。")
    lossless = actual_level in ("lossless", "hires")
    if lossless != (media_type == "flac"):
        raise RuntimeError("网易云音乐返回的音轨格式与实际音质不符。")
    size, checksum, bitrate = item.get("size"), item.get("md5"), item.get("br")
    if (type(size) is not int or not 12 <= size <= _MAX_NATIVE_BYTES or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-fA-F]{32}", checksum)
            or type(bitrate) is not int or not 8_000 <= bitrate <= 1_000_000_000):
        raise RuntimeError("网易云音乐没有返回可验证的音轨文件信息。")
    if media_type == "mp3" and actual_level is not None:
        wanted = {"standard": 128000, "higher": 192000, "exhigh": 320000}[actual_level]
        if not _matching_bitrate(bitrate, wanted):
            raise RuntimeError("网易云音乐返回的码率与实际音质不符。")
    try:
        location = _https_cdn(item.get("url"), media_type)
    except RuntimeError:
        raise RuntimeError(_NATIVE_UNAVAILABLE) from None

    _check_stop(stop)
    details = netease_api_request(session, "/api/v3/song/detail", {
        "c": json.dumps([{"id": song_id}], separators=(",", ":")),
    })
    if details.get("code") in (301, 401):
        raise RuntimeError("网易云音乐登录已失效，请重新扫码。")
    songs = details.get("songs")
    if type(details.get("code")) is not int or details["code"] != 200 or not isinstance(songs, list) or len(songs) != 1:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    song = songs[0]
    if not isinstance(song, dict) or type(song.get("id")) is not int or song["id"] != song_id:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    duration_ms = song.get("dt")
    if type(duration_ms) is not int or not 0 < duration_ms <= 86_400_000:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    return location, duration_ms / 1000, size, checksum.lower(), media_type, actual_level, bitrate


def _media_prefix(prefix, media_type):
    if media_type == "mp3":
        return prefix[:3] == b"ID3" or prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
    if media_type == "flac":
        return prefix[:4] == b"fLaC"
    return prefix[4:8] == b"ftyp" and 12 <= int.from_bytes(prefix[:4], "big") <= 4096


def _copy_media(location, stream, stop, on_status, *, expected_size=None, checksum=None, media_type="mp3"):
    if media_type not in _FORMATS:
        raise ValueError("无效的网易云音乐音轨格式。")
    failure = _NATIVE_UNAVAILABLE if expected_size is not None else _UNAVAILABLE
    maximum = _MAX_BYTES if expected_size is None else _MAX_NATIVE_BYTES
    prefix_length = {"mp3": 3, "flac": 4, "m4a": 12}[media_type]
    mime_types = _FORMATS[media_type][1]
    try:
        with _open(location, ", ".join(sorted(mime_types))) as response:
            headers = response.headers
            if (response.status != 200 or headers.get_content_type() not in mime_types
                    or headers.get("Content-Encoding") not in (None, "identity")
                    or headers.get("Content-Range") or headers.get("Transfer-Encoding")):
                raise RuntimeError(failure)
            sizes = headers.get_all("Content-Length", [])
            if len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,10}", sizes[0]) or not prefix_length <= int(sizes[0]) <= maximum:
                raise RuntimeError("网易云音乐音轨大小无效或超过下载上限。")
            total = int(sizes[0])
            if expected_size is not None and total != expected_size:
                raise RuntimeError("网易云音乐返回的文件大小与完整音轨不符。")
            digest = md5(usedforsecurity=False) if checksum else None
            received = 0
            started = last_update = time.monotonic()
            prefix = b""
            while received < total:
                _check_stop(stop)
                now = time.monotonic()
                if now - started > (180 if expected_size is None else 1200):
                    raise RuntimeError("网易云音乐音轨下载超时。")
                chunk = response.read1(min(_CHUNK, total - received))
                if not chunk or len(chunk) > total - received:
                    raise RuntimeError("网易云音乐音轨下载不完整。")
                if len(prefix) < prefix_length:
                    prefix = (prefix + chunk)[:prefix_length]
                    if len(prefix) == prefix_length and not _media_prefix(prefix, media_type):
                        raise RuntimeError("网易云音乐没有返回可读取的音轨文件。")
                stream.write(chunk)
                if digest is not None:
                    digest.update(chunk)
                received += len(chunk)
                if now - last_update >= 0.5:
                    on_status(f"正在下载网易云音乐 · {received * 100 // total}%")
                    last_update = now
            if digest is not None and digest.hexdigest() != checksum:
                raise RuntimeError("网易云音乐音轨校验失败，请重试。")
    except HTTPError as error:
        error.close()
        if error.code == 401:
            message = "网易云音乐登录已失效，请重新登录。"
        elif error.code == 403:
            message = ("网易云音乐媒体服务器拒绝了当前网络，常见于海外 IP；"
                       "请使用国内网络或代理后重试。")
        elif error.code in (404, 410, 451):
            message = failure
        else:
            message = _NETWORK_ERROR
        raise RuntimeError(message) from None
    except (URLError, TimeoutError, HTTPException):
        raise RuntimeError(_NETWORK_ERROR) from None


def _check_duration(path, ffmpeg, stop, expected, media_type="mp3"):
    demuxer = _FORMATS[media_type][0]
    input_options = ["-enable_drefs", "0", "-use_absolute_path", "0"] if demuxer == "mov" else []
    try:
        progress = audio._run(ffmpeg, [
            "-protocol_whitelist", "file,pipe", "-f", demuxer, *input_options, "-i", str(path), "-map", "0:a:0",
            "-progress", "pipe:1", "-f", "null", "-",
        ], stop, "网易云音乐音轨无法完整解码。")
    except audio.Cancelled:
        raise Cancelled("已取消下载。") from None
    duration_us = 0
    for line in progress.splitlines():
        field, _, value = line.partition(b"=")
        if field == b"out_time_us" and value.isdigit():
            duration_us = int(value)
    if duration_us <= 0:
        raise RuntimeError("无法确认网易云音乐音轨时长。")
    actual = duration_us / 1_000_000
    tolerance = min(2, max(1.25, expected * 0.01))
    if abs(actual - expected) > tolerance:
        raise RuntimeError(f"返回的音轨为 {actual:.1f} 秒，曲目为 {expected:.1f} 秒，可能仅为试听或媒体不完整。")


def _matching_bitrate(actual, expected):
    return type(actual) in (int, float) and abs(actual - expected) <= expected * 0.05


def _check_source_quality(path, media_type, source_quality, level, declared_bitrate):
    details = audio.audio_details(path)
    if not isinstance(details, dict):
        raise RuntimeError("无法确认网易云音乐音轨的实际格式和音质。")
    codec = details.get("codec")
    valid_codec = (codec == "MP3" if media_type == "mp3" else codec == "FLAC" if media_type == "flac"
                   else isinstance(codec, str) and (codec.startswith("AAC") or codec in ("HE-AAC", "HE-AAC v2", "xHE-AAC")))
    if not valid_codec:
        raise RuntimeError("网易云音乐音轨的实际编码与返回的格式不符。")
    if media_type == "mp3" and source_quality in ("128", "192", "320"):
        if not _matching_bitrate(details.get("bitrate"), int(source_quality) * 1000):
            raise RuntimeError("网易云音乐公开或账号音轨的实际码率与所选音质不符。")
    elif media_type == "mp3" and declared_bitrate is not None:
        if not _matching_bitrate(details.get("bitrate"), declared_bitrate):
            raise RuntimeError("网易云音乐音轨的实际码率与返回的信息不符。")
    if media_type == "flac":
        bits, sample_rate = details.get("bits_per_sample"), details.get("sample_rate")
        if type(bits) is not int or bits <= 0 or type(sample_rate) is not int or sample_rate <= 0:
            raise RuntimeError("网易云音乐未返回可确认的无损音轨。")
        if level == "hires" and bits < 24 and sample_rate <= 48000:
            raise RuntimeError("网易云音乐音轨的实际位深和采样率不符合 Hi-Res 音质。")
    return details


def _select_source(url, stop, on_status, *, expected_duration, session, source_quality):
    """Select one permitted source without opening its media body or creating files."""
    song = _SONG.fullmatch(url) if isinstance(url, str) else None
    if not song:
        raise ValueError("请输入网易云音乐单曲链接。")
    if not isinstance(source_quality, str) or source_quality not in {"legacy", "auto", *_LEVELS}:
        raise ValueError("请选择支持的网易云音乐源音质。")
    _check_stop(stop)
    if session is None:
        # NetEase retired the anonymous media route in 2026; every tier now
        # requires an account session, so ask for login before any request.
        raise RuntimeError("网易云音乐现在需要登录才能获取音轨，请先在网易云音乐页面扫码登录。")
    if source_quality == "legacy":
        on_status("正在检查网易云音乐账号可用音轨…")
        result = _legacy_account_source(session, song[1], stop)
    else:
        on_status("正在检查网易云音乐账号可用音轨…")
        result = _quality_source(session, song[1], stop, source_quality)
    _check_stop(stop)
    return result


def probe_netease(url, stop, on_status, *, session=None, expected_duration=None, source_quality="auto"):
    """Return only safe, declared source metadata; never download or decode media."""
    location, duration, size, _, media_type, level, bitrate = _select_source(
        url, stop, on_status, expected_duration=expected_duration, session=session, source_quality=source_quality)
    codec = {"mp3": "MP3", "flac": "FLAC", "m4a": "AAC"}[media_type]
    selected = next((key for key, value in _LEVELS.items() if value == level), None)
    if selected is None and media_type == "mp3":
        selected = next((key for key in ("128", "192", "320")
                         if _matching_bitrate(bitrate, int(key) * 1000)), None)
    parts = ["平台提供"]
    if level in _QUALITY_LABELS:
        parts.append(_QUALITY_LABELS[level])
    parts.append(codec)
    if bitrate is not None:
        parts.append(f"{bitrate / 1000:g} kb/s")
    parts.append("待下载校验")
    _check_stop(stop)
    return {"source": "netease", "requested": source_quality, "selected": selected,
            "label": " · ".join(parts), "codec": codec, "bitrate": bitrate, "size": size, "duration": duration}


def download_netease(url, stage, ffmpeg, stop, on_status, *, expected_duration=None, session=None,
                     source_quality="legacy"):
    """Stage one complete NetEase track using an optional native login session."""
    location, expected_duration, size, checksum, media_type, level, declared_bitrate = _select_source(
        url, stop, on_status, expected_duration=expected_duration, session=session, source_quality=source_quality)
    if source_quality == "legacy":
        # Preserve legacy acceptance and status: only decoded media supplies its displayed quality.
        level = declared_bitrate = None
    _check_stop(stop)
    target = Path(stage) / ("netease." + media_type)
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            on_status("正在下载网易云音乐…")
            _copy_media(location, stream, stop, on_status, expected_size=size, checksum=checksum, media_type=media_type)
        on_status("正在验证网易云音乐音轨…")
        _check_duration(target, ffmpeg, stop, expected_duration, media_type)
        details = _check_source_quality(target, media_type, source_quality, level, declared_bitrate)
        label = _QUALITY_LABELS.get(level)
        on_status("网易云音乐实际源音质 · " + ((label + " · ") if label else "") + audio.describe_audio(details))
        _check_stop(stop)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return target
