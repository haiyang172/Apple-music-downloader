"""Search NetEase Cloud Music and read public or account playlists."""

import json
import re

import netease_auth
import shared_music


_BASE = "https://music.163.com"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_MAX_SEARCH_OFFSET = 10_000
_PRIVATE = 10
_LIKED = 5
_AUTH_CODES = {301, 401}
_ACCESS_DENIED = {403, 404}


class NeteaseCatalogError(RuntimeError):
    """NetEase rejected a catalog read or returned unusable data."""

    def __init__(self, message, *, code=None, denied=False):
        super().__init__(message)
        self.code = code
        self.session_expired = code in _AUTH_CODES
        self.authorization_denied = denied or self.session_expired or code in _ACCESS_DENIED


def _number(value):
    if type(value) is int and 0 <= value < 10**19:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        return int(value)
    return None


def _record(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return " ".join(value.split())[:240] if isinstance(value, str) else ""


def _cancelled(stop):
    if stop is not None and stop.is_set():
        raise NeteaseCatalogError("已取消读取网易云音乐。")


def _checked(response, label):
    code = _number(response.get("code"))
    if code in _AUTH_CODES:
        raise NeteaseCatalogError("网易云音乐登录已失效，请重新扫码。", code=code)
    if code in _ACCESS_DENIED:
        raise NeteaseCatalogError(f"当前账号无法访问网易云音乐{label}。", code=code)
    if code != 200:
        raise NeteaseCatalogError(f"暂时无法读取网易云音乐{label}。", code=code)
    return response


def _account(session):
    if not isinstance(session, netease_auth.NeteaseSession) or not _number(session.user_id):
        raise NeteaseCatalogError("请先登录网易云音乐。", denied=True)
    return int(session.user_id)


def _playlist(raw, user_id):
    data = _record(raw)
    identifier = _number(data.get("id"))
    title = _text(data.get("name"))
    if not (identifier and title):
        return None
    creator = _number(data.get("userId")) or _number(_record(data.get("creator")).get("userId"))
    self_owned = creator == user_id or (creator is None and data.get("subscribed") is not True)
    kind = ("liked" if _number(data.get("specialType")) == _LIKED else "created") if self_owned else "collected"
    private = _number(data.get("privacy")) == _PRIVATE
    return {"id": str(identifier), "title": title, "total": _number(data.get("trackCount")), "kind": kind,
            "url": "" if private else f"{_BASE}/playlist?id={identifier}", "private": private, "_account_id": user_id}


def list_playlists(session, *, stop=None):
    """List the current account's own and subscribed playlists."""
    _cancelled(stop)
    user_id = _account(session)
    account = _checked(netease_auth.netease_api_request(session, "/api/nuser/account/get"), "个人资料")
    profile = account.get("profile")
    returned_id = _number(_record(profile).get("userId"))
    if ("profile" in account and profile is None) or (returned_id and returned_id != user_id):
        raise NeteaseCatalogError("网易云音乐登录已失效，请重新扫码。", code=301)
    if returned_id != user_id:
        raise NeteaseCatalogError("网易云音乐未返回有效的个人资料。")

    warning = ""
    playlists = {}
    incomplete = False
    next_expected = False
    for offset in range(0, _MAX_PAGES * _PAGE_SIZE, _PAGE_SIZE):
        _cancelled(stop)
        try:
            response = netease_auth.netease_api_request(session, "/api/user/playlist",
                                                       {"uid": user_id, "limit": _PAGE_SIZE, "offset": offset})
            data = _checked(response, "歌单")
            page = data.get("playlist")
            if not isinstance(page, list) or len(page) > _PAGE_SIZE:
                raise NeteaseCatalogError("网易云音乐未返回歌单列表。")
        except (netease_auth.NeteaseAuthError, NeteaseCatalogError) as error:
            _cancelled(stop)
            if not playlists or getattr(error, "authorization_denied", False):
                raise
            warning = f"只读取到部分网易云音乐歌单。{error}"
            break
        previous = len(playlists)
        for item in page:
            if entry := _playlist(item, user_id):
                playlists.setdefault(entry["id"], entry)
        more = data.get("more")
        if not page and (next_expected or more is True or _number(more) not in (None, 0)):
            warning = "网易云音乐未返回歌单的下一页。"
            break
        added = len(playlists) - previous
        if page and not added:
            warning = "网易云音乐重复返回歌单或缺少歌单信息，已保留读取到的部分。"
            break
        incomplete = incomplete or added < len(page)
        if not page or more is False or _number(more) == 0 or (more is None and len(page) < _PAGE_SIZE):
            break
        next_expected = more is True or _number(more) not in (None, 0)
    else:
        warning = "歌单数量超出单次读取范围。"
    _cancelled(stop)
    if incomplete and not warning:
        warning = "网易云音乐未返回部分歌单。"
    rows = sorted(playlists.values(), key=lambda item: item["kind"] != "liked")
    return {"source": "netease", "rows": rows, "total": len(rows), "warnings": [warning] if warning else []}


def _track(song, position):
    identifier = _number(song.get("id"))
    title = _text(song.get("name"))
    if not (identifier and title):
        return None
    artists = song.get("ar")
    names = (_text(_record(artist).get("name")) for artist in artists) if isinstance(artists, list) else ()
    album = _record(song.get("al"))
    return {"source": "netease", "id": str(identifier), "title": title, "position": position,
            "artist": " / ".join(dict.fromkeys(filter(None, names))), "album": _text(album.get("name")),
            "duration": (_number(song.get("dt")) or 0) / 1000, "url": f"{_BASE}/song?id={identifier}"}


def search_songs(query, *, session=None, stop=None, offset=0, limit=50):
    """Read one page of public songs in the provider's relevance order."""
    query = " ".join(query.split()) if isinstance(query, str) else ""
    if not query or len(query) > 240 or any(ord(char) < 32 or ord(char) == 127 for char in query):
        raise ValueError("请输入 1 至 240 个字符的歌曲搜索词。")
    if type(offset) is not int or not 0 <= offset < _MAX_SEARCH_OFFSET:
        raise ValueError("无效的网易云音乐搜索位置。")
    if type(limit) is not int or not 1 <= limit <= _PAGE_SIZE:
        raise ValueError("无效的网易云音乐搜索条数。")

    _cancelled(stop)
    response = netease_auth.netease_search_request(query, offset, limit, session=session)
    _cancelled(stop)
    result = _record(_checked(response, "搜索结果").get("result"))
    page = result.get("songs")
    total = _number(result.get("songCount"))
    if page is None and total == 0:
        page = []
    if not isinstance(page, list) or len(page) > limit:
        raise NeteaseCatalogError("网易云音乐未返回有效的歌曲搜索结果。")

    # The service can return more records beyond its published count or recycle earlier pages.
    if total is not None:
        page = page[:max(0, total - offset)]
    rows = [row for position, song in enumerate(page, offset + 1)
            if isinstance(song, dict) and (row := _track(song, position))]
    if page and not rows:
        raise NeteaseCatalogError("网易云音乐未返回有效的歌曲搜索结果。")
    following = offset + len(page)
    more = len(page) == limit and following < _MAX_SEARCH_OFFSET and (total is None or following < total)
    return {"source": "netease", "title": query, "rows": rows, "total": total,
            "next_offset": following if more else None}


def _tracks_by_id(tracks):
    if not isinstance(tracks, list):
        return {}
    return {identifier: song for song in tracks if isinstance(song, dict)
            and (identifier := _number(song.get("id"))) and _text(song.get("name"))}


def _read_playlist(identifier, selection, session, stop, *, public=False):
    def request(path, params):
        if session is None:
            return netease_auth.netease_public_playlist_request(path, params)
        return netease_auth.netease_api_request(session, path, params)

    response = request("/api/v6/playlist/detail", {"id": identifier})
    detail = _record(_checked(response, "歌单曲目").get("playlist"))
    if _number(detail.get("id")) != identifier:
        raise NeteaseCatalogError("网易云音乐未返回所选歌单。")
    private = selection.get("private") or _number(detail.get("privacy")) == _PRIVATE
    if public and private:
        raise ValueError("这是私密歌单，请从网易云音乐资料库中打开。")
    songs = _tracks_by_id(detail.get("tracks"))
    total = _number(detail.get("trackCount"))
    entries = detail.get("trackIds")
    warning = ""
    if not isinstance(entries, list):
        if not songs and total != 0:
            raise NeteaseCatalogError("网易云音乐未返回歌单中的曲目编号。")
        if not public and total is None:
            warning = "网易云音乐未确认返回的歌单曲目是否完整。"
        entries = [{"id": track_id} for track_id in songs]
    ids = [_number(_record(track).get("id")) for track in entries]
    if not public and total is not None and len(ids) != total:
        warning = "网易云音乐返回的曲目数量与歌单不符。"
    missing = list(dict.fromkeys(track_id for track_id in ids if track_id and track_id not in songs))
    for offset in range(0, len(missing), _PAGE_SIZE):
        _cancelled(stop)
        batch = missing[offset:offset + _PAGE_SIZE]
        query = json.dumps([{"id": track_id} for track_id in batch], separators=(",", ":"))
        try:
            response = request("/api/v3/song/detail", {"c": query})
            data = _checked(response, "歌曲详情")
            if not isinstance(data.get("songs"), list):
                raise NeteaseCatalogError("网易云音乐未返回歌曲详情。")
        except (netease_auth.NeteaseAuthError, NeteaseCatalogError) as error:
            if (not songs or getattr(error, "session_expired", False)
                    or (not public and getattr(error, "authorization_denied", False))):
                raise
            warning = f"只读取到部分网易云音乐曲目。{error}"
            break
        songs.update(_tracks_by_id(data["songs"]))
    _cancelled(stop)
    rows = [row for position, track_id in enumerate(ids, 1) if track_id in songs
            and (row := _track(songs[track_id], position))]
    total = max(total or 0, len(ids))
    if not warning and len(rows) < total:
        warning = "网易云音乐未返回部分曲目。"
    return {"source": "netease", "title": _text(detail.get("name")) or selection.get("title") or "网易云音乐歌单",
            "url": "" if private else f"{_BASE}/playlist?id={identifier}", "total": total,
            "rows": rows, "warning": warning}


def resolve_playlist(session, playlist, *, stop=None):
    """Read one playlist returned for this account, in its original order."""
    _cancelled(stop)
    user_id = _account(session)
    selection = _record(playlist)
    if selection.get("_account_id") != user_id:
        raise NeteaseCatalogError("请刷新当前账号的网易云音乐歌单。", denied=True)
    identifier = _number(selection.get("id"))
    if not identifier:
        raise NeteaseCatalogError("网易云音乐未返回有效的歌单编号。")
    return _read_playlist(identifier, selection, session, stop)


def resolve_public_netease(value, *, session=None, stop=None):
    """Open an official sharing link, expanding public playlists in order."""
    _cancelled(stop)
    source, kind, identifier, url = shared_music._parse_link(value)
    if source != "netease":
        raise ValueError("请输入网易云音乐歌曲或歌单链接。")
    if kind == "playlist":
        try:
            return _read_playlist(int(identifier), {}, session, stop, public=True)
        except (netease_auth.NeteaseAuthError, NeteaseCatalogError):
            _cancelled(stop)
    result = shared_music.resolve_music(url)
    _cancelled(stop)
    return result
