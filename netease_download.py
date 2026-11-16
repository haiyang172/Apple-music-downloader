"""Save complete MP3 tracks offered by NetEase Music."""

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
from shared_music import resolve_music


_SONG = re.compile(r"https://music\.163\.com/song\?id=([1-9][0-9]{0,19})")
_CDN = re.compile(r"m[0-9]{1,6}\.music\.126\.net")
_MEDIA_ENDPOINT = "https://music.163.com/song/media/outer/url"
_UNAVAILABLE = "网易云音乐未提供这首歌的公开完整音轨。"
_ACCOUNT_UNAVAILABLE = "网易云音乐未向当前账号提供这首歌的完整 MP3。"
_NETWORK_ERROR = "读取网易云音乐音轨失败，请检查网络或稍后重试。"
_MP3_MIME = {"audio/mpeg", "audio/mp3"}
_MAX_BYTES = 128 * 1024 ** 2
_CHUNK = 128 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消下载。")


def _open(url, accept):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": accept, "Accept-Encoding": "identity"})
    return build_opener(_NoRedirect()).open(request, timeout=10)


def _media_location(identifier):
    try:
        with _open(f"{_MEDIA_ENDPOINT}?id={identifier}.mp3", "audio/mpeg"):
            pass
    except HTTPError as error:
        with error:
            if error.code in (301, 302, 303, 307, 308):
                values = error.headers.get_all("Location", [])
                if len(values) == 1:
                    return _https_cdn(values[0])
            if error.code in (401, 403, 404, 410, 451):
                raise RuntimeError(_UNAVAILABLE) from None
        raise RuntimeError(_NETWORK_ERROR) from None
    except (URLError, OSError, HTTPException):
        raise RuntimeError(_NETWORK_ERROR) from None
    raise RuntimeError(_UNAVAILABLE)


def _https_cdn(value):
    if not isinstance(value, str) or not 0 < len(value) <= 8192 or any(
            not 33 <= ord(char) <= 126 or char == "\\" for char in value):
        raise RuntimeError(_UNAVAILABLE)
    try:
        target = urlsplit(value)
    except ValueError:
        raise RuntimeError(_UNAVAILABLE) from None
    if (target.scheme not in ("https", "http") or not _CDN.fullmatch(target.netloc)
            or not target.path.startswith("/") or not target.path.lower().endswith(".mp3") or target.fragment):
        raise RuntimeError(_UNAVAILABLE)
    # Legacy redirects advertise HTTP. The same signed media locator also supports TLS.
    return target._replace(scheme="https").geturl()


def _account_source(session, identifier, stop):
    from netease_auth import netease_api_request

    song_id = int(identifier)
    reply = netease_api_request(session, "/api/song/enhance/player/url", {
        "id": song_id, "ids": f"[{song_id}]", "br": 320000,
    })
    if reply.get("code") in (301, 401):
        raise RuntimeError("网易云音乐登录已失效，请重新扫码。")
    items = reply.get("data")
    if reply.get("code") != 200 or not isinstance(items, list) or len(items) != 1:
        raise RuntimeError(_ACCOUNT_UNAVAILABLE)
    item = items[0]
    if not isinstance(item, dict) or item.get("id") != song_id:
        raise RuntimeError(_ACCOUNT_UNAVAILABLE)
    if item.get("freeTrialInfo") is not None:
        raise RuntimeError("网易云音乐仅向当前账号提供试听片段。")
    if item.get("code") != 200 or item.get("type") != "mp3" or item.get("encodeType") not in (None, "mp3"):
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
    if details.get("code") != 200 or not isinstance(songs, list) or len(songs) != 1:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    song = songs[0]
    duration_ms = song.get("dt") if isinstance(song, dict) and song.get("id") == song_id else None
    if type(duration_ms) is not int or not 0 < duration_ms <= 86_400_000:
        raise RuntimeError("网易云音乐未返回完整曲目的时长，无法验证音轨。")
    return location, duration_ms / 1000, size, checksum.lower()


def _copy_media(location, stream, stop, on_status, *, expected_size=None, checksum=None):
    failure = _ACCOUNT_UNAVAILABLE if expected_size is not None else _UNAVAILABLE
    try:
        with _open(location, "audio/mpeg") as response:
            headers = response.headers
            if (response.status != 200 or headers.get_content_type() not in _MP3_MIME
                    or headers.get("Content-Encoding") not in (None, "identity")
                    or headers.get("Content-Range") or headers.get("Transfer-Encoding")):
                raise RuntimeError(failure)
            sizes = headers.get_all("Content-Length", [])
            if len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,10}", sizes[0]) or not 3 <= int(sizes[0]) <= _MAX_BYTES:
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
                if now - started > 180:
                    raise RuntimeError("网易云音乐音轨下载超时。")
                chunk = response.read1(min(_CHUNK, total - received))
                if not chunk:
                    raise RuntimeError("网易云音乐音轨下载不完整。")
                if len(prefix) < 3:
                    prefix = (prefix + chunk)[:3]
                    if len(prefix) == 3 and not (prefix == b"ID3" or prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0):
                        raise RuntimeError("网易云音乐没有返回可读取的 MP3 音轨。")
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
        message = failure if error.code in (401, 403, 404, 410, 451) else _NETWORK_ERROR
        raise RuntimeError(message) from None
    except (URLError, TimeoutError, HTTPException):
        raise RuntimeError(_NETWORK_ERROR) from None


def _duration_value(value):
    return type(value) in (int, float) and 0 < value <= 86_400


def _check_duration(path, ffmpeg, stop, expected):
    try:
        progress = audio._run(ffmpeg, [
            "-protocol_whitelist", "file,pipe", "-f", "mp3", "-i", str(path), "-map", "0:a:0",
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


def download_netease(url, stage, ffmpeg, stop, on_status, *, expected_duration=None, session=None):
    """Stage one complete NetEase track using an optional native login session."""
    song = _SONG.fullmatch(url) if isinstance(url, str) else None
    if not song:
        raise ValueError("请输入网易云音乐单曲链接。")
    _check_stop(stop)
    size = checksum = None
    if session is None:
        if not _duration_value(expected_duration):
            on_status("正在读取网易云音乐曲目信息…")
            rows = resolve_music(url)["rows"]
            expected_duration = rows[0].get("duration") if rows else None
        if not _duration_value(expected_duration):
            raise RuntimeError("未获得完整曲目的时长，无法验证公开音轨。")
        _check_stop(stop)
        on_status("正在检查网易云音乐公开音轨…")
        location = _media_location(song[1])
    else:
        on_status("正在检查网易云音乐账号可用音轨…")
        location, expected_duration, size, checksum = _account_source(session, song[1], stop)
    _check_stop(stop)
    target = Path(stage) / "netease.mp3"
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            on_status("正在下载网易云音乐…")
            _copy_media(location, stream, stop, on_status, expected_size=size, checksum=checksum)
        on_status("正在验证网易云音乐音轨…")
        _check_duration(target, ffmpeg, stop, expected_duration)
        _check_stop(stop)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return target
