"""Read Apple Music catalogue metadata for the amdl music browser."""

import json
import math
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_WEB = "https://music.apple.com"
_API = "https://api.music.apple.com"
_WEB_ENTRY = _WEB + "/us/new"
_KINDS = {"song": "songs", "album": "albums", "playlist": "playlists"}
_RESOURCE_TYPES = {resource: name for name, resource in _KINDS.items()}
_ENTRY_SCRIPT = re.compile(rb"/assets/index[~-][A-Za-z0-9._~-]+\.js")
_JWT = re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}")
_RESOURCE_ID = re.compile(r"[A-Za-z0-9._-]{1,180}")
_BASE_HEADERS = {"Origin": _WEB, "User-Agent": "amdl Windows"}
_TOKEN_LOCK = threading.Lock()
_cached_token = ""
_token_expiry = 0.0


class CatalogError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _fetch(url, headers, limit):
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=15) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise CatalogError("Apple Music 的响应超出可读取大小。")
        return body
    except HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            message = "Apple Music 会话无效或已过期。"
        elif status == 404:
            message = "该内容在当前 Apple Music 地区不可用。"
        elif status == 429:
            message = "请求过于频繁，请稍后再试。"
        else:
            message = f"Apple Music 暂时无法响应（{status}）。"
        raise CatalogError(message, status) from None
    except (OSError, URLError):
        raise CatalogError("无法连接 Apple Music，请检查网络后重试。") from None


def _developer_token(refresh=False):
    """Read the public developer bearer used by Apple's web client, in memory only."""
    global _cached_token, _token_expiry
    with _TOKEN_LOCK:
        if _cached_token and not refresh and time.monotonic() < _token_expiry:
            return _cached_token
        page = _fetch(_WEB_ENTRY, _BASE_HEADERS, 9 * 1024 * 1024)
        asset = _ENTRY_SCRIPT.search(page)
        if asset is None:
            raise CatalogError("无法初始化 Apple Music 搜索，请稍后重试。")
        script = _fetch(_WEB + asset[0].decode("ascii"), _BASE_HEADERS, 22 * 1024 * 1024)
        candidate = _JWT.search(script)
        if candidate is None:
            raise CatalogError("无法初始化 Apple Music 搜索，请稍后重试。")
        _cached_token = candidate[0].decode("ascii")
        _token_expiry = time.monotonic() + 30 * 60
        return _cached_token


def _storefront(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2}", value.lower()):
        raise ValueError("Apple Music 地区应使用两位字母代码。")
    return value.lower()


def _types(kind):
    if kind == "all":
        return tuple(_KINDS.values())
    if kind in _KINDS:
        return (_KINDS[kind],)
    if kind in _RESOURCE_TYPES:
        return (kind,)
    raise ValueError("请选择歌曲、专辑或歌单。")


def _number(value, default=0.0):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else default


def _line(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _media_url(url):
    if not isinstance(url, str) or any(char.isspace() or char == "\\" for char in url):
        return ""
    try:
        target = urlsplit(url)
        host = target.hostname or ""
        trusted = host.endswith((".mzstatic.com", ".itunes.apple.com"))
        if target.scheme == "https" and trusted and target.netloc == host:
            return target.geturl()
    except ValueError:
        pass
    return ""


def _artwork_url(artwork):
    if not isinstance(artwork, dict) or not isinstance(artwork.get("url"), str):
        return ""
    url = artwork["url"].replace("{w}", "48").replace("{h}", "48").replace("{f}", "png")
    url = re.sub(r"\.(?:jpe?g|webp)(?=\?|$)", ".png", url, flags=re.IGNORECASE)
    return _media_url(url)


def catalog_row(resource, storefront):
    """Normalize an Apple catalogue resource, keeping links on Apple's hosts."""
    if not isinstance(resource, dict):
        return None
    region = _storefront(storefront)
    kind = _RESOURCE_TYPES.get(resource.get("type"))
    identifier = resource.get("id")
    attributes = resource.get("attributes")
    if not kind or not isinstance(identifier, str) or not _RESOURCE_ID.fullmatch(identifier):
        return None
    if not isinstance(attributes, dict) or not _line(attributes.get("name")):
        return None
    previews = attributes.get("previews")
    preview = previews[0] if isinstance(previews, list) and previews else {}
    url = f"{_WEB}/{region}/{kind}/{identifier}"
    supplied = attributes.get("url")
    if isinstance(supplied, str):
        try:
            parsed = urlsplit(supplied)
            parts = parsed.path.strip("/").split("/")
            if (parsed.scheme == "https" and parsed.netloc == "music.apple.com"
                    and len(parts) >= 3 and parts[:2] == [region, kind] and parts[-1] == identifier):
                url = parsed._replace(query="", fragment="").geturl()
        except ValueError:
            pass
    return {
        "id": identifier,
        "type": kind,
        "title": _line(attributes["name"]),
        "artist": _line(attributes.get("artistName") or attributes.get("curatorName")),
        "album": _line(attributes.get("albumName")),
        "duration": _number(attributes.get("durationInMillis")) / 1000,
        "url": url,
        "artwork_url": _artwork_url(attributes.get("artwork")),
        "preview_url": _media_url(preview.get("url")) if isinstance(preview, dict) else "",
        "storefront": region,
    }


def _rows(resources, region):
    if not isinstance(resources, list):
        return []
    return [row for item in resources if (row := catalog_row(item, region)) is not None]


class CatalogClient:
    """Blocking request interface; call from a background UI worker."""

    def request(self, path, params=None, user_token=None):
        """Read one catalogue or personal-library response from Apple's API."""
        if not isinstance(path, str) or any(ord(char) < 32 or char == "\\" for char in path):
            raise ValueError("Apple Music 请求路径无效。")
        url = urlsplit(path)
        library = url.path.startswith("/v1/me/")
        if url.netloc or url.scheme or url.fragment or not (library or url.path.startswith("/v1/catalog/")):
            raise ValueError("Apple Music 请求路径无效。")
        if library and not (
            isinstance(user_token, str) and re.fullmatch(r"[\x21-\x7e]{50,8192}", user_token)
        ):
            raise CatalogError("请先连接 Apple Music 资料库。", 401)
        if not library and user_token is not None:
            raise ValueError("公开目录查询无需个人会话。")
        if params is not None:
            if url.query:
                raise ValueError("Apple Music 查询参数重复。")
            url = url._replace(query=urlencode(params, doseq=True))
        headers = {**_BASE_HEADERS, "Accept": "application/json"}
        if library:
            headers["Music-User-Token"] = user_token
        for refresh in (False, True):
            headers["Authorization"] = "Bearer " + _developer_token(refresh)
            try:
                body = _fetch(_API + url.geturl(), headers, 8 * 1024 * 1024)
                break
            except CatalogError as error:
                if error.status != 401 or refresh:
                    raise
        try:
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (UnicodeError, ValueError):
            raise CatalogError("Apple Music 返回了无法读取的内容。") from None

    def search(self, term, storefront, kind="song", limit=25):
        region = _storefront(storefront)
        categories = _types(kind)
        term = _line(term)
        if not term:
            raise ValueError("请输入要搜索的歌曲、歌手或专辑。")
        if len(term) > 160:
            raise ValueError("搜索内容请控制在 160 个字符内。")
        payload = self.request(f"/v1/catalog/{region}/search", {
            "term": term, "types": ",".join(categories), "limit": max(1, min(int(limit), 25)),
        })
        results = payload.get("results", {})
        return [row for category in categories for row in _rows(results.get(category, {}).get("data"), region)]

    def charts(self, storefront, kind="song", limit=25):
        region = _storefront(storefront)
        categories = _types(kind)
        payload = self.request(f"/v1/catalog/{region}/charts", {
            "types": ",".join(categories), "limit": max(1, min(int(limit), 25)),
        })
        rows = []
        for category in categories:
            charts = payload.get("results", {}).get(category, [])
            if charts:
                rows.extend(_rows(charts[0].get("data"), region))
        return rows

    def tracks(self, item, storefront):
        region = _storefront(storefront)
        kind = item.get("type")
        identifier = item.get("id")
        if kind not in ("album", "playlist") or not isinstance(identifier, str) or not _RESOURCE_ID.fullmatch(identifier):
            raise ValueError("请选择一张专辑或一份歌单。")
        endpoint = f"/v1/catalog/{region}/{_KINDS[kind]}/{identifier}/tracks"
        page = self.request(endpoint, {"limit": 100})
        rows = _rows(page.get("data"), region)
        seen = set()
        while (link := page.get("next")) and len(seen) < 20:
            if not isinstance(link, str) or urlsplit(link).path != endpoint or link in seen:
                break
            seen.add(link)
            page = self.request(link)
            rows.extend(_rows(page.get("data"), region))
        return rows
