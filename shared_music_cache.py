"""Cache complete QQ Music and NetEase library reads by verified account."""

from hashlib import sha256
import json
import re
import sqlite3

from library_cache import LibraryCache
from shared_music import _parse_link


_KINDS = frozenset(("liked", "created", "collected"))
_QQ_ACCOUNT = re.compile(r"[A-Za-z0-9]{1,64}")
_NUMERIC_ID = re.compile(r"[1-9][0-9]{0,19}")
_QQ_SONG_ID = re.compile(r"[A-Za-z0-9]{14}")
_CACHE_ERRORS = (OSError, ValueError, sqlite3.Error, RecursionError)


def _need(condition):
    if not condition:
        raise ValueError("Invalid music library snapshot")


def _integer(value, *, nullable=False):
    _need((nullable and value is None) or (type(value) is int and 0 <= value < 10**20))
    return value


def _text(value, *, optional=False, limit=240):
    _need(isinstance(value, str) and (optional or value.strip()) and len(value) <= limit and "\x00" not in value)
    return value


def _url(value, source, kind, identifier, *, optional=False):
    if optional and value == "":
        return ""
    platform, link_type, link_id, canonical = _parse_link(value)
    _need((platform, link_type, link_id) == (source, kind, identifier))
    return canonical


def _playlist_row(source, item):
    _need(isinstance(item, dict))
    identifier = _text(item.get("id"))
    kind = _text(item.get("kind"))
    private = item.get("private")
    _need(kind in _KINDS and type(private) is bool)
    row = {"id": identifier, "kind": kind, "title": _text(item.get("title")), "private": private,
           "total": _integer(item.get("total"), nullable=True)}
    link_id = identifier
    if source == "qq":
        directory = _integer(item.get("dir_id"))
        tid = _integer(item.get("tid"))
        _need(directory or tid)
        if kind == "liked":
            _need(directory == 201 and identifier == "liked:201")
        else:
            _need(identifier == (str(tid) if tid else f"directory:{directory}"))
            _need(kind == "created" or (tid and directory == 0 and not private))
        row.update(dir_id=directory, tid=tid)
        link_id = str(tid)
        private = private or kind == "liked" or not tid
    else:
        _need(_NUMERIC_ID.fullmatch(identifier))
    value = item.get("url")
    _need(not private or value == "")
    row["url"] = _url(value, source, "playlist", link_id, optional=True)
    return row


def _track_row(source, item, position):
    _need(isinstance(item, dict) and item.get("source") == source)
    identifier = _text(item.get("id"), limit=20)
    id_pattern = _QQ_SONG_ID if source == "qq" else _NUMERIC_ID
    _need(id_pattern.fullmatch(identifier) and _integer(item.get("position")) == position)
    seconds = item.get("duration")
    _need(type(seconds) in (int, float) and 0 <= seconds < 10**10)
    row = {"source": source, "id": identifier, "title": _text(item.get("title")),
           "artist": _text(item.get("artist", ""), optional=True, limit=8192),
           "album": _text(item.get("album", ""), optional=True), "position": position, "duration": seconds,
           "url": _url(item.get("url"), source, "song", identifier)}
    if source == "qq":
        mid = _text(item.get("media_mid", ""), optional=True, limit=14)
        _need(not mid or _QQ_SONG_ID.fullmatch(mid))
        row["media_mid"] = mid
    return row


def _scope(source, account_id, playlist):
    if source == "qq":
        _need(isinstance(account_id, str) and account_id != "0" and _QQ_ACCOUNT.fullmatch(account_id))
    else:
        _need(source == "netease" and _integer(account_id) and account_id < 10**19)
    identifier = ""
    if playlist is not None:
        _need(isinstance(playlist, dict) and type(playlist.get("_account_id")) is type(account_id)
              and playlist["_account_id"] == account_id)
        identifier = _playlist_row(source, playlist)["id"]
    key = json.dumps([source, account_id, identifier], separators=(",", ":")).encode("ascii")
    return "shared-music:1:" + sha256(key).hexdigest()


def _snapshot(source, account_id, result, playlist, *, from_cache=False):
    _need(isinstance(result, dict) and result.get("source") == source)
    _need(not result.get("warning") and not result.get("warnings"))
    rows = result.get("rows")
    _need(isinstance(rows, list) and _integer(result.get("total")) == len(rows))
    if playlist is None:
        if not from_cache:
            _need(all(isinstance(row, dict) and type(row.get("_account_id")) is type(account_id)
                      and row["_account_id"] == account_id for row in rows))
        clean = [_playlist_row(source, row) for row in rows]
        _need(len({row["id"] for row in clean}) == len(clean))
        if from_cache:
            for row in clean:
                row["_account_id"] = account_id
        return {"source": source, "rows": clean, "total": len(clean), "warnings": []}
    selected = _playlist_row(source, playlist)
    clean = [_track_row(source, row, position) for position, row in enumerate(rows, 1)]
    value = result.get("url")
    _need(not value or selected["url"])
    identifier = str(selected["tid"]) if source == "qq" else selected["id"]
    url = _url(value, source, "playlist", identifier, optional=True)
    return {"source": source, "title": _text(result.get("title")), "url": url,
            "rows": clean, "total": len(clean), "warning": ""}


class SharedMusicCache:
    def __init__(self, path):
        self.store = LibraryCache(path)

    def read(self, source, account_id, *, playlist=None):
        """Return a complete snapshot, including an empty one, or None on a cache miss."""
        try:
            key = _scope(source, account_id, playlist)
            records = self.store.load(key)
            if records is not None and len(records) == 1 and records[0].get("scope") == key:
                return _snapshot(source, account_id, records[0], playlist, from_cache=True)
        except _CACHE_ERRORS:
            pass
        return None

    def replace(self, source, account_id, result, *, playlist=None):
        """Accept only a complete, warning-free response from the current account."""
        try:
            key = _scope(source, account_id, playlist)
            record = _snapshot(source, account_id, result, playlist)
            self.store.save(key, [dict(record, scope=key)])
        except _CACHE_ERRORS:
            return False
        return True

    def discard(self, source, account_id, *, playlist=None):
        """Forget one verified scope after access to it is denied."""
        try:
            return self.store.discard(_scope(source, account_id, playlist))
        except _CACHE_ERRORS:
            return False
