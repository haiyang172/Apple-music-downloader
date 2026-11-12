"""Read-only browsing of an Apple Music user's cloud library."""

import re
from urllib.parse import urlsplit

from music_catalog import CatalogClient, CatalogError, catalog_row


LIBRARY_TYPES = {
    "song": "library-songs",
    "album": "library-albums",
    "playlist": "library-playlists",
}
LIBRARY_BASE = "/v1/me/library"
LIBRARY_ID = re.compile(r"[A-Za-z0-9_.-]{1,200}")
CATALOG_SONG_ID = re.compile(r"[0-9]{1,25}")
APPLE_API_HOSTS = {"api.music.apple.com", "amp-api.music.apple.com"}


class LibrarySessionRequired(RuntimeError):
    """The Apple Music web session needs to be connected or refreshed."""


def _string(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _data(value):
    items = value.get("data") if isinstance(value, dict) else None
    return items if isinstance(items, list) else []


def _artwork(attributes):
    artwork = attributes.get("artwork")
    if not isinstance(artwork, dict) or not isinstance(artwork.get("url"), str):
        return ""
    value = artwork["url"].replace("{w}", "48").replace("{h}", "48").replace("{f}", "png")
    value = re.sub(r"\.(?:jpe?g|webp)(?=\?|$)", ".png", value, flags=re.IGNORECASE)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme == "https" and host.endswith(".mzstatic.com") and parsed.port is None
                and not parsed.username and not parsed.password):
            return value
    except ValueError:
        pass
    return ""


def _library_row(resource, storefront):
    if not isinstance(resource, dict):
        return None
    identifier = _string(resource.get("id"))
    if not LIBRARY_ID.fullmatch(identifier) or identifier in (".", ".."):
        return None
    resource_type = resource.get("type")
    if resource_type == "songs":
        return catalog_row(resource, storefront)
    kinds = {resource_name: kind for kind, resource_name in LIBRARY_TYPES.items()}
    kind = kinds.get(resource_type)
    attributes = resource.get("attributes")
    if not kind or not isinstance(attributes, dict):
        return None
    title = _string(attributes.get("name"))
    if not title:
        return None

    relationships = resource.get("relationships")
    associated = _data(relationships.get("catalog")) if isinstance(relationships, dict) else []
    matched = next((row for row in associated if isinstance(row, dict)), None)
    catalog = catalog_row(matched, storefront) or {} if matched else {}

    artist = _string(attributes.get("artistName") or attributes.get("curatorName"))
    duration_ms = attributes.get("durationInMillis")
    duration = duration_ms / 1000 if type(duration_ms) is int and duration_ms > 0 else 0
    url, catalog_id = "", ""
    if kind == "song":
        play_params = attributes.get("playParams")
        if not isinstance(play_params, dict):
            play_params = {}
        identifier_hint = catalog.get("id") or (matched or {}).get("id") or play_params.get("catalogId")
        if isinstance(identifier_hint, str) and CATALOG_SONG_ID.fullmatch(identifier_hint):
            catalog_id = identifier_hint
            url = catalog.get("url") or f"https://music.apple.com/{storefront}/song/{catalog_id}"
    # Expand a personal playlist or album to keep its actual library track selection.
    return {
        "id": catalog_id or identifier,
        "type": kind,
        "title": title,
        "artist": artist or catalog.get("artist", ""),
        "album": _string(attributes.get("albumName")) or catalog.get("album", ""),
        "duration": duration or catalog.get("duration", 0),
        "url": url,
        "artwork_url": _artwork(attributes) or catalog.get("artwork_url", ""),
        "preview_url": catalog.get("preview_url", ""),
        "storefront": storefront,
        "library_id": identifier,
        "expandable": kind in ("album", "playlist"),
    }


def _cursor_path(value, endpoint):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("资料库分页地址无效。")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("资料库分页地址无效。") from None
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc.lower() not in APPLE_API_HOSTS:
            raise ValueError("资料库分页地址无效。")
    if parsed.path != endpoint or parsed.fragment or not parsed.query:
        raise ValueError("资料库分页地址无效。")
    return parsed.path + "?" + parsed.query


class LibraryClient:
    """Read one page of the library at a time with an existing Music User Token."""

    def __init__(self, user_token, storefront="au", catalog=None):
        if not isinstance(user_token, str) or not re.fullmatch(r"[\x21-\x7e]{50,8192}", user_token):
            raise LibrarySessionRequired("请在账户页连接 Apple Music 资料库。")
        if not isinstance(storefront, str) or not re.fullmatch(r"[a-z]{2}", storefront.lower()):
            raise ValueError("Apple Music 地区应为两位字母，例如 au 或 cn。")
        self._user_token = user_token
        self.storefront = storefront.lower()
        self.catalog = catalog if catalog is not None else CatalogClient()

    def _request(self, path, params=None):
        try:
            return self.catalog.request(path, params=params, user_token=self._user_token)
        except CatalogError as error:
            if error.status in (401, 403):
                raise LibrarySessionRequired("Apple Music 资料库会话已失效，请重新连接。") from None
            raise

    def _page(self, endpoint, cursor, limit):
        if cursor:
            response = self._request(_cursor_path(cursor, endpoint))
        else:
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("资料库每页应请求 1 到 100 条。")
            response = self._request(endpoint, {"limit": limit, "include": "catalog"})
        if not isinstance(response, dict) or not isinstance(response.get("data"), list):
            raise RuntimeError("Apple Music 返回的资料库数据无效。")
        rows = [row for item in _data(response) if (row := _library_row(item, self.storefront))]
        next_page = response.get("next")
        return rows, _cursor_path(next_page, endpoint) if next_page else None

    def items(self, kind="song", cursor=None, *, limit=50):
        if kind not in LIBRARY_TYPES:
            raise ValueError("请选择资料库中的歌曲、专辑或歌单。")
        return self._page(f"{LIBRARY_BASE}/{LIBRARY_TYPES[kind][8:]}", cursor, limit)

    def tracks(self, collection, cursor=None, *, limit=100):
        if not isinstance(collection, dict):
            raise ValueError("请选择资料库中的专辑或歌单。")
        kind = collection.get("type")
        identifier = collection.get("library_id")
        if (kind not in ("album", "playlist") or not isinstance(identifier, str)
                or not LIBRARY_ID.fullmatch(identifier) or identifier in (".", "..")):
            raise ValueError("请选择资料库中的专辑或歌单。")
        endpoint = f"{LIBRARY_BASE}/{LIBRARY_TYPES[kind][8:]}/{identifier}/tracks"
        return self._page(endpoint, cursor, limit)
