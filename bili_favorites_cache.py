"""Cache complete Bilibili favorites snapshots for the verified account."""

from hashlib import sha256
import sqlite3

from bili_catalog import VIDEO_BASE, parse_video_link
from library_cache import LibraryCache


_CACHE_ERRORS = (OSError, ValueError, sqlite3.Error, RecursionError)
_KINDS = ("favorite", "followed-favorite", "season")


def _require(condition):
    if not condition:
        raise ValueError("Invalid Bilibili favorites snapshot")


def _integer(value, *, minimum=0):
    _require(type(value) is int and minimum <= value < 10**19)
    return value


def _text(value, *, optional=False):
    _require(isinstance(value, str) and len(value) <= 220 and "\x00" not in value and (optional or value.strip()))
    return value


def _owner_matches(row, account_id):
    return type(row.get("_account_id")) is int and row["_account_id"] == account_id


def _folder(account_id, row, *, stored=False):
    _require(isinstance(row, dict))
    kind = row.get("kind")
    _require(kind in _KINDS)
    collection_id = _integer(row.get("collection_id"), minimum=1)
    _require("owner_id" in row)
    owner = row.get("owner_id")
    if stored and owner is None:
        owner = account_id
    owner = _integer(owner, minimum=1)
    _require(kind != "favorite" or owner == account_id)
    identifier = f"{kind}:{collection_id}"
    path = f"lists/{collection_id}?type=season" if kind == "season" else f"favlist?fid={collection_id}"
    url = f"https://space.bilibili.com/{owner}/{path}"
    _require(row.get("id") == identifier)
    if not stored:
        _require(_owner_matches(row, account_id) and row.get("url") == url)
    return {"id": identifier, "collection_id": collection_id, "kind": kind, "owner_id": owner,
            "title": _text(row.get("title")), "total": _integer(row.get("total")),
            "url": url, "_account_id": account_id}


def _scope(account_id, folder):
    _integer(account_id, minimum=1)
    key = str(account_id)
    if folder is not None:
        key += f":{folder['owner_id']}:{folder['id']}"
    return "bili-favorites:1:" + sha256(key.encode("ascii")).hexdigest()


def _video_row(item, batch_key, title, total, previous):
    _require(isinstance(item, dict) and item.get("source") == "bilibili")
    identifier = _text(item.get("id"))
    _require(parse_video_link(identifier) == (identifier, None))
    url = f"{VIDEO_BASE}{identifier}?p=1"
    _require(item.get("url") == url)
    _require(item.get("batch_key") == batch_key)
    position = _integer(item.get("position"), minimum=previous + 1)
    _require(position <= total and _integer(item.get("total")) == total)
    preview_title = item.get("preview_audio_title")
    if preview_title is not None:
        preview_title = _text(preview_title, optional=True)
    media_category = item.get("preview_media_category", "")
    _require(media_category in ("", "music", "video"))
    return {"source": "bilibili", "id": identifier, "url": url, "title": _text(item.get("title")),
            "artist": "", "uploader": _text(item.get("uploader", ""), optional=True),
            "preview_audio_title": preview_title,
            "preview_artist": _text(item.get("preview_artist", ""), optional=True),
            "preview_media_category": media_category,
            "preview_source_category": _text(item.get("preview_source_category", ""), optional=True),
            "position": position, "duration": _integer(item.get("duration")), "total": total,
            "batch_key": batch_key, "batch_title": title}


def _snapshot(account_id, result, folder, *, stored=False):
    _require(isinstance(result, dict) and result.get("source") == "bilibili" and result.get("complete") is True
             and (stored or _owner_matches(result, account_id)))
    rows = result.get("rows")
    _require(isinstance(rows, list))
    total = _integer(result.get("total"))
    warning = result.get("warning", "")
    title = _text(result.get("title"))
    common = {"source": "bilibili", "complete": True, "title": title, "total": total,
              "warning": warning, "_account_id": account_id}
    if folder is None:
        _require(result.get("view") == "favorites")
        _require(total == len(rows) and not warning)
        clean = [_folder(account_id, row, stored=stored) for row in rows]
        _require(len({row["id"] for row in clean}) == len(clean))
        return dict(common, view="favorites", rows=clean, warning="")

    identifier = str(folder["collection_id"])
    kind = "season" if folder["kind"] == "season" else "favorite"
    _require(result.get("id") == identifier and result.get("folder_id") == folder["id"])
    _require(result.get("collection_kind") == kind and (stored or result.get("url") == folder["url"]))
    scanned = _integer(result.get("scanned"))
    skipped = _integer(result.get("skipped"))
    _require(scanned == total == len(rows) + skipped)
    expected_warning = f"已跳过 {skipped} 项不可用或不支持的内容" if skipped else ""
    _require(warning == expected_warning)
    clean = []
    previous = 0
    batch_key = f"bilibili:{kind}:{identifier}"
    for item in rows:
        row = _video_row(item, batch_key, title, total, previous)
        clean.append(row)
        previous = row["position"]
    return dict(common, id=identifier, rows=clean, collection_kind=kind, folder_id=folder["id"],
                url=folder["url"], scanned=scanned, skipped=skipped)


class BiliFavoritesCache:
    def __init__(self, path):
        self.store = LibraryCache(path)

    def read(self, account_id, *, folder=None):
        """Return a verified complete snapshot or None on a cache miss."""
        try:
            selected = _folder(account_id, folder) if folder is not None else None
            key = _scope(account_id, selected)
            records = self.store.load(key)
            if records and len(records) == 1 and records[0].get("scope") == key:
                return _snapshot(account_id, records[0], selected, stored=True)
        except _CACHE_ERRORS:
            pass
        return None

    def replace(self, account_id, result, *, folder=None):
        """Save complete account or folder scans without credentials or the account ID."""
        try:
            selected = _folder(account_id, folder) if folder is not None else None
            key = _scope(account_id, selected)
            record = _snapshot(account_id, result, selected)
            record.pop("_account_id")
            if selected is None:
                for row in record["rows"]:
                    row.pop("_account_id")
                    row.pop("url")
                    if row["owner_id"] == account_id:
                        row["owner_id"] = None
            else:
                record.pop("url")
            self.store.save(key, [dict(record, scope=key)])
        except _CACHE_ERRORS:
            return False
        return True

    def discard(self, account_id, *, folder=None):
        """Remove one scope whose account or folder access was denied."""
        try:
            selected = _folder(account_id, folder) if folder is not None else None
            return self.store.discard(_scope(account_id, selected))
        except _CACHE_ERRORS:
            return False
