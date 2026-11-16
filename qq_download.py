"""Save complete, unencrypted MP3 tracks offered to a QQ Music session."""

from http.client import HTTPException
from pathlib import Path
import re
from secrets import token_hex
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import audio
from engine import Cancelled
from qq_auth import QQ_AUTH_EXPIRED_CODES, QqSession, qq_music_request
from shared_music import _parse_link


_CDN = "https://isure.stream.qqmusic.qq.com/"
_MID = re.compile(r"[A-Za-z0-9]{14}")
_MP3_MIME = {"audio/mpeg", "audio/mp3", "application/octet-stream"}
_MAX_BYTES = 128 * 1024 ** 2
_CHUNK = 128 * 1024
_EXPIRED_CODES = {str(code) for code in QQ_AUTH_EXPIRED_CODES}
_UNAVAILABLE = "QQ 音乐未向当前账号提供这首歌的完整 MP3。"
_NETWORK_ERROR = "无法读取 QQ 音乐音轨，请检查网络后重试。"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消下载。")


def _module_data(response):
    code = response.get("code")
    if str(code) in _EXPIRED_CODES:
        raise RuntimeError("QQ 音乐登录已失效，请重新扫码。")
    data = response.get("data")
    if code not in (0, "0") or not isinstance(data, dict):
        raise RuntimeError(_UNAVAILABLE)
    return data


def _track_info(session, mid):
    data = _module_data(qq_music_request(
        session, "music.pf_song_detail_svr", "get_song_detail_yqq", {"song_mid": mid},
        common={"ct": 19, "cv": 0},
    ))
    track = data.get("track_info")
    if not isinstance(track, dict) or track.get("mid") != mid:
        raise RuntimeError("QQ 音乐没有返回对应的曲目信息。")
    file = track.get("file")
    media_mid = file.get("media_mid") if isinstance(file, dict) else None
    duration = track.get("interval")
    if not isinstance(media_mid, str) or not _MID.fullmatch(media_mid):
        raise RuntimeError(_UNAVAILABLE)
    if type(duration) is not int or not 0 < duration <= 86_400:
        raise RuntimeError("QQ 音乐未返回曲目时长，无法验证完整音轨。")
    size = file.get("size_128mp3")
    if type(size) is not int or not 3 <= size <= _MAX_BYTES:
        raise RuntimeError(_UNAVAILABLE)
    return media_mid, duration, size


def _media_location(session, mid, media_mid):
    # The normal web player request uses the media MID twice in the plain MP3 filename.
    filename = f"M500{media_mid}{media_mid}.mp3"
    params = {"filename": [filename], "guid": token_hex(16), "songmid": [mid], "songtype": [0],
              "uin": session.musicid, "loginflag": 1, "platform": "20"}
    data = _module_data(qq_music_request(
        session, "vkey.GetVkeyServer", "CgiGetVkey", params, common={"ct": 24, "cv": 0},
    ))
    items = data.get("midurlinfo")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError(_UNAVAILABLE)
    item = items[0]
    if item.get("songmid") != mid or item.get("filename") != filename:
        raise RuntimeError(_UNAVAILABLE)
    if item.get("result") not in (0, "0") or item.get("ekey"):
        if item.get("uiAlert") in (41, 42, "41", "42"):
            raise RuntimeError("QQ 音乐未向当前账号开放这首 VIP 曲目。")
        if item.get("pneedbuy") in (1, "1"):
            raise RuntimeError("QQ 音乐要求先购买这首曲目。")
        raise RuntimeError(_UNAVAILABLE)

    purl = item.get("purl")
    if not isinstance(purl, str) or not 0 < len(purl) <= 8192 or any(
            not 33 <= ord(char) <= 126 or char == "\\" for char in purl):
        raise RuntimeError(_UNAVAILABLE)
    try:
        parts = urlsplit(purl)
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32)
    except ValueError:
        raise RuntimeError(_UNAVAILABLE) from None
    if parts.scheme or parts.netloc or parts.fragment or parts.path != filename:
        raise RuntimeError(_UNAVAILABLE)
    vkey = query.get("vkey", [])
    if len(vkey) != 1 or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,2048}={0,2}", vkey[0]):
        raise RuntimeError(_UNAVAILABLE)
    if item.get("vkey") and item["vkey"] != vkey[0]:
        raise RuntimeError(_UNAVAILABLE)
    return _CDN + purl


def _copy_mp3(location, stream, expected_size, stop, on_status):
    request = Request(location, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/",
                                         "Accept": "audio/mpeg,*/*;q=0.1", "Accept-Encoding": "identity"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            headers = response.headers
            if (response.status != 200 or headers.get_content_type() not in _MP3_MIME
                    or headers.get("Content-Encoding") not in (None, "identity")
                    or headers.get("Content-Range") or headers.get("Transfer-Encoding")):
                raise RuntimeError(_UNAVAILABLE)
            sizes = headers.get_all("Content-Length", [])
            if len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,10}", sizes[0]) or int(sizes[0]) != expected_size:
                raise RuntimeError("QQ 音乐返回的文件大小与完整音轨不符。")
            received = 0
            started = last_update = time.monotonic()
            prefix = b""
            while received < expected_size:
                _check_stop(stop)
                now = time.monotonic()
                if now - started > 180:
                    raise RuntimeError("QQ 音乐音轨下载超时。")
                block = response.read1(min(_CHUNK, expected_size - received))
                if not block:
                    raise RuntimeError("QQ 音乐音轨下载不完整。")
                if len(prefix) < 3:
                    prefix = (prefix + block)[:3]
                    if len(prefix) == 3 and not (prefix == b"ID3" or prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0):
                        raise RuntimeError("QQ 音乐没有返回可读取的 MP3 音轨。")
                stream.write(block)
                received += len(block)
                if now - last_update >= 0.5:
                    on_status(f"正在下载 QQ 音乐 · {received * 100 // expected_size}%")
                    last_update = now
    except HTTPError as error:
        error.close()
        message = _UNAVAILABLE if error.code in (401, 403, 404, 410, 451) else _NETWORK_ERROR
        raise RuntimeError(message) from None
    except (URLError, TimeoutError, HTTPException):
        raise RuntimeError(_NETWORK_ERROR) from None


def _check_duration(path, ffmpeg, stop, expected):
    try:
        progress = audio._run(ffmpeg, [
            "-protocol_whitelist", "file,pipe", "-f", "mp3", "-i", str(path), "-map", "0:a:0",
            "-progress", "pipe:1", "-f", "null", "-",
        ], stop, "QQ 音乐音轨无法完整解码。")
    except audio.Cancelled:
        raise Cancelled("已取消下载。") from None
    elapsed = 0
    for line in progress.splitlines():
        key, _, value = line.partition(b"=")
        if key == b"out_time_us" and value.isdigit():
            elapsed = int(value)
    if elapsed <= 0 or abs(elapsed / 1_000_000 - expected) > 1.25:
        raise RuntimeError("QQ 音乐返回的音轨时长不符，可能为试听或文件不完整。")


def download_qq(url, stage, ffmpeg, stop, on_status, *, session, expected_duration=None):
    """Stage one authorized full MP3 and return its path after local decoding."""
    provider, kind, mid, _ = _parse_link(url)
    if provider != "qq" or kind != "song":
        raise ValueError("请输入 QQ 音乐单曲链接。")
    if not isinstance(session, QqSession):
        raise RuntimeError("请先登录 QQ 音乐。")
    _check_stop(stop)
    on_status("正在读取 QQ 音乐曲目信息…")
    media_mid, duration, size = _track_info(session, mid)
    if (type(expected_duration) in (int, float) and 0 < expected_duration <= 86_400
            and abs(duration - expected_duration) > 1.25):
        raise RuntimeError("QQ 音乐曲目信息已更新，请刷新歌单后重试。")
    _check_stop(stop)
    location = _media_location(session, mid, media_mid)
    _check_stop(stop)
    target = Path(stage) / "qq.mp3"
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            on_status("正在下载 QQ 音乐…")
            _copy_mp3(location, stream, size, stop, on_status)
        on_status("正在验证 QQ 音乐音轨…")
        _check_duration(target, ffmpeg, stop, duration)
        _check_stop(stop)
        return target
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
