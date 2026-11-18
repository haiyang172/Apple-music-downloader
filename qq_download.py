"""Save complete, unencrypted native tracks offered to a QQ Music session."""

from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from hashlib import md5
from hmac import compare_digest
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
_MAX_BYTES = 128 * 1024 ** 2
_MAX_FLAC_BYTES = 2 * 1024 ** 3
_CHUNK = 128 * 1024
_EXPIRED_CODES = {str(code) for code in QQ_AUTH_EXPIRED_CODES}
_UNAVAILABLE = "QQ 音乐未向当前账号提供所选音质的完整音轨。"
_PROTOCOL_ERROR = "QQ 音乐返回的音轨信息无效，请稍后重试。"
_NETWORK_ERROR = "无法读取 QQ 音乐音轨，请检查网络后重试。"


@dataclass(frozen=True)
class _SourceQuality:
    prefix: str
    suffix: str
    size_field: str
    label: str
    mime: frozenset[str]
    maximum_size: int
    bitrate: int | None = None


_QUALITIES = {
    "128": _SourceQuality("M500", ".mp3", "size_128mp3", "MP3 128 kbps",
                          frozenset({"audio/mpeg", "audio/mp3", "application/octet-stream"}), _MAX_BYTES, 128_000),
    "320": _SourceQuality("M800", ".mp3", "size_320mp3", "MP3 320 kbps",
                          frozenset({"audio/mpeg", "audio/mp3", "application/octet-stream"}), _MAX_BYTES, 320_000),
    "flac": _SourceQuality("F000", ".flac", "size_flac", "FLAC",
                           frozenset({"audio/flac", "audio/x-flac", "application/octet-stream"}), _MAX_FLAC_BYTES),
}


class _QualityUnavailable(RuntimeError):
    """The service explicitly declined a tier; only this allows auto fallback."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消下载。")


def _module_data(response):
    if not isinstance(response, dict):
        raise RuntimeError(_PROTOCOL_ERROR)
    code = response.get("code")
    if str(code) in _EXPIRED_CODES:
        raise RuntimeError("QQ 音乐登录已失效，请重新扫码。")
    data = response.get("data")
    if type(code) not in (int, str) or code not in (0, "0") or not isinstance(data, dict):
        raise RuntimeError(_PROTOCOL_ERROR)
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
        raise RuntimeError(_PROTOCOL_ERROR)
    if type(duration) is not int or not 0 < duration <= 86_400:
        raise RuntimeError("QQ 音乐未返回曲目时长，无法验证完整音轨。")
    return media_mid, duration, file


def _source_size(file, quality):
    size = file.get(quality.size_field)
    if size is None or type(size) is int and size == 0:
        raise _QualityUnavailable(_UNAVAILABLE)
    minimum = 4 if quality.suffix == ".flac" else 3
    if type(size) is not int or not minimum <= size <= quality.maximum_size:
        raise RuntimeError(_PROTOCOL_ERROR)
    return size


def _unavailable_message(item):
    if item.get("uiAlert") in (41, 42, "41", "42"):
        return "QQ 音乐未向当前账号开放这首 VIP 曲目的所选音质。"
    if item.get("pneedbuy") in (1, "1"):
        return "QQ 音乐要求先购买这首曲目。"
    return _UNAVAILABLE


def _media_location(session, mid, media_mid, source_quality="128"):
    quality = _QUALITIES[source_quality]
    # Use the same signed web-player API for each native filename, without conversion.
    filename = f"{quality.prefix}{media_mid}{media_mid}{quality.suffix}"
    params = {"filename": [filename], "guid": token_hex(16), "songmid": [mid], "songtype": [0],
              "uin": session.musicid, "loginflag": 1, "platform": "20"}
    data = _module_data(qq_music_request(
        session, "vkey.GetVkeyServer", "CgiGetVkey", params, common={"ct": 24, "cv": 0},
    ))
    items = data.get("midurlinfo")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError(_PROTOCOL_ERROR)
    item = items[0]
    if item.get("songmid") != mid or item.get("filename") != filename:
        raise RuntimeError(_PROTOCOL_ERROR)
    result = item.get("result")
    if str(result) in _EXPIRED_CODES:
        raise RuntimeError("QQ 音乐登录已失效，请重新扫码。")
    if (type(result) not in (int, str)
            or not re.fullmatch(r"-?[0-9]{1,9}", str(result))):
        raise RuntimeError(_PROTOCOL_ERROR)
    if item.get("ekey") not in (None, ""):
        raise RuntimeError("QQ 音乐返回了受保护的音轨，无法保存为普通音频。")
    if (item.get("freeTrialInfo") is not None
            or any(item.get(key) not in (None, False, 0, "0", "") for key in ("isTry", "is_try", "isTrial"))):
        raise RuntimeError("QQ 音乐返回了试听音轨，无法保存为完整歌曲。")
    if int(result) != 0:
        raise _QualityUnavailable(_unavailable_message(item))

    purl = item.get("purl")
    if purl == "":
        raise _QualityUnavailable(_unavailable_message(item))
    if not isinstance(purl, str) or not 0 < len(purl) <= 8192 or any(
            not 33 <= ord(char) <= 126 or char == "\\" for char in purl):
        raise RuntimeError(_PROTOCOL_ERROR)
    try:
        parts = urlsplit(purl)
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=32)
    except ValueError:
        raise RuntimeError(_PROTOCOL_ERROR) from None
    if parts.scheme or parts.netloc or parts.fragment or parts.path != filename:
        raise RuntimeError(_PROTOCOL_ERROR)
    vkey = query.get("vkey", [])
    if len(vkey) != 1 or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,2048}={0,2}", vkey[0]):
        raise RuntimeError(_PROTOCOL_ERROR)
    if item.get("vkey") and item["vkey"] != vkey[0]:
        raise RuntimeError(_PROTOCOL_ERROR)
    return _CDN + purl


def _content_md5(headers):
    checksums = headers.get_all("Content-MD5", [])
    if not checksums:
        return None
    try:
        digest = b64decode(checksums[0], validate=True) if len(checksums) == 1 else b""
    except (Base64Error, ValueError):
        raise RuntimeError("QQ 音乐返回的文件校验值无效。") from None
    if len(digest) != 16:
        raise RuntimeError("QQ 音乐返回的文件校验值无效。")
    return digest


def _copy_media(location, stream, expected_size, stop, on_status, quality):
    accept = "audio/flac" if quality.suffix == ".flac" else "audio/mpeg"
    request = Request(location, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/",
                                         "Accept": accept + ",*/*;q=0.1", "Accept-Encoding": "identity"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            headers = response.headers
            if (response.status != 200 or len(headers.get_all("Content-Type", [])) != 1
                    or headers.get_content_type() not in quality.mime
                    or headers.get_all("Content-Encoding", []) not in ([], ["identity"])
                    or headers.get("Content-Range") is not None or headers.get("Transfer-Encoding") is not None):
                raise RuntimeError("QQ 音乐返回的文件类型或传输信息无效。")
            sizes = headers.get_all("Content-Length", [])
            if len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,10}", sizes[0]) or int(sizes[0]) != expected_size:
                raise RuntimeError("QQ 音乐返回的文件大小与完整音轨不符。")
            expected_md5 = _content_md5(headers)
            checksum = md5(usedforsecurity=False) if expected_md5 is not None else None
            received = 0
            started = last_update = time.monotonic()
            prefix = b""
            while received < expected_size:
                _check_stop(stop)
                now = time.monotonic()
                if now - started > 180:
                    raise RuntimeError("QQ 音乐音轨下载超时。")
                block = response.read1(min(_CHUNK, expected_size - received))
                if not block or received + len(block) > expected_size:
                    raise RuntimeError("QQ 音乐音轨下载不完整。")
                header_size = 4 if quality.suffix == ".flac" else 3
                if len(prefix) < header_size:
                    prefix = (prefix + block)[:header_size]
                    valid = prefix == b"fLaC" if quality.suffix == ".flac" else (
                        prefix == b"ID3" or len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0)
                    if len(prefix) == header_size and not valid:
                        raise RuntimeError("QQ 音乐没有返回与所选音质对应的普通音轨。")
                stream.write(block)
                if checksum is not None:
                    checksum.update(block)
                received += len(block)
                if now - last_update >= 0.5:
                    on_status(f"正在下载 QQ 音乐 · {quality.label} · {received * 100 // expected_size}%")
                    last_update = now
            if checksum is not None and not compare_digest(checksum.digest(), expected_md5):
                raise RuntimeError("QQ 音乐音轨校验失败，请重新下载。")
    except HTTPError as error:
        error.close()
        message = _UNAVAILABLE if error.code in (401, 403, 404, 410, 451) else _NETWORK_ERROR
        raise RuntimeError(message) from None
    except (URLError, TimeoutError, HTTPException):
        raise RuntimeError(_NETWORK_ERROR) from None


def _check_duration(path, ffmpeg, stop, expected, source_quality="128"):
    quality = _QUALITIES[source_quality]
    details = audio.audio_details(path)
    codec = "FLAC" if quality.suffix == ".flac" else "MP3"
    if not isinstance(details, dict) or details.get("codec") != codec:
        raise RuntimeError("QQ 音乐返回的实际音轨格式与所选音质不符。")
    if quality.bitrate is not None:
        bitrate = details.get("bitrate")
        if type(bitrate) not in (int, float) or not abs(bitrate - quality.bitrate) <= quality.bitrate * 0.03:
            raise RuntimeError("QQ 音乐返回的实际 MP3 码率与所选音质不符。")
    try:
        progress = audio._run(ffmpeg, [
            "-protocol_whitelist", "file,pipe", "-f", quality.suffix[1:], "-i", str(path), "-map", "0:a:0",
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
    return details


def _resolve_source(url, stop, on_status, *, session, expected_duration, source_quality):
    if not isinstance(source_quality, str) or source_quality not in ("auto", *_QUALITIES):
        raise ValueError("请选择自动、MP3 128、MP3 320 或 FLAC 源音质。")
    provider, kind, mid, _ = _parse_link(url)
    if provider != "qq" or kind != "song":
        raise ValueError("请输入 QQ 音乐单曲链接。")
    if not isinstance(session, QqSession):
        raise RuntimeError("请先登录 QQ 音乐。")
    _check_stop(stop)
    requested = "自动（最高可用）" if source_quality == "auto" else _QUALITIES[source_quality].label
    on_status(f"正在读取 QQ 音乐曲目信息 · 请求 {requested}")
    media_mid, duration, file = _track_info(session, mid)
    if (type(expected_duration) in (int, float) and 0 < expected_duration <= 86_400
            and abs(duration - expected_duration) > 1.25):
        raise RuntimeError("QQ 音乐曲目信息已更新，请刷新歌单后重试。")
    _check_stop(stop)
    candidates = ("flac", "320", "128") if source_quality == "auto" else (source_quality,)
    for index, selected in enumerate(candidates):
        _check_stop(stop)
        quality = _QUALITIES[selected]
        try:
            size = _source_size(file, quality)
            location = _media_location(session, mid, media_mid, selected)
            break
        except _QualityUnavailable:
            if index == len(candidates) - 1:
                raise
            on_status(f"QQ 音乐 {quality.label} 暂不可用，正在尝试下一档源音质…")
    _check_stop(stop)
    return requested, selected, quality, size, duration, location


def probe_qq(url, stop, on_status, *, session, expected_duration=None, source_quality="auto"):
    """Read offered source metadata without transferring media or retaining its locator."""
    _, selected, quality, size, duration, _ = _resolve_source(
        url, stop, on_status, session=session, expected_duration=expected_duration, source_quality=source_quality)
    return {"source": "qq", "requested": source_quality, "selected": selected, "label": quality.label,
            "codec": "FLAC" if quality.suffix == ".flac" else "MP3", "bitrate": quality.bitrate,
            "size": size, "duration": duration}


def download_qq(url, stage, ffmpeg, stop, on_status, *, session, expected_duration=None, source_quality="128"):
    """Stage a full native track; only auto may try a lower available source tier."""
    requested, selected, quality, size, duration, location = _resolve_source(
        url, stop, on_status, session=session, expected_duration=expected_duration, source_quality=source_quality)
    target = Path(stage) / ("qq" + quality.suffix)
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            on_status(f"正在下载 QQ 音乐 · 请求 {requested} · 已选择 {quality.label}")
            _copy_media(location, stream, size, stop, on_status, quality)
        on_status("正在验证 QQ 音乐音轨…")
        details = _check_duration(target, ffmpeg, stop, duration, selected)
        _check_stop(stop)
        on_status(f"QQ 音乐源音轨 · 请求 {requested} · 实际 {audio.describe_audio(details)}")
        return target
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
