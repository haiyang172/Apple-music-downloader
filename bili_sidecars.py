"""Export public Bilibili covers, danmaku and authored subtitles as sidecar files."""

from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import struct
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
import zlib

from bili_catalog import VIDEO_BASE, _VideoMetadataParser, parse_video_link, resolve_video
from engine import Cancelled


_COMMENTS_ENDPOINT = "https://api.bilibili.com/x/v1/dm/list.so"
_SUBTITLE_PLAYER = "https://api.bilibili.com/x/player/wbi/v2"
_SUBTITLE_HOST = re.compile(r"(?:aisubtitle|i[0-9])\.hdslb\.com")
_SUBTITLE_PATH = re.compile(r"/bfs/subtitle/[A-Za-z0-9_./-]{1,800}\.json")
_SUBTITLE_LANG = re.compile(r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8}){0,3}")
_SUBTITLE_TRACK_LIMIT = 128
_SUBTITLE_ENTRY_LIMIT = 20_000
_SUBTITLE_TEXT_LIMIT = 2_048
_COVER_HOST = re.compile(r"i[0-2]\.hdslb\.com")
_COVER_PATH = re.compile(r"/bfs/archive/[0-9a-f]{40}\.(?:jpe?g|png)(?:@[0-9A-Za-z_.-]{1,80})?", re.I)
_COVER_MIME = {"image/jpeg", "image/jpg", "image/png"}
_COVER_BYTES = 4 * 1024 * 1024
_COVER_SIDE = 4_096
_JPEG_FRAME = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_WIRE_LIMIT = 2 * 1024 * 1024
_XML_LIMIT = 4 * 1024 * 1024
_ENTRY_LIMIT = 10_000
_TEXT_LIMIT = 240
_WIDTH = 1280
_HEIGHT = 720
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,Microsoft YaHei,25,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled()


def _decode(raw, encoding):
    if encoding in ("", "identity"):
        return raw
    if encoding == "deflate":
        # Bilibili normally sends raw deflate. Some servers add a zlib wrapper.
        types = (-zlib.MAX_WBITS, zlib.MAX_WBITS)
    elif encoding == "gzip":
        types = (zlib.MAX_WBITS + 16,)
    else:
        raise RuntimeError("B 站返回了无法读取的弹幕压缩格式。")

    for kind in types:
        try:
            stream = zlib.decompressobj(kind)
            data = stream.decompress(raw, _XML_LIMIT + 1)
        except zlib.error:
            continue
        if len(data) > _XML_LIMIT or stream.unconsumed_tail:
            raise RuntimeError("B 站返回的弹幕数据超出可读取大小。")
        if stream.eof and not stream.unused_data:
            return data
    raise RuntimeError("B 站返回了无法读取的弹幕压缩数据。")


class _CoverMetadataParser(_VideoMetadataParser):
    def __init__(self):
        super().__init__()
        self.open_graph = []

    def handle_starttag(self, tag, attrs):
        if tag == "meta" and len(self.open_graph) < 6:
            attributes = dict(attrs)
            if attributes.get("property") == "og:image":
                self.open_graph.append(attributes.get("content"))
        super().handle_starttag(tag, attrs)


def _cover_location(page):
    parser = _CoverMetadataParser()
    parser.feed(page.split("</head>", 1)[0])
    thumbnail = parser.video.get("thumbnailUrl")
    if not isinstance(thumbnail, list):
        thumbnail = [thumbnail]
    for value in [*thumbnail[:6], *parser.open_graph]:
        if not isinstance(value, str) or not 0 < len(value) <= 200 or any(
                not 33 <= ord(char) <= 126 or char == "\\" for char in value):
            continue
        try:
            target = urlsplit("https:" + value if value.startswith("//") else value)
        except ValueError:
            continue
        if (target.scheme == "https" and _COVER_HOST.fullmatch(target.netloc)
                and _COVER_PATH.fullmatch(target.path) and not target.query and not target.fragment):
            return target.geturl()
    return None


def _cover_resource(url, stop, *, referer=None):
    image = referer is not None
    size = _COVER_BYTES if image else _WIRE_LIMIT
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity" if image else "gzip, identity",
               "Accept": "image/jpeg, image/png" if image else "text/html"}
    if image:
        headers["Referer"] = referer
    deadline = time.monotonic() + 18
    body = bytearray()
    _check_stop(stop)
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=7) as response:
            mime = response.headers.get_content_type()
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if response.status != 200 or (image and (mime not in _COVER_MIME or encoding != "identity")):
                raise RuntimeError("B 站返回了无法读取的封面格式。")
            if not image and mime != "text/html":
                return None, ""
            while len(body) <= size:
                _check_stop(stop)
                if time.monotonic() > deadline:
                    raise RuntimeError("读取 B 站封面超时，请重试。")
                block = response.read1(min(65_536, size + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
    except HTTPError as error:
        error.close()
        _check_stop(stop)
        if not image and error.code in (401, 403, 404, 410, 412, 451):
            return None, ""
        raise RuntimeError("B 站暂未提供可读取的封面。") from None
    except (OSError, HTTPException):
        _check_stop(stop)
        raise RuntimeError("无法读取 B 站封面，请检查网络后重试。") from None
    if len(body) > size:
        raise RuntimeError("B 站封面数据超出可读取大小。")
    if not image:
        try:
            body = _decode(body, encoding)
        except RuntimeError:
            raise RuntimeError("B 站返回了无法读取的封面页面。") from None
    return bytes(body), mime


def _jpeg_dimensions(data):
    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        return None
    offset = 2
    dimensions = None
    while offset + 4 < len(data) and data[offset] == 0xFF:
        offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset + 2 >= len(data):
            return None
        marker = data[offset]
        offset += 1
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if marker in _JPEG_FRAME and length >= 8:
            dimensions = struct.unpack_from(">HH", data, offset + 3)
        if marker == 0xDA:
            return dimensions
        offset += length
    return None


def _cover_extension(data, mime):
    if mime == "image/png":
        if not (len(data) >= 45 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")):
            return ""
        dimensions, extension = struct.unpack_from(">II", data, 16), ".png"
    else:
        dimensions, extension = _jpeg_dimensions(data), ".jpg"
    return extension if dimensions and all(1 <= side <= _COVER_SIDE for side in dimensions) else ""


def save_cover(url, stage, stop, on_status):
    """Stage a cover explicitly published by the public video page if available."""
    _check_stop(stop)
    video, _ = parse_video_link(url)
    canonical = f"{VIDEO_BASE}{video}/"
    on_status("正在读取 B 站封面…")
    page, _ = _cover_resource(canonical, stop)
    _check_stop(stop)
    location = _cover_location(page.decode("utf-8", "replace")) if page else None
    if not location:
        return None
    image, mime = _cover_resource(location, stop, referer=canonical)
    extension = _cover_extension(image, mime)
    if not extension:
        raise RuntimeError("B 站返回的封面不是可读取的 JPEG 或 PNG 图片。")
    _check_stop(stop)
    output = Path(stage) / f"staged-cover{extension}"
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            stream.write(image)
        _check_stop(stop)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output


def _fetch_xml(cid, referer, stop):
    request = Request(f"{_COMMENTS_ENDPOINT}?{urlencode({'oid': cid})}", headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Accept": "application/xml, text/xml",
        "Accept-Encoding": "deflate, gzip",
    })
    deadline = time.monotonic() + 25
    data = bytearray()
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            encoding = response.headers.get("Content-Encoding", "").strip().lower()
            while len(data) <= _WIRE_LIMIT:
                _check_stop(stop)
                if time.monotonic() > deadline:
                    raise RuntimeError("读取 B 站弹幕超时，请重试。")
                chunk = response.read1(min(65536, _WIRE_LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
    except HTTPError as error:
        error.close()
        _check_stop(stop)
        raise RuntimeError("B 站暂未提供该分 P 的公开弹幕。") from None
    except (OSError, HTTPException):
        _check_stop(stop)
        raise RuntimeError("无法读取 B 站弹幕，请检查网络后重试。") from None
    if len(data) > _WIRE_LIMIT:
        raise RuntimeError("B 站返回的弹幕数据超出可读取大小。")
    return _decode(data, encoding)


def _comments(raw, duration, stop):
    if len(raw) > _XML_LIMIT:
        raise RuntimeError("B 站返回的弹幕数据超出可读取大小。")
    try:
        xml = raw.decode("utf-8-sig")
        if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
            raise ValueError()
        root = ElementTree.fromstring(xml)
        if root.tag != "i":
            raise ValueError()
    except (UnicodeError, ElementTree.ParseError, ValueError):
        raise RuntimeError("B 站返回了无法读取的弹幕列表。") from None

    comments = []
    limit = duration if 0 < duration <= 86400 else 86400
    for index, item in enumerate(root.iterfind("d")):
        _check_stop(stop)
        if index >= _ENTRY_LIMIT:
            raise RuntimeError("B 站返回的弹幕数量超出可读取上限。")
        fields = item.get("p", "").split(",", 4)
        if len(fields) < 4 or any(len(field) > 24 for field in fields[:4]):
            continue
        try:
            start = float(fields[0])
            mode, size, color = map(int, fields[1:4])
        except ValueError:
            continue
        if not math.isfinite(start) or not 0 <= start < limit or not 1 <= mode <= 6:
            continue
        safe_text = " ".join((item.text or "").split())[:_TEXT_LIMIT].translate(str.maketrans("\\{}", "＼｛｝"))
        if safe_text:
            comments.append((start, mode, min(32, max(18, size)), min(0xFFFFFF, max(0, color)), safe_text))
    return sorted(comments, key=lambda comment: comment[0])


def _ass_time(seconds):
    minutes, fraction = divmod(round(seconds * 100), 6000)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{fraction // 100:02d}.{fraction % 100:02d}"


def _write_ass(output, comments, stop):
    lanes = {"moving": [0.0] * 12, "top": [0.0] * 4, "bottom": [0.0] * 4}
    with output.open("x", encoding="utf-8-sig", newline="\n") as stream:
        stream.write(_ASS_HEADER)
        for start, mode, size, color, text in comments:
            _check_stop(stop)
            area = {4: "bottom", 5: "top"}.get(mode, "moving")
            ends = lanes[area]
            lane = min(range(len(ends)), key=ends.__getitem__)
            y = 10 + lane * 40
            if area == "moving":
                width = len(text) * size
                origin, destination = (_WIDTH, -width) if mode != 6 else (-width, _WIDTH)
                placement = f"\\an7\\move({origin},{y},{destination},{y})"
                end = start + 8
                ends[lane] = start + min(8, 8 * (width + 24) / (_WIDTH + width))
            else:
                y = _HEIGHT - y if area == "bottom" else y
                alignment = 2 if area == "bottom" else 8
                placement = f"\\an{alignment}\\pos({_WIDTH // 2},{y})"
                end = ends[lane] = start + 4
            red, green, blue = color >> 16, (color >> 8) & 255, color & 255
            style = f"{{{placement}\\fs{size}\\c&H{blue:02X}{green:02X}{red:02X}&}}"
            stream.write(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Danmaku,,0,0,0,,{style}{text}\n")


def save_danmaku(url, stage, stop, on_status):
    """Save an ASS attachment for one public video part, including an empty pool."""
    _check_stop(stop)
    video_id, page = parse_video_link(url)
    canonical = f"{VIDEO_BASE}{video_id}?p={page or 1}"
    on_status("正在读取 B 站弹幕…")
    part, = resolve_video(canonical, metadata=False)["rows"]
    raw = _fetch_xml(part["cid"], canonical, stop)
    comments = _comments(raw, part["duration"], stop)
    output = Path(stage) / "staged-danmaku.ass"
    if output.exists():
        raise RuntimeError("暂存目录内已存在弹幕文件。")
    try:
        _write_ass(output, comments, stop)
        _check_stop(stop)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output


def _fetch_subtitle_json(url, referer, stop, *, cookie=None):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if cookie:
        if not url.startswith(_SUBTITLE_PLAYER + "?"):
            raise ValueError("B 站会话只能用于站内字幕列表。")
        headers["Cookie"] = cookie
    request = Request(url, headers=headers)
    deadline = time.monotonic() + 25
    data = bytearray()
    _check_stop(stop)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise RuntimeError("B 站返回了无法读取的字幕编码。")
            while len(data) <= _WIRE_LIMIT:
                _check_stop(stop)
                if time.monotonic() > deadline:
                    raise RuntimeError("读取 B 站字幕超时，请重试。")
                chunk = response.read1(min(65_536, _WIRE_LIMIT + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
    except HTTPError as error:
        error.close()
        _check_stop(stop)
        raise RuntimeError("B 站未允许读取字幕，请检查视频权限或登录状态。") from None
    except (OSError, HTTPException):
        _check_stop(stop)
        raise RuntimeError("无法读取 B 站字幕，请检查网络后重试。") from None
    if len(data) > _WIRE_LIMIT:
        raise RuntimeError("B 站字幕数据超出可读取大小。")
    try:
        payload = json.loads(data)
    except (UnicodeError, ValueError, RecursionError):
        raise RuntimeError("B 站返回了无法读取的字幕数据。") from None
    if not isinstance(payload, dict):
        raise RuntimeError("B 站返回了无法读取的字幕数据。")
    return payload


def _subtitle_location(value):
    if not isinstance(value, str) or len(value) > 8192 or any(
            char.isspace() or ord(char) < 32 or char == "\\" for char in value):
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (url.scheme != "https" or not _SUBTITLE_HOST.fullmatch(url.netloc)
            or not _SUBTITLE_PATH.fullmatch(url.path) or ".." in url.path.split("/") or url.fragment):
        return None
    return url.geturl()


def _select_authored_subtitle(subtitles):
    if not isinstance(subtitles, list) or len(subtitles) > _SUBTITLE_TRACK_LIMIT:
        raise RuntimeError("B 站返回了无法读取的字幕列表。")
    priority = {"zh-hans": 0, "zh-cn": 1, "zh": 2, "zh-hant": 3, "zh-tw": 4, "en-us": 5, "en": 6}
    candidates = []
    for track in subtitles:
        if not isinstance(track, dict):
            continue
        language = track.get("lan")
        if not isinstance(language, str) or not _SUBTITLE_LANG.fullmatch(language):
            continue
        language = language.lower()
        if language.startswith("ai-") or track.get("type", 0) != 0 or track.get("ai_type", 0) != 0:
            continue
        location = _subtitle_location(track.get("subtitle_url"))
        if location:
            candidates.append((priority.get(language, len(priority)), location))
    return min(candidates, key=lambda track: track[0])[1] if candidates else None


def _srt_cues(entries, duration, stop):
    if not isinstance(entries, list) or len(entries) > _SUBTITLE_ENTRY_LIMIT:
        raise RuntimeError("B 站返回了无法读取的字幕正文。")
    limit = duration + 1 if 0 < duration < 86_400 else 86_400
    cues = []
    for entry in entries:
        _check_stop(stop)
        if not isinstance(entry, dict):
            continue
        start, end, content = (entry.get(key) for key in ("from", "to", "content"))
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not 0 <= start < end <= 86_400 or start >= limit or not isinstance(content, str)):
            continue
        clean = "".join(char for char in content[:_SUBTITLE_TEXT_LIMIT]
                        if char in "\r\n\t" or (ord(char) >= 32 and not 0x7F <= ord(char) <= 0x9F
                                                 and not 0xD800 <= ord(char) <= 0xDFFF))
        clean = clean.translate(str.maketrans("<>{}\\", "＜＞｛｝＼"))
        text = "\n".join(line.strip() for line in clean.splitlines() if line.strip())
        start_ms, end_ms = round(start * 1_000), round(min(end, limit) * 1_000)
        if text and end_ms > start_ms:
            cues.append((start_ms, end_ms, text))
    if not cues:
        raise RuntimeError("B 站字幕轨道没有有效正文。")
    return sorted(cues, key=lambda cue: cue[0])


def _srt_time(milliseconds):
    seconds, milliseconds = divmod(milliseconds, 1_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def save_subtitle(url, stage, stop, on_status, *, session=None):
    """Save one authored track as an SRT, or return None if no track is offered."""
    _check_stop(stop)
    video_id, page = parse_video_link(url)
    canonical = f"{VIDEO_BASE}{video_id}?p={page or 1}"
    on_status("正在读取 B 站字幕列表…")
    _check_stop(stop)
    part, = resolve_video(canonical, metadata=False, session=session)["rows"]
    query = {"bvid": video_id} if video_id.startswith("BV") else {"aid": video_id[2:]}
    query["cid"] = part["cid"]
    cookie = session.cookie_header() if session else None
    payload = _fetch_subtitle_json(f"{_SUBTITLE_PLAYER}?{urlencode(query)}", canonical, stop, cookie=cookie)
    metadata = payload.get("data")
    if payload.get("code") == -101:
        raise RuntimeError("请登录 B 站后再读取字幕。")
    if (type(payload.get("code")) is not int or payload["code"] != 0 or not isinstance(metadata, dict)
            or metadata.get("cid", part["cid"]) != part["cid"] or metadata.get("is_ugc_pay_preview")):
        raise RuntimeError("B 站未向当前会话提供该视频的字幕信息。")
    if metadata.get("need_login_subtitle") and not cookie:
        raise RuntimeError("请登录 B 站后再读取字幕。")
    subtitles = metadata.get("subtitle")
    if subtitles is not None and not isinstance(subtitles, dict):
        raise RuntimeError("B 站返回了无法读取的字幕列表。")
    location = _select_authored_subtitle((subtitles or {}).get("subtitles", []))
    if not location:
        if metadata.get("need_login_subtitle") and not (subtitles or {}).get("subtitles"):
            raise RuntimeError("当前 B 站会话没有字幕权限，请检查登录状态。")
        return None

    on_status("正在保存 B 站字幕…")
    payload = _fetch_subtitle_json(location, "https://www.bilibili.com/", stop)
    cues = _srt_cues(payload.get("body"), part["duration"], stop)
    output = Path(stage) / "staged-subtitle.srt"
    created = False
    try:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            for index, (start, end, text) in enumerate(cues, 1):
                _check_stop(stop)
                stream.write(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n\n")
        _check_stop(stop)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output
