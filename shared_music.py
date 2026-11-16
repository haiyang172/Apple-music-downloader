"""Read song and playlist metadata published on QQ Music and NetEase pages."""

import json
import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_QQ_HOSTS = {"y.qq.com", "i.y.qq.com"}
_NETEASE_HOSTS = {"music.163.com", "y.music.163.com"}
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,19}")
_QQ_SONG_ID = re.compile(r"[A-Za-z0-9]{14}")
_QQ_PATH = re.compile(r"/n/(?:ryqq(?:_v2)?|yqq)/(songDetail|song|playlist)/([A-Za-z0-9]+)(?:\.html)?/?")
_NETEASE_PATH = re.compile(r"/(?:m/)?(song|playlist)(?:/([1-9][0-9]{0,19})(?:/[A-Za-z0-9_-]+)?)?/?")
_DURATION = re.compile(r"PT(?:(\d{1,3})H)?(?:(\d{1,3})M)?(?:(\d{1,3})(?:\.\d+)?S)?")
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_INVALID = "请输入 QQ 音乐或网易云音乐的公开单曲或歌单链接。"


def _query_id(query, key):
    values = parse_qs(query, keep_blank_values=True).get(key, [])
    return values[0] if len(values) == 1 else ""


def _parse_link(value):
    if not isinstance(value, str):
        raise ValueError(_INVALID)
    value = value.strip()
    if len(value) > 4096 or any(char.isspace() or ord(char) < 32 or char == "\\" for char in value):
        raise ValueError(_INVALID)
    try:
        url = urlsplit(value)
    except ValueError:
        raise ValueError(_INVALID) from None
    host = url.netloc.lower()
    if url.scheme != "https":
        raise ValueError(_INVALID)

    if host in _NETEASE_HOSTS:
        source = "netease"
        if url.path in ("", "/") and url.fragment.startswith("/"):
            if url.fragment.startswith("//"):
                raise ValueError(_INVALID)
            url = urlsplit(url.fragment)
        match = _NETEASE_PATH.fullmatch(url.path)
        if not match:
            raise ValueError(_INVALID)
        kind, identifier = match.groups()
        identifier = identifier or _query_id(url.query, "id")
        if _NUMERIC_ID.fullmatch(identifier):
            return source, kind, identifier, f"https://music.163.com/{kind}?id={identifier}"
    elif host in _QQ_HOSTS:
        source = "qq"
        match = _QQ_PATH.fullmatch(url.path)
        if match:
            route, identifier = match.groups()
            kind = "playlist" if route == "playlist" else "song"
        elif url.path == "/n2/m/share/details/taoge.html" or url.path == "/musicmac/v6/playlist/detail.html":
            kind, identifier = "playlist", _query_id(url.query, "id")
        elif url.path == "/n2/m/share/details/song.html":
            kind, identifier = "song", _query_id(url.query, "songmid")
        else:
            raise ValueError(_INVALID)
        if kind == "playlist" and _NUMERIC_ID.fullmatch(identifier):
            return source, kind, identifier, f"https://y.qq.com/n/ryqq_v2/playlist/{identifier}"
        if kind == "song" and _QQ_SONG_ID.fullmatch(identifier):
            return source, kind, identifier, f"https://y.qq.com/n/ryqq_v2/songDetail/{identifier}"
    raise ValueError(_INVALID)


def is_shared_music_link(value):
    """Recognize a supported official page without making a network request."""
    return shared_music_source(value) is not None


def shared_music_source(value):
    """Return the provider for a supported song or playlist URL."""
    try:
        source, _, _, _ = _parse_link(value)
    except ValueError:
        return None
    return source


class _PageRedirect(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def __init__(self, identity):
        self.identity = identity

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            if _parse_link(newurl)[:3] != self.identity:
                return None
        except ValueError:
            return None
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _read_page(url, identity):
    request = Request(url, headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"})
    try:
        with build_opener(_PageRedirect(identity)).open(request, timeout=8) as response:
            body = response.read(_MAX_PAGE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise RuntimeError("平台没有返回公开页面，请在浏览器确认链接。") from None
    except (URLError, OSError):
        raise RuntimeError("无法读取音乐分享页面，请检查网络后重试。") from None
    if len(body) > _MAX_PAGE_BYTES:
        raise RuntimeError("音乐分享页面超出可读取大小。")
    return body.decode("utf-8-sig", errors="replace")


def _line(value):
    return " ".join(value.split())[:500] if isinstance(value, str) else ""


class _PublishedPage(HTMLParser):
    def __init__(self, read_track_list):
        super().__init__()
        self.read_track_list = read_track_list
        self.schemas = []
        self.track_links = []
        self.title = ""
        self.script = None
        self.list_depth = 0
        self.track_url = None
        self.track_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("property") == "og:title":
            self.title = _line(attributes.get("content"))
        elif tag == "script" and attributes.get("type") == "application/ld+json":
            self.script = []
        elif self.read_track_list and tag == "ul":
            if self.list_depth or "f-hide" in (attributes.get("class") or "").split():
                self.list_depth += 1
        elif self.list_depth and tag == "a":
            self.track_url = attributes.get("href")
            self.track_text = []

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            try:
                self.schemas.append(json.loads("".join(self.script)))
            except ValueError:
                pass
            self.script = None
        elif tag == "ul" and self.list_depth:
            self.list_depth -= 1
        elif tag == "a" and self.track_url is not None:
            self.track_links.append((self.track_url, _line("".join(self.track_text))))
            self.track_url = None

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif self.track_url is not None:
            self.track_text.append(data)


def _schema_records(schemas):
    for value in schemas:
        if isinstance(value, list):
            yield from _schema_records(value)
        elif isinstance(value, dict):
            yield value
            graph = value.get("@graph")
            if isinstance(graph, list):
                yield from _schema_records(graph)


def _matches_type(record, expected):
    types = record.get("@type")
    return expected in types if isinstance(types, list) else types == expected


def _names(value):
    records = value if isinstance(value, list) else [value]
    names = (_line(item.get("name") if isinstance(item, dict) else item) for item in records)
    return " / ".join(dict.fromkeys(name for name in names if name))


def _duration_seconds(value):
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _track(record, url, position):
    return {
        "title": _line(record.get("name")),
        "artist": _names(record.get("byArtist")),
        "album": _names(record.get("inAlbum")),
        "duration": _duration_seconds(record.get("duration")),
        "url": url,
        "position": position,
    }


def _song_url(url, source, base):
    if not isinstance(url, str) or not url:
        return ""
    try:
        parsed_source, kind, _, canonical = _parse_link(urljoin(base, url))
    except ValueError:
        return ""
    return canonical if parsed_source == source and kind == "song" else ""


def _playlist_rows(record, page, source, base):
    entries = record.get("track")
    if isinstance(entries, dict):
        entries = entries.get("itemListElement")
    rows = []
    for index, entry in enumerate(entries if isinstance(entries, list) else [], 1):
        if not isinstance(entry, dict):
            continue
        position = entry.get("position", index)
        metadata = entry.get("item", entry)
        if not isinstance(metadata, dict):
            continue
        url = _song_url(metadata.get("url"), source, base)
        if not url:
            continue
        row = _track(metadata, url, position if type(position) is int and position > 0 else index)
        if row["title"]:
            rows.append(row)

    if source != "netease" or not page.track_links:
        return rows
    by_url = {row["url"]: row for row in rows}
    listed = []
    for href, title in page.track_links:
        url = _song_url(href, source, base)
        if url and title:
            metadata = by_url.get(url) or _track({"name": title}, url, 0)
            listed.append(dict(metadata, position=len(listed) + 1))
    return listed or rows


def resolve_music(value):
    """Read published rows in source order, with the advertised total if present."""
    source, kind, identifier, url = _parse_link(value)
    page = _PublishedPage(source == "netease" and kind == "playlist")
    page.feed(_read_page(url, (source, kind, identifier)))
    page.close()
    expected = "MusicPlaylist" if kind == "playlist" else "MusicRecording"
    record = next((item for item in _schema_records(page.schemas) if _matches_type(item, expected)), {})
    title = _line(record.get("name")) or page.title
    if not record or not title:
        raise RuntimeError("公开页面没有返回可用的歌曲或歌单信息。")
    if kind == "song":
        row = _track(record, url, 1)
        row["title"] = title
        return {"source": source, "title": title, "url": url, "total": 1, "rows": [row]}
    rows = _playlist_rows(record, page, source, url)
    count = record.get("numTracks")
    total = max(count, len(rows)) if type(count) is int and count >= 0 else None
    return {"source": source, "title": title, "url": url, "total": total, "rows": rows}
