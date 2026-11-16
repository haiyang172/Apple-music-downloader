"""Stage timed lyrics and album artwork published by NetEase Music."""

from http.client import HTTPException
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bili_sidecars import _cover_extension
from engine import Cancelled


_SITE = "https://music.163.com"
_SONG = re.compile(r"https://music\.163\.com/song\?id=([1-9][0-9]{0,19})")
_COVER_HOST = re.compile(r"p[1-9][0-9]?\.music\.126\.net")
_COVER_PATH = re.compile(r"/[A-Za-z0-9_=-]{8,120}/[A-Za-z0-9_-]{1,120}\.(?:jpg|jpeg|png)", re.IGNORECASE)
_LYRIC_CUE = re.compile(r"(?:\[[0-9]{1,3}:[0-5][0-9](?:[.:][0-9]{1,3})?\])+\s*\S")
_JPEG_TYPES = {"image/jpeg", "image/jpg"}
_PNG_TYPE = "image/png"
_MAX_LYRIC = 256 * 1024
_MAX_JSON = 2 * 1024 * 1024
_MAX_IMAGE = 4 * 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消下载。")


def _song_id(url):
    match = _SONG.fullmatch(url) if isinstance(url, str) else None
    if match is None:
        raise ValueError("请输入网易云音乐的标准单曲链接。")
    return int(match[1])


def _fetch(url, stop, label, *, image=False):
    known_types = _JPEG_TYPES | {_PNG_TYPE} if image else {"application/json"}
    limit = _MAX_IMAGE if image else _MAX_JSON
    headers = {"User-Agent": "Mozilla/5.0", "Referer": _SITE + "/", "Accept-Encoding": "identity",
               "Accept": "image/jpeg, image/png" if image else "application/json"}
    deadline = time.monotonic() + 18
    result = bytearray()
    _check_stop(stop)
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=7) as response:
            mime = response.headers.get_content_type()
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if response.status != 200 or mime not in known_types or encoding != "identity":
                raise RuntimeError(f"网易云音乐返回了无法读取的{label}格式。")
            size = response.headers.get("Content-Length")
            if size is not None:
                if not re.fullmatch(r"[0-9]{1,12}", size.strip()) or int(size) > limit:
                    raise RuntimeError(f"网易云音乐{label}超出可读取大小。")
                size = int(size)
            while len(result) <= limit:
                _check_stop(stop)
                if time.monotonic() > deadline:
                    raise RuntimeError(f"读取网易云音乐{label}超时，请重试。")
                block = response.read1(min(65_536, limit + 1 - len(result)))
                if not block:
                    break
                result.extend(block)
            if len(result) > limit:
                raise RuntimeError(f"网易云音乐{label}超出可读取大小。")
            if size is not None and len(result) != size:
                raise RuntimeError(f"网易云音乐返回的{label}不完整，请重试。")
    except HTTPError as error:
        error.close()
        _check_stop(stop)
        if error.code in (404, 410):
            return None, ""
        raise RuntimeError(f"网易云音乐暂未允许读取{label}，请稍后重试。") from None
    except (OSError, HTTPException):
        _check_stop(stop)
        raise RuntimeError(f"无法连接网易云音乐读取{label}，请检查网络。") from None
    _check_stop(stop)
    return bytes(result), mime


def _metadata(route, params, stop, label):
    raw, _ = _fetch(f"{_SITE}{route}?{urlencode(params)}", stop, label)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise RuntimeError(f"网易云音乐返回了无法读取的{label}资料。") from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise RuntimeError(f"网易云音乐返回了无效的{label}资料。")
    if payload["code"] in (404, 410):
        return None
    if payload["code"] != 200:
        raise RuntimeError(f"网易云音乐未能读取{label}，请稍后重试。")
    return payload


def _stage(stage, basename, content, stop):
    _check_stop(stop)
    output = Path(stage) / basename
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            stream.write(content)
        _check_stop(stop)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output


def save_lyrics(url, stage, stop, on_status, *, session=None):
    """Save the site's original timed lyrics as UTF-8 LRC when available."""
    _check_stop(stop)
    identifier = _song_id(url)
    on_status("正在读取网易云音乐歌词…")
    payload = _metadata("/api/song/lyric", {"id": identifier, "lv": 1, "kv": 1, "tv": 1}, stop, "歌词")
    if payload is None or payload.get("nolyric") is True or payload.get("uncollected") is True:
        return None
    lrc = payload.get("lrc")
    if lrc is None:
        return None
    raw = lrc.get("lyric") if isinstance(lrc, dict) else None
    if not isinstance(raw, str) or len(raw) > _MAX_LYRIC:
        raise RuntimeError("网易云音乐返回了无效的歌词正文。")
    lines = raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lyrics = ["".join(char for char in line if char == "\t" or (
        32 <= ord(char) and not 127 <= ord(char) <= 159 and not 0xD800 <= ord(char) <= 0xDFFF
    )).rstrip() for line in lines]
    if not any(_LYRIC_CUE.match(line.lstrip()) for line in lyrics):
        return None
    encoded = ("\n".join(lyrics).rstrip() + "\n").encode("utf-8")
    if len(encoded) > _MAX_LYRIC:
        raise RuntimeError("网易云音乐歌词超出可读取大小。")
    return _stage(stage, "staged-lyrics.lrc", encoded, stop)


def _cover_url(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or any(
            not 33 <= ord(char) <= 126 or char == "\\" for char in value):
        return None
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if (url.scheme not in ("https", "http") or not _COVER_HOST.fullmatch(url.netloc.lower())
            or not _COVER_PATH.fullmatch(url.path) or url.fragment):
        return None
    return url._replace(scheme="https", query="param=600y600").geturl()


def save_cover(url, stage, stop, on_status, *, session=None):
    """Save album artwork from NetEase's image CDN without sending credentials."""
    _check_stop(stop)
    identifier = _song_id(url)
    on_status("正在读取网易云音乐封面…")
    payload = _metadata("/api/v3/song/detail", {"c": json.dumps([{"id": identifier}], separators=(",", ":"))},
                        stop, "封面")
    if payload is None:
        return None
    songs = payload.get("songs")
    if songs == []:
        return None
    if (not isinstance(songs, list) or len(songs) != 1 or not isinstance(songs[0], dict)
            or type(songs[0].get("id")) is not int or songs[0]["id"] != identifier):
        raise RuntimeError("网易云音乐返回的封面资料与所选歌曲不一致。")
    album = songs[0].get("al")
    location = _cover_url(album.get("picUrl")) if isinstance(album, dict) else None
    if not location:
        return None
    image, mime = _fetch(location, stop, "封面", image=True)
    if image is None:
        return None
    # Some NetEase CDN URLs label their original PNG data as image/jpg.
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = _PNG_TYPE
    extension = _cover_extension(image, mime)
    if not extension:
        raise RuntimeError("网易云音乐返回的封面不是可读取的 JPEG 或 PNG 图片。")
    return _stage(stage, "staged-cover" + extension, image, stop)
