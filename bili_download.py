"""Save the full, unencrypted DASH tracks offered by Bilibili's player."""

from http.client import HTTPException
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bili_catalog import VIDEO_BASE, parse_video_link, resolve_video
from bili_resume import choose_resume_url, resume_stream
from engine import Cancelled


_PLAYURL = "https://api.bilibili.com/x/player/playurl"
_ACCESS_ERROR = "B 站未向当前会话提供完整音视频，请确认账号和视频的播放权限。"
_REFUSED = "B 站拒绝了本次播放请求，请稍后重试。"
_RESPONSE_LIMIT = 2 * 1024 * 1024
_VIDEO_LIMIT = 4 * 1024 ** 3
_AUDIO_LIMIT = 512 * 1024 ** 2
_MEDIA_TYPES = {"audio/mp4", "video/mp4", "application/mp4", "application/octet-stream"}
_OUTPUTS = {"video": ".mp4", "audio": ".m4a", "mp3": ".mp3", "flac": ".flac"}
_CHUNK = 256 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消下载。")


def _open(url, referer, **headers):
    return build_opener(_NoRedirect()).open(Request(url, headers={
        "Referer": referer,
        "User-Agent": "Mozilla/5.0",
        **headers,
    }), timeout=10)


def _network_error(error):
    if isinstance(error, HTTPError):
        error.close()
        if error.code in (401, 403, 404, 410):
            return RuntimeError(_ACCESS_ERROR)
        if error.code in (412, 429):
            return RuntimeError(_REFUSED)
    return RuntimeError("B 站下载请求失败，请检查网络后重试。")


def _playback(video_id, part, referer, stop, session=None):
    query = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    query.update(cid=part["cid"], qn=80 if session else 64, fnval=16, fourk=0)
    headers = {"Accept": "application/json"}
    if session is not None:
        headers["Cookie"] = session.cookie_header()
    _check_stop(stop)
    try:
        with _open(f"{_PLAYURL}?{urlencode(query)}", referer, **headers) as response:
            raw = response.read(_RESPONSE_LIMIT + 1)
    except (OSError, HTTPException) as error:
        raise _network_error(error) from None
    _check_stop(stop)
    if len(raw) > _RESPONSE_LIMIT:
        raise RuntimeError("B 站返回的播放信息过大。")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError("无法读取 B 站播放信息。") from None
    if not isinstance(result, dict) or type(result.get("code")) is not int:
        raise RuntimeError("无法读取 B 站播放信息。")
    if result["code"] != 0:
        raise RuntimeError(_ACCESS_ERROR)
    data = result.get("data")
    if (not isinstance(data, dict) or _restricted(data)
            or not isinstance(data.get("dash"), dict)):
        raise RuntimeError(_ACCESS_ERROR)
    expected, actual = part.get("duration"), data.get("timelength")
    if type(expected) is int and type(actual) is int and expected > 0 and actual < (expected - 3) * 1000:
        raise RuntimeError(_ACCESS_ERROR)
    return data["dash"]


def _restricted(data):
    return any(data.get(key) not in (None, False, 0, "", "0")
               for key in ("is_preview", "isPreview", "is_drm", "isDrm", "drm_tech_type"))


def _trusted_cdn(value):
    if not isinstance(value, str) or len(value) > 8192 or any(char.isspace() or char == "\\" for char in value):
        return False
    try:
        url = urlsplit(value)
    except ValueError:
        return False
    host = url.hostname or ""
    return (url.scheme == "https" and url.netloc == host and bool(url.path) and not url.fragment
            and (host.endswith((".bilivideo.com", ".bilivideo.cn"))
                 or host == "upos-hz-mirrorakam.akamaized.net"))


def _select_stream_entry(dash, kind):
    entries = dash.get(kind)
    if not isinstance(entries, list):
        raise RuntimeError(_ACCESS_ERROR)
    prefix = "avc1" if kind == "video" else "mp4a"
    supported = []
    for entry in entries:
        if not isinstance(entry, dict) or _restricted(entry):
            continue
        mime = entry.get("mimeType") or entry.get("mime_type")
        codec, bitrate = entry.get("codecs"), entry.get("bandwidth")
        if mime != f"{kind}/mp4" or not isinstance(codec, str) or not codec.startswith(prefix + "."):
            continue
        if urls := _stream_urls(entry):
            supported.append((bitrate if type(bitrate) is int else 0, entry, urls))
    if not supported:
        label = "H.264 视频" if kind == "video" else "AAC 音频"
        raise RuntimeError(f"B 站没有提供可下载的 {label}。")
    selected = max(supported, key=lambda entry: entry[0])
    return selected[1], selected[2]


def _select_stream(dash, kind):
    return _select_stream_entry(dash, kind)[1]


def _stream_urls(entry):
    primary = entry.get("baseUrl") or entry.get("base_url")
    backups = entry.get("backupUrl") or entry.get("backup_url")
    candidates = [primary, *backups] if isinstance(backups, list) else [primary]
    return tuple(dict.fromkeys(url for url in candidates if _trusted_cdn(url)))


def _select_flac_entry(dash):
    flac = dash.get("flac")
    entry = flac.get("audio") if isinstance(flac, dict) else None
    if isinstance(entry, dict) and not _restricted(flac) and not _restricted(entry):
        codec = entry.get("codecs")
        mime = entry.get("mimeType") or entry.get("mime_type")
        if mime == "audio/mp4" and isinstance(codec, str) and codec.casefold() == "flac":
            if urls := _stream_urls(entry):
                return entry, urls
    raise RuntimeError("B 站没有向当前会话提供原始 FLAC 音轨。可检查网页播放权限，或改选 M4A。")


def _select_flac(dash):
    return _select_flac_entry(dash)[1]


def _resolve_selection(url, mode, stop, on_status, session):
    if mode not in _OUTPUTS:
        raise ValueError("不支持所选 B 站输出格式。")
    video_id, page = parse_video_link(url)
    referer = f"{VIDEO_BASE}{video_id}?p={page or 1}"
    _check_stop(stop)
    on_status("正在读取 B 站播放信息…")
    part = resolve_video(referer, metadata=False, session=session)["rows"][0]
    dash = _playback(video_id, part, referer, stop, session)
    audio = _select_flac_entry(dash) if mode == "flac" else _select_stream_entry(dash, "audio")
    video = _select_stream_entry(dash, "video") if mode == "video" else None
    return video_id, part, referer, dash, audio, video


def _positive_number(value):
    return value if type(value) in (int, float) and 0 < value < float("inf") else None


def _declared_size(entry, limit):
    # DASH size is optional; an invalid declaration is never a reason to try a
    # lower quality or a different origin. HEAD may supply the missing length.
    size = entry.get("size")
    return size if type(size) is int and 8 <= size <= limit else None


def _head_size(url, referer, stop, limit):
    _check_stop(stop)
    video_id, page = parse_video_link(referer)
    if not _trusted_cdn(url) or referer != f"{VIDEO_BASE}{video_id}?p={page or 1}":
        raise RuntimeError("B 站返回了无法验证的媒体地址。")
    request = Request(url, method="HEAD", headers={
        "Referer": referer, "User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity",
    })
    try:
        # A fresh opener carries no account cookies and refuses redirects. The
        # media response body is deliberately never read, even if HEAD is not
        # implemented correctly by a server.
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            size = _content_size(response, limit)
    except HTTPError as error:
        if error.code in (405, 501):
            error.close()
            _check_stop(stop)
            return None
        raise _network_error(error) from None
    except (OSError, HTTPException) as error:
        raise _network_error(error) from None
    _check_stop(stop)
    return size


def probe_video(url, mode, stop, on_status, *, session=None):
    """Return selected source-track specifications without downloading media.

    Sizes are source transfer bytes; MP4 is the sum of both selected tracks,
    while MP3 still reports its AAC input. Codecs and bitrate are declarations
    from the playback API, not measurements of decoded or converted media.
    No signed URL, account information or resumable state leaves this call.
    """
    _, part, referer, dash, audio, video = _resolve_selection(url, mode, stop, on_status, session)
    _check_stop(stop)
    on_status("正在读取 B 站媒体规格…")
    sizes = []
    warning = None
    for selected, limit in ((audio, _AUDIO_LIMIT), (video, _VIDEO_LIMIT)):
        if selected is not None:
            entry, urls = selected
            size = _declared_size(entry, limit)
            if size is None:
                try:
                    size = _head_size(urls[0], referer, stop, limit)
                except Cancelled:
                    raise
                except RuntimeError:
                    # Size is optional metadata. A failed HEAD cannot authorize
                    # a GET, another tier, or a different media URL; download
                    # keeps its own full response and permission validation.
                    size = None
                if size is None:
                    warning = "平台未返回可读取的文件大小，下载时仍会校验。"
            sizes.append(size)
            _check_stop(stop)
    codec = "FLAC" if mode == "flac" else "AAC"
    labels = {"video": "H.264 + AAC", "audio": "AAC 原始音轨",
              "mp3": "AAC 源音轨（转 MP3）", "flac": "原始 FLAC 音轨"}
    video_entry = video[0] if video is not None else {}
    duration = _positive_number(part.get("duration")) or _positive_number(dash.get("duration"))
    bitrate = audio[0].get("bandwidth")
    _check_stop(stop)
    result = {
        "source": "bilibili", "mode": mode,
        "label": labels[mode] + ("（待下载校验）" if warning else ""), "codec": codec,
        "bitrate": bitrate if type(bitrate) is int and bitrate > 0 else None,
        "size": sum(sizes) if all(size is not None for size in sizes) else None,
        "duration": duration, "video_codec": "H.264" if video is not None else None,
        "width": video_entry.get("width") if type(video_entry.get("width")) is int and video_entry["width"] > 0 else None,
        "height": video_entry.get("height") if type(video_entry.get("height")) is int and video_entry["height"] > 0 else None,
    }
    if warning:
        result["warning"] = warning
    return result


def _content_size(response, limit):
    if response.headers.get_content_type() not in _MEDIA_TYPES or response.headers.get("Content-Encoding") not in (None, "identity"):
        raise RuntimeError("B 站媒体服务器没有返回预期的音视频。")
    content_length = response.headers.get("Content-Length")
    try:
        length = int(content_length) if content_length is not None else None
    except ValueError:
        raise RuntimeError("B 站返回了无效的媒体长度。") from None
    if response.status == 206:
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes 0-(\d{1,12})/(\d{1,12})", content_range)
        if not match or int(match[1]) + 1 != int(match[2]) or length not in (None, int(match[2])):
            raise RuntimeError("B 站只返回了部分媒体，请重试。")
        length = int(match[2])
    elif response.status != 200:
        raise RuntimeError("B 站媒体服务器没有返回完整音视频。")
    if length is not None and not 8 <= length <= limit:
        raise RuntimeError("B 站媒体长度无效或超过下载上限。")
    return length


def _save_stream(url, referer, target, stop, label, limit, on_status):
    _check_stop(stop)
    if not _trusted_cdn(url):
        raise RuntimeError("B 站返回了无法验证的媒体地址。")
    started = time.monotonic()
    on_status(f"正在下载{label}…")
    try:
        with _open(url, referer, Range="bytes=0-", **{"Accept-Encoding": "identity"}) as response:
            length = _content_size(response, limit)
            with target.open("xb") as file:
                received, last_update = 0, started
                while chunk := response.read(_CHUNK):
                    _check_stop(stop)
                    now = time.monotonic()
                    if now - started > 1800:
                        raise RuntimeError("B 站媒体下载超时。")
                    if received == 0 and chunk[4:8] != b"ftyp":
                        raise RuntimeError("B 站返回的媒体格式无法识别。")
                    received += len(chunk)
                    if received > limit or (length is not None and received > length):
                        raise RuntimeError("B 站媒体超过预期大小。")
                    file.write(chunk)
                    if now - last_update >= 0.5:
                        progress = f"{received * 100 // length}%" if length else f"{received / 1024 ** 2:.1f} MB"
                        on_status(f"正在下载{label} · {progress}")
                        last_update = now
                if received < 8 or (length is not None and received != length):
                    raise RuntimeError("B 站媒体下载未完成，请重试。")
    except (OSError, HTTPException) as error:
        raise _network_error(error) from None
    _check_stop(stop)


def _mux(ffmpeg, inputs, mode, output, stop):
    args = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-n"]
    for path in inputs:
        args.extend(["-protocol_whitelist", "file", "-f", "mov", "-i", str(path)])
    if mode == "video":
        args.extend(["-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart"])
    elif mode == "mp3":
        args.extend(["-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2"])
    elif mode == "flac":
        args.extend(["-map", "0:a:0", "-c:a", "copy", "-f", "flac"])
    else:
        args.extend(["-map", "0:a:0", "-c:a", "copy", "-movflags", "+faststart"])
    args.extend(["-map_metadata", "-1", "-map_chapters", "-1", str(output)])
    _check_stop(stop)
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise RuntimeError("无法启动 FFmpeg，请检查安装组件。") from None
    with process:
        deadline = time.monotonic() + 600
        try:
            while process.poll() is None:
                if stop.wait(0.15):
                    _check_stop(stop)
                if time.monotonic() >= deadline:
                    raise RuntimeError("FFmpeg 处理超时。")
        finally:
            if process.poll() is None:
                process.kill()
    _check_stop(stop)
    if process.returncode or not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg 无法生成 B 站媒体文件。")


def download_video(url, mode, stage, ffmpeg, stop, on_status, *, session=None, checkpoint=None, check_volume=None):
    """Download one available video part and return the prepared media path."""
    video_id, part, referer, _, audio, video = _resolve_selection(url, mode, stop, on_status, session)
    audio_urls = audio[1]
    video_urls = video[1] if video is not None else ()

    stage = Path(stage)
    output = stage / ("staged-media" + _OUTPUTS[mode])
    if output.exists():
        raise RuntimeError("暂存目录内已存在输出文件。")
    scope = sha256(("account:" + session.cookie_header() if session else "anonymous").encode("utf-8")).hexdigest()
    media = f"{video_id}:{part['cid']}:{mode}"

    def track(slot, urls, destination, label, limit):
        if checkpoint is None:
            _save_stream(urls[0], referer, destination, stop, label, limit, on_status)
            return destination
        address = choose_resume_url(checkpoint, slot, scope, media, urls, check_volume=check_volume)
        return resume_stream(checkpoint, slot, scope, media, address, lambda headers: _open(address, referer, **headers),
                             stop, label, limit, on_status, check_volume=check_volume)

    try:
        with TemporaryDirectory(prefix="bili-", dir=stage) as folder:
            audio = track("audio", audio_urls, Path(folder) / "audio.m4s", "音轨", _AUDIO_LIMIT)
            inputs = [audio]
            if video_urls:
                video = track("video", video_urls, Path(folder) / "video.m4s", "视频", _VIDEO_LIMIT)
                inputs.insert(0, video)
            on_status("正在生成 MP3…" if mode == "mp3" else "正在封装 B 站媒体…")
            _mux(ffmpeg, inputs, mode, output, stop)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output
