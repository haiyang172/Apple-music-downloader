"""Resolve public or account-accessible Bilibili lists into video queue rows."""

from dataclasses import dataclass
from hashlib import sha256
from http.client import HTTPException
from itertools import count
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import bili_catalog
from bili_auth import BiliAccount, BiliSession
from bili_catalog import SOURCE, VIDEO_BASE, parse_video_link
from engine import Cancelled


_SPACE = "https://space.bilibili.com"
_API = "https://api.bilibili.com"
_LABELS = {"season": "合集", "series": "系列", "favorite": "收藏夹", "followed-favorite": "收藏的收藏夹"}
_FOLLOWED_KINDS = {11: "followed-favorite", 21: "season"}
_BAD_LINK = "请输入 B 站合集、系列或带编号的收藏夹链接。"
_BAD_RESPONSE = "B 站返回的列表信息无效。"
_DENIED = "无法访问该 B 站列表，请在浏览器中确认内容和账号权限。"
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_ITEMS = 500
_PAGE_SIZE = 30
_FAVORITE_PAGE_SIZE = 20
_NUMBER = r"[1-9][0-9]{0,18}"
_SPACE_LIST = re.compile(rf"/({_NUMBER})/lists/({_NUMBER})/?")
_SPACE_LEGACY = re.compile(rf"/({_NUMBER})/channel/(series|collection)detail/?")
_SPACE_FAVORITE = re.compile(rf"/({_NUMBER})/favlist/?")
_FAVORITE = re.compile(rf"/(?:medialist/(?:detail/ml|play/)|list/)({_NUMBER})/?")


@dataclass(frozen=True)
class _CollectionLink:
    kind: str
    identifier: int
    owner: int | None
    url: str


class BiliCollectionError(RuntimeError):
    def __init__(self, message, *, auth_expired=False, denied=False, mismatched=False):
        super().__init__(message)
        self.auth_expired = auth_expired
        self.denied = denied
        self.mismatched = mismatched


def _expired():
    return BiliCollectionError("B 站登录已过期，请重新扫码。", auth_expired=True)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _parameter(query, *names):
    values = [value for name in names for value in query.get(name, ())]
    return values[0] if len(values) == 1 and re.fullmatch(_NUMBER, values[0]) else ""


def _parse(value):
    if not isinstance(value, str):
        raise ValueError(_BAD_LINK)
    value = value.strip()
    if len(value) > 4096 or any(char.isspace() or ord(char) < 33 or char in "\\\x7f" for char in value):
        raise ValueError(_BAD_LINK)
    try:
        link = urlsplit(value)
        query = parse_qs(link.query, keep_blank_values=True, max_num_fields=40)
        host = link.hostname
    except ValueError:
        raise ValueError(_BAD_LINK) from None
    if link.scheme != "https" or host is None or link.netloc.lower() != host:
        raise ValueError(_BAD_LINK)

    kind, identifier, owner = "", "", ""
    if host == "space.bilibili.com":
        if match := _SPACE_LIST.fullmatch(link.path):
            types = query.get("type", ["season"])
            if types not in (["season"], ["series"]):
                raise ValueError(_BAD_LINK)
            owner, identifier = match.groups()
            kind = types[0]
        elif match := _SPACE_LEGACY.fullmatch(link.path):
            owner, family = match.groups()
            identifier = _parameter(query, "sid", "series_id" if family == "series" else "season_id")
            kind = "series" if family == "series" else "season"
        elif match := _SPACE_FAVORITE.fullmatch(link.path):
            owner = match[1]
            identifier = _parameter(query, "fid", "media_id")
            kind = "favorite"
    elif host in {"bilibili.com", "www.bilibili.com"} and (match := _FAVORITE.fullmatch(link.path)):
        identifier, kind = match[1], "favorite"
    if not identifier:
        raise ValueError(_BAD_LINK)

    if kind == "favorite":
        url = f"{_SPACE}/{owner}/favlist?fid={identifier}" if owner else f"https://www.bilibili.com/list/{identifier}"
    else:
        url = f"{_SPACE}/{owner}/lists/{identifier}?type={kind}"
    return _CollectionLink(kind, int(identifier), int(owner) if owner else None, url)


def is_collection_link(value):
    """Check whether a link denotes a supported Bilibili list."""
    try:
        _parse(value)
    except ValueError:
        return False
    return True


def _request(endpoint, params, referer, session):
    url = f"{_API}{endpoint}?{urlencode(params)}"
    headers = {"Accept": "application/json", "Referer": referer, "User-Agent": "Mozilla/5.0"}
    if session is not None:
        headers["Cookie"] = session.cookie_header()
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=9) as response:
            raw = response.read(_MAX_RESPONSE + 1)
    except HTTPError as error:
        error.close()
        if error.code == 401 and session is not None:
            raise _expired() from None
        if error.code in (401, 403, 404, 410):
            raise BiliCollectionError(_DENIED, denied=True) from None
        if error.code in (412, 429):
            raise RuntimeError("B 站暂时拒绝请求，请稍后重试。") from None
        raise RuntimeError("B 站列表暂时无法读取，请稍后重试。") from None
    except (OSError, HTTPException):
        raise RuntimeError("无法连接 B 站，请检查网络后重试。") from None

    if len(raw) > _MAX_RESPONSE:
        raise RuntimeError("B 站返回的列表信息过大。")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise RuntimeError(_BAD_RESPONSE) from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise RuntimeError(_BAD_RESPONSE)
    code = payload["code"]
    if code == -101 and session is not None:
        raise _expired()
    if code in (-412, -429, -352):
        raise RuntimeError("B 站暂时拒绝请求，请稍后重试。")
    if code in (-101, -403, -404):
        raise BiliCollectionError(_DENIED, denied=True)
    if code != 0:
        raise RuntimeError(_DENIED)
    data = payload.get("data")
    if data is None and endpoint == "/x/v3/fav/folder/created/list-all":
        raise BiliCollectionError(_DENIED, denied=True)
    if not isinstance(data, dict):
        raise RuntimeError(_BAD_RESPONSE)
    return data


def _collection_meta(link, data, key, id_key):
    meta = data.get(key)
    if meta is None and link.kind == "favorite":
        raise BiliCollectionError(_DENIED, denied=True)
    if (not isinstance(meta, dict) or not _identifier(meta.get(id_key)) or meta[id_key] != link.identifier
            or (link.owner is not None and (not _identifier(meta.get("mid")) or meta["mid"] != link.owner))):
        raise BiliCollectionError("B 站返回的列表与请求链接不符。", mismatched=True)
    return meta


def _list_page(link, page, fetch):
    if link.kind == "favorite":
        data = fetch("/x/v3/fav/resource/list", {"media_id": link.identifier, "pn": page, "ps": _FAVORITE_PAGE_SIZE,
                     "keyword": "", "order": "mtime", "type": 0, "tid": 0, "platform": "web"})
        meta = _collection_meta(link, data, "info", "id")
        total = meta.get("media_count")
        more = data.get("has_more")
        items = data.get("medias")
        if items is None and not more:
            items = []
        if type(more) is not bool:
            raise RuntimeError(_BAD_RESPONSE)
        return items, total, more, meta.get("title")

    if link.kind == "series":
        data = fetch("/x/series/archives", {"mid": link.owner, "series_id": link.identifier,
                     "pn": page, "ps": _PAGE_SIZE, "only_normal": "true", "sort": "asc"})
        page_field, size_field, title = "num", "size", ""
    else:
        data = fetch("/x/polymer/web-space/seasons_archives_list", {"mid": link.owner, "season_id": link.identifier,
                     "page_num": page, "page_size": _PAGE_SIZE, "sort_reverse": "false"})
        page_field, size_field = "page_num", "page_size"
        meta = _collection_meta(link, data, "meta", "season_id")
        title = meta.get("title") or meta.get("name")
    pagination = data.get("page")
    if not isinstance(pagination, dict) or pagination.get(page_field) != page:
        raise RuntimeError(_BAD_RESPONSE)
    total, size = pagination.get("total"), pagination.get(size_field, _PAGE_SIZE)
    if type(total) is not int or total < 0 or type(size) is not int or not 1 <= size <= _PAGE_SIZE:
        raise RuntimeError(_BAD_RESPONSE)
    items = data.get("archives")
    return [] if items is None and total == 0 else items, total, page * size < total, title


def _title(value, fallback=""):
    return (" ".join(value.split())[:220] or fallback) if isinstance(value, str) else fallback


def _video_id(item, favorite):
    if not isinstance(item, dict) or (favorite and item.get("type") != 2):
        return ""
    attr = item.get("attr")
    if ((favorite and type(attr) is int and attr & 1) or item.get("state", 0) != 0
            or item.get("ugc_pay") or item.get("is_lesson_video") or item.get("interactive_video")):
        return ""
    identifier = item.get("bvid") or item.get("bv_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", identifier):
        aid = item.get("aid") if not favorite else item.get("id")
        identifier = f"av{aid}" if type(aid) is int else ""
    try:
        return parse_video_link(identifier)[0]
    except ValueError:
        return ""


def _fetcher(session, referer, stop):
    deadline = time.monotonic() + 90

    def fetch(endpoint, params):
        if stop is not None and stop.is_set():
            raise Cancelled("已取消读取 B 站列表。")
        if time.monotonic() >= deadline:
            raise RuntimeError("读取 B 站列表超时，请稍后重试。")
        return _request(endpoint, params, referer, session)

    return fetch


def _identifier(value):
    return type(value) is int and 0 < value < 10**19


def verified_favorites_uid(session, account):
    """Return the verified account ID eligible to read a private scope."""
    if not isinstance(session, BiliSession) or not isinstance(account, BiliAccount) or not _identifier(account.uid):
        raise BiliCollectionError("请先扫码验证当前 B 站账号。", auth_expired=True)
    credentials = dict(session.cookies)
    if not credentials.get("SESSDATA"):
        raise BiliCollectionError("请先扫码验证当前 B 站账号。", auth_expired=True)
    cookie_uid = credentials.get("DedeUserID")
    if cookie_uid is not None and cookie_uid != str(account.uid):
        raise BiliCollectionError("B 站会话与当前账号不符，请重新扫码。", auth_expired=True, mismatched=True)
    return account.uid


def _folder_row(item, uid, kind):
    if not isinstance(item, dict):
        raise RuntimeError(_BAD_RESPONSE)
    identifier, owner, total = (item.get(key) for key in ("id", "mid", "media_count"))
    if not _identifier(identifier) or not _identifier(owner) or type(total) is not int or total < 0:
        raise RuntimeError(_BAD_RESPONSE)
    if kind == "favorite" and owner != uid:
        raise BiliCollectionError("B 站返回了其他账号的收藏夹。", mismatched=True)
    url = (f"{_SPACE}/{owner}/lists/{identifier}?type=season" if kind == "season"
           else f"{_SPACE}/{owner}/favlist?fid={identifier}")
    return {"id": f"{kind}:{identifier}", "collection_id": identifier, "kind": kind, "owner_id": owner,
            "title": _title(item.get("title"), f"{_LABELS[kind]} {identifier}"), "total": total, "url": url,
            "_account_id": uid}


def _folder_items(data):
    total, items = data.get("count"), data.get("list")
    if items is None and total == 0:
        items = []
    if type(total) is not int or total < 0 or not isinstance(items, list):
        raise RuntimeError(_BAD_RESPONSE)
    return total, items


def _followed_folders(fetch, uid):
    seen = set()
    scanned = 0
    first_total = None
    for page in count(1):
        data = fetch("/x/v3/fav/folder/collected/list", {"up_mid": uid, "pn": page, "ps": _FAVORITE_PAGE_SIZE,
                                                       "platform": "web"})
        total, items = _folder_items(data)
        more = data.get("has_more")
        if type(more) is not bool or len(items) > _FAVORITE_PAGE_SIZE or (more and not items):
            raise RuntimeError(_BAD_RESPONSE)
        if first_total is None:
            first_total = total
        if total != first_total:
            raise RuntimeError("已收藏合集在读取时发生变化，请刷新。")
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(_BAD_RESPONSE)
            identity = item.get("type"), item.get("id")
            if not all(_identifier(value) for value in identity):
                raise RuntimeError(_BAD_RESPONSE)
            if identity in seen:
                raise RuntimeError("B 站返回了重复的已收藏合集。")
            seen.add(identity)
            attr = item.get("attr")
            kind = _FOLLOWED_KINDS.get(identity[0])
            if kind and item.get("state", 0) == 0 and not (type(attr) is int and attr & 1):
                yield _folder_row(item, uid, kind)
        scanned += len(items)
        if not more:
            if scanned != total:
                raise RuntimeError("B 站只返回了部分已收藏合集。")
            return


def _must_raise(error):
    return isinstance(error, Cancelled) or (isinstance(error, BiliCollectionError)
                                          and (error.auth_expired or error.denied or error.mismatched))


def list_my_favorites(session, account, *, stop=None):
    """List the verified account's own folders, followed folders, and followed seasons."""
    uid = verified_favorites_uid(session, account)
    fetch = _fetcher(session, f"{_SPACE}/{uid}/favlist", stop)
    data = fetch("/x/v3/fav/folder/created/list-all", {"up_mid": uid})
    total, items = _folder_items(data)
    rows = [_folder_row(item, uid, "favorite") for item in items]
    if len({row["id"] for row in rows}) != len(rows):
        raise RuntimeError("B 站返回了重复的收藏夹。")
    warnings = [] if len(rows) == total else ["B 站只返回了部分收藏夹。"]
    try:
        rows.extend(_followed_folders(fetch, uid))
    except RuntimeError as error:
        if _must_raise(error):
            raise
        warnings.append(f"未能读取全部已收藏合集。{error}")
    if stop is not None and stop.is_set():
        raise Cancelled("已取消读取 B 站列表。")
    return {"source": SOURCE, "view": "favorites", "title": "我的收藏夹", "_account_id": uid,
            "rows": rows, "total": len(rows), "complete": not warnings, "warning": " · ".join(warnings)}


def _personal_link(folder, uid):
    try:
        link = _parse(folder.get("url")) if isinstance(folder, dict) else None
    except ValueError:
        link = None
    kind = folder.get("kind") if isinstance(folder, dict) else None
    link_kind = "favorite" if kind == "followed-favorite" else kind
    if (link is None or link.kind not in ("favorite", "season") or link.kind != link_kind
            or not _identifier(link.owner) or (kind == "favorite" and link.owner != uid)
            or not _identifier(folder.get("_account_id"))
            or folder["_account_id"] != uid or not _identifier(folder.get("owner_id"))
            or not _identifier(folder.get("collection_id"))
            or folder.get("owner_id") != link.owner or folder.get("collection_id") != link.identifier
            or folder.get("id") != f"{kind}:{link.identifier}"):
        raise BiliCollectionError("请选择当前账号收藏夹中的列表。", mismatched=True)
    return link


def resolve_my_favorite(session, account, folder, *, stop=None):
    """Read a selected folder using only the verified account's session."""
    uid = verified_favorites_uid(session, account)
    link = _personal_link(folder, uid)
    result = _resolve_collection(link, session, stop, personal=True)
    return dict(result, _account_id=uid, folder_id=folder["id"])


def resolve_collection(value, session=None, stop=None):
    """Read up to 500 pasted list entries for the shared queue."""
    link = _parse(value)
    try:
        return _resolve_collection(link, session, stop)
    except BiliCollectionError as error:
        if session is None or not error.auth_expired:
            raise
        try:
            return _resolve_collection(link, None, stop)
        except BiliCollectionError as guest_error:
            if guest_error.denied:
                raise error from None
            raise


def _source_positions(items):
    for item in items:
        if isinstance(item, dict):
            identifier = item.get("id") or item.get("aid")
            item_type = item.get("type")
            if _identifier(identifier):
                yield type(item_type) is int and item_type, identifier
            elif video_id := item.get("bvid") or item.get("bv_id"):
                try:
                    yield "video", parse_video_link(video_id)[0]
                except ValueError:
                    pass


def _queue_row(item, favorite, position, batch_key, batch_title):
    video_id = _video_id(item, favorite)
    if not video_id:
        return None
    owner = item.get("upper") or item.get("owner")
    duration = item.get("duration")
    video_title = _title(item.get("title"), video_id)
    partition = _title(item.get("tname")) or _title(item.get("tname_v2"))
    # List titles describe the whole video. Resolve its actual part before tagging the download.
    preview = bili_catalog.classify_title(video_title, partition)
    return {
        "source": SOURCE, "id": video_id, "url": f"{VIDEO_BASE}{video_id}?p=1", "title": video_title,
        "artist": "", "uploader": _title(owner.get("name")) if isinstance(owner, dict) else "",
        "preview_audio_title": preview["audio_title"], "preview_artist": preview["artist"],
        "preview_media_category": preview["media_category"], "preview_source_category": partition,
        "position": position, "duration": duration if type(duration) is int and duration >= 0 else 0,
        "batch_key": batch_key, "batch_title": batch_title,
    }


def _resolve_collection(link, session, stop, *, personal=False):
    fetch = _fetcher(session, link.url, stop)

    title = ""
    if link.kind == "series":
        meta = _collection_meta(link, fetch("/x/series/series", {"series_id": link.identifier}), "meta", "series_id")
        title = _title(meta.get("name"))

    rows, warnings = [], []
    seen_pages, seen_positions = set(), set()
    offset = 0
    total = None
    batch_key = f"bilibili:{link.kind}:{link.identifier}"
    favorite = link.kind == "favorite"
    page_size = _FAVORITE_PAGE_SIZE if favorite else _PAGE_SIZE
    for page in count(1):
        try:
            items, page_total, more, page_title = _list_page(link, page, fetch)
            if (not isinstance(items, list) or type(page_total) is not int or page_total < 0
                    or len(items) > page_size):
                raise RuntimeError(_BAD_RESPONSE)
            if not items and more:
                raise RuntimeError("B 站未返回列表的下一页。")
            if items:
                fingerprint = sha256(json.dumps(items, sort_keys=True).encode("ascii")).digest()
                if fingerprint in seen_pages:
                    raise RuntimeError("B 站返回了重复的列表分页。")
                seen_pages.add(fingerprint)
            if personal:
                if total is not None and total != page_total:
                    raise RuntimeError("收藏夹在读取时发生变化，请刷新。")
                positions = tuple(_source_positions(items))
                missing_type = favorite and any(type(item.get("type")) is not int
                                                for item in items if isinstance(item, dict))
                if len(positions) != len(items) or missing_type:
                    raise RuntimeError("B 站未返回完整的视频信息，请刷新。")
                unique = set(positions)
                if len(unique) != len(positions) or not unique.isdisjoint(seen_positions):
                    raise RuntimeError("B 站重复返回了列表中的内容。")
                seen_positions.update(unique)
        except RuntimeError as error:
            if not personal or not offset or _must_raise(error):
                raise
            warnings.append(f"只读取到部分列表。{error}")
            break
        total = page_total
        title = title or _title(page_title)
        batch_title = title or f"{_LABELS[link.kind]} {link.identifier}"
        if not personal:
            items = items[:_MAX_ITEMS - offset]
        for position, item in enumerate(items, offset + 1):
            if row := _queue_row(item, favorite, position, batch_key, batch_title):
                rows.append(row)
        offset += len(items)
        if not more or (not personal and offset >= _MAX_ITEMS):
            break
    if stop is not None and stop.is_set():
        raise Cancelled("已取消读取 B 站列表。")
    if not rows and not personal:
        raise RuntimeError("B 站列表中没有可加入队列的视频。")
    effective_total = max(offset, total)
    for row in rows:
        row["total"] = effective_total
    result = {"id": str(link.identifier), "title": title or f"{_LABELS[link.kind]} {link.identifier}",
              "rows": rows, "collection_kind": link.kind, "url": link.url}
    skipped = offset - len(rows)
    if personal:
        complete = not warnings and offset == total
        if not complete and not warnings:
            warnings.append("B 站只返回了部分列表。")
        result.update(source=SOURCE, complete=complete, scanned=offset, skipped=skipped, total=effective_total)
    elif total > _MAX_ITEMS or more:
        warnings.append(f"仅显示前 {_MAX_ITEMS} 项")
    if skipped:
        warnings.append(f"已跳过 {skipped} 项不可用或不支持的内容")
    if warnings:
        result["warning"] = " · ".join(warnings)
    return result
