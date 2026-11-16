"""Expand first-party QQ Music and NetEase Music share links."""

from http.client import HTTPException
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from engine import Cancelled


_QQ_HOSTS = {"c.y.qq.com", "c6.y.qq.com"}
_QQ_PATH = "/base/fcgi-bin/u"
_QQ_CODE = re.compile(r"[A-Za-z0-9_-]{6,128}")
_NETEASE_HOST = "163cn.tv"
_NETEASE_PATH = re.compile(r"/[A-Za-z0-9]{5,64}/?")
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_MAX_HOPS = 3
_INVALID = "分享链接没有指向同一音乐平台的单曲或歌单。"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _safe_text(value):
    return (isinstance(value, str) and 0 < len(value) <= 4096
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value))


def _short_link(value):
    if not isinstance(value, str) or not _safe_text(value.strip()):
        return None
    try:
        url = urlsplit(value.strip())
        if url.scheme not in ("https", "http") or url.fragment:
            return None
        host = url.netloc.lower()
        if host in _QQ_HOSTS and url.path == _QQ_PATH:
            keys = parse_qs(url.query, keep_blank_values=True, max_num_fields=24).get("__", [])
            if len(keys) == 1 and _QQ_CODE.fullmatch(keys[0]):
                return "qq", f"https://{host}{_QQ_PATH}?{urlencode({'__': keys[0]})}"
        if host == _NETEASE_HOST and _NETEASE_PATH.fullmatch(url.path):
            return "netease", f"https://{_NETEASE_HOST}{url.path.rstrip('/')}"
    except ValueError:
        pass
    return None


def short_music_source(value):
    """Recognize an official short share link without making network requests."""
    parsed = _short_link(value)
    return parsed[0] if parsed else None


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise Cancelled("已取消读取分享链接。")


def _location(opener, url, stop):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    _check_stop(stop)
    try:
        with opener.open(request, timeout=7):
            pass
    except HTTPError as response:
        with response:
            _check_stop(stop)
            if response.code in _REDIRECT_CODES:
                locations = response.headers.get_all("Location", [])
                if len(locations) == 1 and _safe_text(locations[0]):
                    return locations[0]
    except (OSError, HTTPException):
        _check_stop(stop)
        raise RuntimeError("无法展开音乐分享链接，请检查网络后重试。") from None
    _check_stop(stop)
    raise RuntimeError(_INVALID)


def expand_short_music_link(value, *, stop=None):
    """Return a recognized share link's canonical song or playlist URL."""
    short = _short_link(value)
    if short is None:
        return value
    # Resolve canonical long links only after these modules have initialized.
    from shared_music import _parse_link

    source, url = short
    seen = set()
    opener = build_opener(_NoRedirect())
    for _ in range(_MAX_HOPS):
        seen.add(url)
        location = _location(opener, url, stop)
        try:
            target = urlsplit(urljoin(url, location))
            if target.scheme == "http":
                target = target._replace(scheme="https")
            target = target.geturl()
        except ValueError:
            raise RuntimeError(_INVALID) from None
        try:
            provider, _, _, canonical = _parse_link(target)
        except ValueError:
            next_short = _short_link(target)
            if next_short is None or next_short[0] != source or next_short[1] in seen:
                raise RuntimeError(_INVALID) from None
            url = next_short[1]
        else:
            if provider != source:
                raise RuntimeError(_INVALID)
            _check_stop(stop)
            return canonical
    raise RuntimeError(_INVALID)
