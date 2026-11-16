"""Resolve public or account-accessible Bilibili lists into video queue rows."""

from dataclasses import dataclass
from hashlib import sha256
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bili_catalog import SOURCE, VIDEO_BASE, parse_video_link
from engine import Cancelled


_SPACE = "https://space.bilibili.com"
_API = "https://api.bilibili.com"
_LABELS = {"season": "合集", "series": "系列", "favorite": "收藏夹"}
_BAD_LINK = "请输入 B 站合集、系列或带编号的收藏夹链接。"
_BAD_RESPONSE = "B 站返回的列表信息无效。"
_DENIED = "无法访问该 B 站列表，请在浏览器中确认内容和账号权限。"
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_ITEMS = 500
_PAGE_SIZE = 30
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
        if error.code in (401, 403, 404, 410):
            raise RuntimeError(_DENIED) from None
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
    if payload["code"] in (-412, -429, -352):
        raise RuntimeError("B 站暂时拒绝请求，请稍后重试。")
    if payload["code"] != 0:
        raise RuntimeError(_DENIED)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(_BAD_RESPONSE)
    return data


def _collection_meta(link, data, key, id_key):
    meta = data.get(key)
    if (not isinstance(meta, dict) or meta.get(id_key) != link.identifier
            or (link.owner is not None and meta.get("mid") != link.owner)):
        raise RuntimeError("B 站返回的列表与请求链接不符。")
    return meta


def _list_page(link, page, fetch):
    if link.kind == "favorite":
        data = fetch("/x/v3/fav/resource/list", {"media_id": link.identifier, "pn": page, "ps": 20,
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


def resolve_collection(value, session=None, stop=None):
    """Read up to 500 list entries, queuing the first part of each accessible video."""
    link = _parse(value)
    deadline = time.monotonic() + 90

    def fetch(endpoint, params):
        if stop is not None and stop.is_set():
            raise Cancelled("已取消读取 B 站列表。")
        if time.monotonic() >= deadline:
            raise RuntimeError("读取 B 站列表超时，请稍后重试。")
        return _request(endpoint, params, link.url, session)

    title = ""
    if link.kind == "series":
        meta = _collection_meta(link, fetch("/x/series/series", {"series_id": link.identifier}), "meta", "series_id")
        title = _title(meta.get("name"))

    rows, seen_pages = [], set()
    offset = 0
    batch_key = f"bilibili:{link.kind}:{link.identifier}"
    for page in range(1, _MAX_ITEMS + 1):
        items, total, more, page_title = _list_page(link, page, fetch)
        if not isinstance(items, list) or type(total) is not int or total < 0 or len(items) > _PAGE_SIZE:
            raise RuntimeError(_BAD_RESPONSE)
        if not items:
            if more:
                raise RuntimeError("B 站未返回列表的下一页。")
            break
        # Refuse repeated page data instead of multiplying entries until the limit.
        fingerprint = sha256(json.dumps(items, sort_keys=True).encode("ascii")).digest()
        if fingerprint in seen_pages:
            raise RuntimeError("B 站返回了重复的列表分页。")
        seen_pages.add(fingerprint)
        title = title or _title(page_title)
        batch_title = title or f"{_LABELS[link.kind]} {link.identifier}"
        for position, item in enumerate(items[:_MAX_ITEMS - offset], offset + 1):
            video_id = _video_id(item, link.kind == "favorite")
            if not video_id:
                continue
            owner = item.get("upper") or item.get("owner")
            duration = item.get("duration")
            rows.append({
                "source": SOURCE, "id": video_id, "url": f"{VIDEO_BASE}{video_id}?p=1",
                "title": _title(item.get("title"), video_id), "artist": "",
                "uploader": _title(owner.get("name")) if isinstance(owner, dict) else "",
                "position": position, "duration": duration if type(duration) is int and duration >= 0 else 0,
                "batch_key": batch_key, "batch_title": batch_title,
            })
        offset = min(_MAX_ITEMS, offset + len(items))
        if not more or offset >= _MAX_ITEMS:
            break
    if stop is not None and stop.is_set():
        raise Cancelled("已取消读取 B 站列表。")
    if not rows:
        raise RuntimeError("B 站列表中没有可加入队列的视频。")
    for row in rows:
        row["total"] = max(offset, total)
    result = {"id": str(link.identifier), "title": title or f"{_LABELS[link.kind]} {link.identifier}",
              "rows": rows, "collection_kind": link.kind, "url": link.url}
    warnings = []
    if total > _MAX_ITEMS or more:
        warnings.append(f"仅显示前 {_MAX_ITEMS} 项")
    if skipped := offset - len(rows):
        warnings.append(f"已跳过 {skipped} 项不可用或不支持的内容")
    if warnings:
        result["warning"] = " · ".join(warnings)
    return result
