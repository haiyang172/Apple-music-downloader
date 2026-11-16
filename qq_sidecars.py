"""Stage public QQ Music timed lyrics and album artwork on request."""

from html import unescape
from http.client import HTTPException
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from bili_sidecars import _cover_extension
from engine import Cancelled
from qq_auth import _NoRedirect, _musicu
from shared_music import _parse_link


_REFERER = "https://y.qq.com/"
_LYRICS = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
_COVER = "https://y.gtimg.cn/music/photo_new/T002R800x800M000"
_ALBUM_IMAGE_ID = re.compile(r"[A-Za-z0-9]{14}(?:_[0-9]{1,6})?")
_TIMESTAMP = re.compile(r"^[ \t]*\[[0-9]{1,3}:[0-5][0-9](?:[.:][0-9]{1,3})?\]", re.M)
_JSON_CALLBACK = re.compile(r"MusicJsonCallback\s*\((.*)\)\s*;?\s*", re.S)
_LYRICS_MIME = frozenset({"application/json", "text/html", "text/plain", "application/javascript", "text/javascript"})
_COVER_MIME = frozenset({"image/jpeg", "image/jpg", "image/png"})
_LYRICS_LIMIT = 256 * 1024
_COVER_LIMIT = 4 * 1024 * 1024


def _check_stop(stop):
    if stop.is_set():
        raise Cancelled("已取消 QQ 音乐附件保存。")


def _song_id(url):
    source, kind, identifier, _ = _parse_link(url)
    if source != "qq" or kind != "song":
        raise ValueError("请输入 QQ 音乐单曲链接。")
    return identifier


def _public_resource(url, stop, *, media_types, limit, label):
    _check_stop(stop)
    request = Request(url, headers={"Referer": _REFERER, "User-Agent": "Mozilla/5.0",
                                    "Accept": ", ".join(sorted(media_types)), "Accept-Encoding": "identity"})
    deadline = time.monotonic() + 16
    body = bytearray()
    try:
        with build_opener(_NoRedirect()).open(request, timeout=7) as response:
            headers = response.headers
            mime = headers.get_content_type()
            if (response.status != 200 or mime not in media_types
                    or headers.get("Content-Encoding", "identity").strip().casefold() != "identity"):
                raise RuntimeError(f"QQ 音乐返回了无法读取的{label}格式。")
            while len(body) <= limit:
                _check_stop(stop)
                if time.monotonic() > deadline:
                    raise RuntimeError(f"读取 QQ 音乐{label}超时，请重试。")
                block = response.read1(min(65_536, limit + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
    except HTTPError as error:
        error.close()
        _check_stop(stop)
        if error.code in (401, 403, 404, 410, 451):
            return None, ""
        raise RuntimeError(f"暂时无法读取 QQ 音乐{label}。") from None
    except (OSError, HTTPException):
        _check_stop(stop)
        raise RuntimeError(f"无法读取 QQ 音乐{label}，请检查网络后重试。") from None
    if len(body) > limit:
        raise RuntimeError(f"QQ 音乐{label}数据超出可读取大小。")
    return bytes(body), mime


def _save(stage, filename, value, stop):
    _check_stop(stop)
    target = Path(stage).resolve(strict=True) / filename
    created = False
    try:
        with target.open("xb") as stream:
            created = True
            stream.write(value)
        _check_stop(stop)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return target


def _timed_lyrics(body):
    try:
        text = body.decode("utf-8-sig").strip()
        wrapped = _JSON_CALLBACK.fullmatch(text)
        payload = json.loads(wrapped[1] if wrapped else text)
    except (ValueError, RecursionError):
        raise RuntimeError("QQ 音乐歌词数据无法读取。") from None
    if not isinstance(payload, dict):
        raise RuntimeError("QQ 音乐歌词数据无法读取。")
    if payload.get("code", 0) not in (0, "0") or payload.get("retcode", 0) not in (0, "0"):
        return ""
    lyric = payload.get("lyric")
    if not isinstance(lyric, str):
        return ""
    lyric = "\n".join(unescape(lyric).lstrip("\ufeff").splitlines()).strip()
    if any((ord(char) < 32 and char not in "\n\t") or char == "\x7f" or "\ud800" <= char <= "\udfff" for char in lyric):
        raise RuntimeError("QQ 音乐歌词数据无法读取。")
    return lyric if _TIMESTAMP.search(lyric) else ""


def save_lyrics(url, stage, stop, on_status, *, session=None):
    """Stage the public, line-timed LRC returned for one QQ Music track."""
    _check_stop(stop)
    identifier = _song_id(url)
    on_status("正在读取 QQ 音乐歌词…")
    params = {"songmid": identifier, "format": "json", "nobase64": 1}
    body, _ = _public_resource(f"{_LYRICS}?{urlencode(params)}", stop, media_types=_LYRICS_MIME,
                              limit=_LYRICS_LIMIT, label="歌词")
    _check_stop(stop)
    lyric = _timed_lyrics(body) if body else ""
    return _save(stage, "qq-lyrics.lrc", (lyric + "\n").encode("utf-8"), stop) if lyric else None


def save_cover(url, stage, stop, on_status, *, session=None):
    """Stage the artwork published for one QQ Music track's album."""
    _check_stop(stop)
    identifier = _song_id(url)
    on_status("正在读取 QQ 音乐封面…")
    response = _musicu("music.pf_song_detail_svr", "get_song_detail_yqq", {"song_mid": identifier},
                       {"ct": 19, "cv": 0, "format": "json", "uid": "0"})
    _check_stop(stop)
    data = response.get("data")
    if response.get("code") not in (0, "0") or not isinstance(data, dict):
        return None
    track = data.get("track_info")
    if not isinstance(track, dict) or track.get("mid") != identifier:
        raise RuntimeError("QQ 音乐未返回所选曲目的封面信息。")
    album = track.get("album")
    if not isinstance(album, dict):
        return None
    image_ids = dict.fromkeys(mid for mid in (album.get("pmid"), album.get("mid"))
                              if isinstance(mid, str) and _ALBUM_IMAGE_ID.fullmatch(mid))
    for mid in image_ids:
        image, mime = _public_resource(f"{_COVER}{mid}.jpg", stop, media_types=_COVER_MIME,
                                       limit=_COVER_LIMIT, label="封面")
        if image is None:
            continue
        extension = _cover_extension(image, mime)
        if not extension:
            raise RuntimeError("QQ 音乐返回的封面不是可读取的 JPEG 或 PNG 图片。")
        return _save(stage, f"qq-cover{extension}", image, stop)
    return None
