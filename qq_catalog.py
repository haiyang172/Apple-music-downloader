"""Read QQ Music public playlists and the current account's own collection."""

import re

import qq_auth
import shared_music


_PLAYLIST_LIST = "music.musicasset.PlaylistBaseRead"
_PLAYLIST_DETAIL = "music.srfDissInfo.DissInfo"
_WEB_OPTIONS = {"cv": 13020508, "v": 13020508}
_TRACK_MID = re.compile(r"[A-Za-z0-9]{14}")
_ENCODED_UIN = re.compile(r"[A-Za-z0-9_.~+\-/=]{1,128}")
_AUTH_CODES = qq_auth.QQ_AUTH_EXPIRED_CODES | {1101, 80030}
_ACCESS_DENIED = 10004
_FAVORITE = 201
_PAGE_SIZE = 100
_COLLECTED_PAGE_SIZE = 80
_MAX_PAGES = 100
_SEARCH_PAGE_SIZE = 50
_PUBLIC_BASE = "https://y.qq.com/n/ryqq_v2"


class QqCatalogError(RuntimeError):
    """QQ Music rejected a catalog request or returned unusable data."""

    def __init__(self, message, *, code=None, denied=False):
        super().__init__(message)
        self.code = code
        self.session_expired = code in _AUTH_CODES
        self.authorization_denied = denied or self.session_expired or code == _ACCESS_DENIED


def _number(value):
    if type(value) is int and abs(value) < 10**19:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,19}", value):
        return int(value)
    return None


def _text(value):
    return " ".join(value.split())[:240] if isinstance(value, str) else ""


def _record(value):
    return value if isinstance(value, dict) else {}


def _more(data, key, alternate):
    value = data.get(key, data.get(alternate))
    return int(value) if type(value) is bool else _number(value)


def _cancelled(stop):
    if stop is not None and stop.is_set():
        raise QqCatalogError("已取消读取 QQ 音乐。")


def _data(response, label):
    code = _number(response.get("code"))
    if code in _AUTH_CODES:
        raise QqCatalogError("QQ 音乐登录已失效，请重新扫码。", code=code)
    if code == _ACCESS_DENIED:
        raise QqCatalogError("当前账号无法访问这个 QQ 音乐歌单。", code=code)
    data = response.get("data")
    if code != 0 or not isinstance(data, dict):
        raise QqCatalogError(f"暂时无法读取 QQ 音乐{label}。", code=code)
    return data


def _rows(data, key, label):
    rows = data.get(key)
    if not isinstance(rows, list):
        raise QqCatalogError(f"QQ 音乐未返回{label}。")
    return rows


def _playlist(raw, kind, session):
    row = _record(raw)
    if kind == "created":
        title = _text(row.get("dirName"))
        directory = _number(row.get("dirId")) or 0
        tid = _number(row.get("tid")) or 0
        total = _number(row.get("songNum"))
        private = _number(row.get("dirShow", row.get("dir_show"))) == 2
    else:
        title = _text(row.get("dissname") or row.get("diss_name"))
        directory = 0
        tid = _number(row.get("dissid") or row.get("tid") or row.get("diss_id")) or 0
        total = _number(row.get("songnum", row.get("song_cnt")))
        private = False
    if directory == _FAVORITE and kind == "created":
        kind = "liked"
        title = title or "我喜欢的音乐"
    if not title or (directory <= 0 and tid <= 0):
        return None
    identifier = ("liked:201" if kind == "liked" else str(tid) if tid > 0 else f"directory:{directory}")
    return {"id": identifier, "dir_id": max(0, directory), "tid": max(0, tid), "kind": kind,
            "title": title, "total": total if total is not None and total >= 0 else None, "private": private,
            "url": f"{_PUBLIC_BASE}/playlist/{tid}" if tid > 0 and kind != "liked" and not private else "",
            "_account_id": session.musicid}


def _collected(session, stop):
    warnings = []
    rows = []
    seen = set()
    next_expected = False
    incomplete = False
    for start in range(0, _COLLECTED_PAGE_SIZE * _MAX_PAGES, _COLLECTED_PAGE_SIZE):
        _cancelled(stop)
        try:
            response = qq_auth.qq_profile_request(session, "collected", start=start, end=start + _COLLECTED_PAGE_SIZE - 1)
            data = _data(response, "收藏歌单")
            page = _rows(data, "cdlist", "收藏歌单")
            if len(page) > _COLLECTED_PAGE_SIZE:
                raise QqCatalogError("QQ 音乐返回了不完整的收藏歌单分页。")
        except (qq_auth.QqAuthError, QqCatalogError) as error:
            _cancelled(stop)
            if getattr(error, "authorization_denied", False):
                raise
            warnings.append(str(error))
            break
        new = []
        for raw in page:
            if (item := _playlist(raw, "collected", session)) and item["id"] not in seen:
                seen.add(item["id"])
                new.append(item)
        rows.extend(new)
        has_more = _more(data, "has_more", "hasmore")
        if not page and (next_expected or has_more not in (None, 0)):
            warnings.append("QQ 音乐未返回收藏歌单的下一页。")
            break
        incomplete = incomplete or (bool(new) and len(new) < len(page))
        if page and not new:
            warnings.append("QQ 音乐重复返回收藏歌单或缺少歌单信息，已保留读取到的部分。")
            break
        if not page or has_more == 0 or (has_more is None and len(page) < _COLLECTED_PAGE_SIZE):
            break
        next_expected = has_more not in (None, 0)
    else:
        warnings.append("收藏歌单数量超出单次读取范围。")
    if incomplete and not warnings:
        warnings.append("QQ 音乐未返回部分收藏歌单。")
    return rows, warnings


def list_playlists(session, *, stop=None):
    """List the signed-in account's created and collected playlists."""
    _cancelled(stop)
    if not isinstance(session, qq_auth.QqSession):
        raise QqCatalogError("请先登录 QQ 音乐。", denied=True)
    response = qq_auth.qq_music_request(session, _PLAYLIST_LIST, "GetPlaylistByUin", {"uin": session.musicid},
                                        common=_WEB_OPTIONS)
    created = _rows(_data(response, "自建歌单"), "v_playlist", "自建歌单")
    own = [item for raw in created if (item := _playlist(raw, "created", session))]
    incomplete = len({item["id"] for item in own}) < len(created)
    if not any(item["kind"] == "liked" for item in own):
        own.insert(0, _playlist({"dirId": _FAVORITE}, "created", session))
    collected, warnings = _collected(session, stop)
    if incomplete:
        warnings.insert(0, "QQ 音乐未返回部分自建歌单。")
    _cancelled(stop)
    combined = {}
    for item in own + collected:
        combined.setdefault(item["id"], item)
    rows = sorted(combined.values(), key=lambda item: item["kind"] != "liked")
    return {"source": "qq", "rows": rows, "total": len(rows), "warnings": warnings}


def _own_directory(session, directory):
    profile = _data(qq_auth.qq_profile_request(session, "homepage"), "个人资料")
    owner = _record(profile.get("creator")).get("encrypt_uin")
    if not isinstance(owner, str) or not _ENCODED_UIN.fullmatch(owner):
        raise QqCatalogError("暂时无法确认 QQ 音乐歌单的账号归属。")
    return {"disstid": 0, "dirid": directory, "enc_host_uin": owner}


def _track(raw, position):
    song = _record(raw)
    nested = _record(song.get("track_info"))
    mid = song.get("mid") or nested.get("mid") or song.get("songmid")
    if not isinstance(mid, str) or not _TRACK_MID.fullmatch(mid):
        return None
    title = _text(song.get("name")) or _text(song.get("title")) or _text(nested.get("name") or nested.get("title"))
    if not title:
        return None
    singers = song.get("singer") or nested.get("singer")
    names = (_text(_record(item).get("name")) for item in singers) if isinstance(singers, list) else ()
    album = _record(song.get("album") or nested.get("album"))
    duration = _number(song.get("interval")) or _number(nested.get("interval")) or 0
    file = _record(song.get("file") or nested.get("file"))
    media_mid = file.get("media_mid") or song.get("media_mid") or nested.get("media_mid")
    return {"source": "qq", "id": mid, "title": title, "artist": " / ".join(dict.fromkeys(filter(None, names))),
            "album": _text(album.get("name") or album.get("title")), "duration": max(0, duration),
            "position": position, "url": f"{_PUBLIC_BASE}/songDetail/{mid}",
            "media_mid": media_mid if isinstance(media_mid, str) and _TRACK_MID.fullmatch(media_mid) else ""}


def _playlist_tracks(playlist, identity, session, stop, *, private=False):
    rows = []
    title = playlist.get("title")
    total = playlist.get("total")
    position = 0
    warning = ""
    previous = None
    reported_total = None
    next_expected = False
    incomplete = False
    for _ in range(_MAX_PAGES):
        _cancelled(stop)
        params = {**identity, "song_begin": position, "song_num": _PAGE_SIZE, "onlysonglist": True,
                  "userinfo": True, "tag": 1, "orderlist": True}
        try:
            response = (qq_auth.qq_music_request(session, _PLAYLIST_DETAIL, "CgiGetDiss", params, common=_WEB_OPTIONS)
                        if session is not None else qq_auth.qq_public_playlist_request(params))
            data = _data(response, "歌单曲目")
            page = _rows(data, "songlist", "歌单曲目")
        except (qq_auth.QqAuthError, QqCatalogError) as error:
            if (not rows or getattr(error, "session_expired", False)
                    or (private and getattr(error, "authorization_denied", False))):
                raise
            warning = f"只读取到部分 QQ 音乐曲目。{error}"
            break
        info = _record(data.get("dirinfo"))
        title = title or _text(info.get("title"))
        count = _number(data.get("total_song_num"))
        if count is not None and count >= 0:
            incomplete = incomplete or (reported_total is not None and reported_total != count)
            reported_total = count
            total = count
        tracks = [row for index, item in enumerate(page, position + 1) if (row := _track(item, index))]
        signature = tuple(row["id"] for row in tracks)
        if page and signature and signature == previous:
            warning = "QQ 音乐重复返回同一页曲目，已保留读取到的部分。"
            break
        previous = signature
        rows.extend(tracks)
        position += len(page)
        has_more = _more(data, "hasmore", "has_more")
        if private:
            incomplete = incomplete or len(page) > _PAGE_SIZE or bool(
                (not page and (next_expected or has_more not in (None, 0)))
                or (reported_total is not None
                    and (position > reported_total or (position == reported_total and has_more not in (None, 0))))
            )
        if not page or has_more == 0 or (total is not None and position >= total):
            break
        if has_more is None and total is None and len(page) < _PAGE_SIZE:
            break
        next_expected = has_more not in (None, 0)
    else:
        warning = "歌单曲目数量超出单次读取范围。"
    _cancelled(stop)
    if not warning and ((private and incomplete) or (total is not None and len(rows) < total) or len(rows) < position):
        warning = "QQ 音乐未返回部分曲目。"
    return {"source": "qq", "title": title or "QQ 音乐歌单", "url": playlist["url"],
            "total": max(total, len(rows)) if total is not None else len(rows), "rows": rows, "warning": warning}


def resolve_playlist(session, playlist, *, stop=None):
    """Resolve a playlist row returned for the current QQ Music account."""
    _cancelled(stop)
    if not isinstance(session, qq_auth.QqSession) or _record(playlist).get("_account_id") != session.musicid:
        raise QqCatalogError("请先登录 QQ 音乐并刷新当前账号的歌单。", denied=True)
    directory, tid = _number(playlist.get("dir_id")) or 0, _number(playlist.get("tid")) or 0
    kind = playlist.get("kind")
    if kind == "liked" or (kind == "created" and directory > 0 and (tid <= 0 or playlist.get("private"))):
        return _playlist_tracks(playlist, _own_directory(session, directory), session, stop, private=True)
    if tid <= 0 or kind not in {"created", "collected"}:
        raise QqCatalogError("QQ 音乐未返回有效的歌单编号。")
    try:
        return _playlist_tracks(playlist, {"disstid": tid, "dirid": 0}, session, stop, private=True)
    except QqCatalogError as error:
        if error.code != _ACCESS_DENIED or kind != "created" or directory <= 0:
            raise
    return _playlist_tracks(playlist, _own_directory(session, directory), session, stop, private=True)


def resolve_public_qq(value, *, session=None, stop=None):
    """Read a QQ Music sharing link without looking up an account's collection."""
    _cancelled(stop)
    source, kind, identifier, url = shared_music._parse_link(value)
    if source != "qq":
        raise ValueError("请输入 QQ 音乐歌曲或歌单链接。")
    if kind != "playlist":
        return shared_music.resolve_music(value)
    playlist = {"url": url}
    identity = {"disstid": int(identifier), "dirid": 0}
    try:
        return _playlist_tracks(playlist, identity, session, stop)
    except (qq_auth.QqAuthError, QqCatalogError):
        _cancelled(stop)
        return shared_music.resolve_music(url)


def search_songs(query, *, session=None, stop=None, offset=0, limit=50):
    """Search public songs independently of the current account session."""
    _cancelled(stop)
    query = " ".join(query.split()) if isinstance(query, str) else ""
    if not 0 < len(query) <= 120 or any(ord(character) < 32 or ord(character) == 127 for character in query):
        raise ValueError("请输入 1–120 字的 QQ 音乐搜索关键词。")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= _SEARCH_PAGE_SIZE:
        raise ValueError("QQ 音乐搜索分页参数无效。")

    rows = []
    total = next_offset = None
    first_page = offset - offset % _SEARCH_PAGE_SIZE
    for page_start in range(first_page, offset + limit, _SEARCH_PAGE_SIZE):
        _cancelled(stop)
        page = page_start // _SEARCH_PAGE_SIZE + 1
        try:
            data = _data(qq_auth.qq_public_search_request(query, page, _SEARCH_PAGE_SIZE), "搜索结果")
            search = _record(data.get("song"))
            songs = _rows(search, "list", "搜索结果")[:_SEARCH_PAGE_SIZE]
        except (qq_auth.QqAuthError, QqCatalogError):
            raise QqCatalogError("暂时无法搜索 QQ 音乐，请稍后重试。") from None
        if _number(search.get("curpage")) not in {None, page}:
            raise QqCatalogError("QQ 音乐未返回所请求的搜索页。")
        published_total = _number(search.get("totalnum"))
        if published_total is not None and published_total >= 0:
            total = published_total
        begin = max(offset, page_start)
        end = min(offset + limit, page_start + len(songs))
        for position in range(begin, end):
            item = _record(songs[position - page_start])
            title = _text(item.get("title")) or _text(item.get("name"))
            if row := _track(dict(item, name=title), position + 1):
                rows.append(row)

        if not songs or (total is not None and end >= total):
            next_offset = None
            break
        if end < page_start + len(songs):
            next_offset = end
        elif len(songs) == _SEARCH_PAGE_SIZE or (total is not None and total > page_start + _SEARCH_PAGE_SIZE):
            next_offset = page_start + _SEARCH_PAGE_SIZE
        else:
            next_offset = None
            break
    _cancelled(stop)
    return {"source": "qq", "title": query, "rows": rows, "total": total, "next_offset": next_offset}
