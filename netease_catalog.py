"""Read the signed-in NetEase Cloud Music account's playlists and tracks."""

import json
import re

import netease_auth


_BASE = "https://music.163.com"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_PRIVATE = 10
_LIKED = 5
_AUTH_CODES = {301, 401}


class NeteaseCatalogError(RuntimeError):
    """NetEase rejected a catalog read or returned unusable data."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


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
    if code in {403, 404}:
        raise NeteaseCatalogError(f"当前账号无法访问网易云音乐{label}。", code=code)
    if code != 200:
        raise NeteaseCatalogError(f"暂时无法读取网易云音乐{label}。", code=code)
    return response


def _account(session):
    if not isinstance(session, netease_auth.NeteaseSession) or not _number(session.user_id):
        raise NeteaseCatalogError("请先登录网易云音乐。")
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
    if _number(_record(account.get("profile")).get("userId")) != user_id:
        raise NeteaseCatalogError("网易云音乐登录已失效，请重新扫码。", code=301)

    warning = ""
    playlists = {}
    for offset in range(0, _MAX_PAGES * _PAGE_SIZE, _PAGE_SIZE):
        _cancelled(stop)
        try:
            response = netease_auth.netease_api_request(session, "/api/user/playlist",
                                                       {"uid": user_id, "limit": _PAGE_SIZE, "offset": offset})
            data = _checked(response, "歌单")
            page = data.get("playlist")
            if not isinstance(page, list):
                raise NeteaseCatalogError("网易云音乐未返回歌单列表。")
        except (netease_auth.NeteaseAuthError, NeteaseCatalogError) as error:
            if not playlists or getattr(error, "code", None) in _AUTH_CODES:
                raise
            warning = f"只读取到部分网易云音乐歌单。{error}"
            break
        previous = len(playlists)
        for item in page:
            if entry := _playlist(item, user_id):
                playlists.setdefault(entry["id"], entry)
        more = data.get("more")
        if not page or more is False or _number(more) == 0 or (more is None and len(page) < _PAGE_SIZE):
            break
        if len(playlists) == previous:
            warning = "网易云音乐重复返回歌单，已保留读取到的部分。"
            break
    else:
        warning = "歌单数量超出单次读取范围。"
    _cancelled(stop)
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


def _tracks_by_id(tracks):
    if not isinstance(tracks, list):
        return {}
    return {identifier: song for song in tracks if isinstance(song, dict)
            and (identifier := _number(song.get("id"))) and _text(song.get("name"))}


def resolve_playlist(session, playlist, *, stop=None):
    """Read one playlist returned for this account, in its original order."""
    _cancelled(stop)
    user_id = _account(session)
    selection = _record(playlist)
    if selection.get("_account_id") != user_id:
        raise NeteaseCatalogError("请刷新当前账号的网易云音乐歌单。")
    identifier = _number(selection.get("id"))
    if not identifier:
        raise NeteaseCatalogError("网易云音乐未返回有效的歌单编号。")
    response = netease_auth.netease_api_request(session, "/api/v6/playlist/detail", {"id": identifier})
    detail = _record(_checked(response, "歌单曲目").get("playlist"))
    if _number(detail.get("id")) != identifier:
        raise NeteaseCatalogError("网易云音乐未返回所选歌单。")
    songs = _tracks_by_id(detail.get("tracks"))
    total = _number(detail.get("trackCount"))
    entries = detail.get("trackIds")
    if not isinstance(entries, list):
        if not songs and total != 0:
            raise NeteaseCatalogError("网易云音乐未返回歌单中的曲目编号。")
        entries = [{"id": track_id} for track_id in songs]
    ids = [_number(_record(track).get("id")) for track in entries]
    missing = list(dict.fromkeys(track_id for track_id in ids if track_id and track_id not in songs))
    warning = ""
    for offset in range(0, len(missing), _PAGE_SIZE):
        _cancelled(stop)
        batch = missing[offset:offset + _PAGE_SIZE]
        query = json.dumps([{"id": track_id} for track_id in batch], separators=(",", ":"))
        try:
            response = netease_auth.netease_api_request(session, "/api/v3/song/detail", {"c": query})
            data = _checked(response, "歌曲详情")
            if not isinstance(data.get("songs"), list):
                raise NeteaseCatalogError("网易云音乐未返回歌曲详情。")
        except (netease_auth.NeteaseAuthError, NeteaseCatalogError) as error:
            if not songs or getattr(error, "code", None) in _AUTH_CODES:
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
    private = selection.get("private") or _number(detail.get("privacy")) == _PRIVATE
    return {"source": "netease", "title": _text(detail.get("name")) or selection["title"],
            "url": "" if private else f"{_BASE}/playlist?id={identifier}", "total": total,
            "rows": rows, "warning": warning}
