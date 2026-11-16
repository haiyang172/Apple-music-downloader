"""NetEase Music QR login and Windows-protected session storage."""

from dataclasses import dataclass, field
from http.client import HTTPException
from http.cookiejar import CookieJar, DefaultCookiePolicy
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
from secrets import randbelow
from tempfile import NamedTemporaryFile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from settings import _dpapi


_SITE = "https://music.163.com"
_UNIQUE_KEY = "/api/login/qrcode/unikey"
_POLL = "/api/login/qrcode/client/login"
_ACCOUNT = "/api/nuser/account/get"
_USER_PLAYLISTS = "/api/user/playlist"
_SONG_SEARCH = "/api/cloudsearch/pc"
_PUBLIC_PLAYLIST_ROUTES = frozenset({"/api/v6/playlist/detail", "/api/v3/song/detail"})
_AUTH_ROUTES = frozenset({_UNIQUE_KEY, _POLL})
_API_ROUTES = _PUBLIC_PLAYLIST_ROUTES | {_ACCOUNT, _USER_PLAYLISTS, _SONG_SEARCH, "/api/playlist/detail",
                                         "/api/song/detail", "/api/song/enhance/player/url"}
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
_CHAIN = re.compile(r"v1_unknown-[0-9]{1,6}_web_login_[0-9]{13,16}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}")
_AUTH_COOKIES = ("MUSIC_U", "__csrf")
_MAX_RESPONSE = 4 * 1024 * 1024
_MAX_SESSION = 32_768


class NeteaseAuthError(RuntimeError):
    """NetEase Music or Windows could not complete an account operation."""


@dataclass(frozen=True)
class NeteaseQR:
    url: str = field(repr=False)
    _key: str = field(repr=False)
    _chain: str = field(repr=False)
    _jar: CookieJar = field(repr=False, compare=False)


@dataclass(frozen=True)
class NeteaseSession:
    user_id: int = field(repr=False)
    _cookies: tuple[tuple[str, str], ...] = field(repr=False)


@dataclass(frozen=True)
class NeteaseAccount:
    uid: int = field(repr=False)
    name: str


@dataclass(frozen=True)
class NeteasePoll:
    state: str
    session: NeteaseSession | None = field(default=None, repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _cookies(values):
    if not isinstance(values, dict) or not values.get("MUSIC_U"):
        raise NeteaseAuthError("网易云音乐未返回有效的登录会话，请重新扫码。")
    result = []
    for name in _AUTH_COOKIES:
        if name in values:
            value = values[name]
            if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
                raise NeteaseAuthError("网易云音乐返回了无效的登录会话，请重新扫码。")
            result.append((name, value))
    return tuple(result)


def _session(uid, values):
    if type(uid) is not int or not 0 < uid < 10 ** 20:
        raise NeteaseAuthError("无法确认当前网易云音乐账号。")
    return NeteaseSession(uid, _cookies(values))


def _request(path, params=None, *, chain=None, jar=None, cookies=()):
    if path not in _API_ROUTES and path not in _AUTH_ROUTES:
        raise ValueError("无效的网易云音乐接口。")
    headers = {"User-Agent": _USER_AGENT, "Referer": _SITE + "/", "Origin": _SITE,
               "Accept": "application/json", "Accept-Encoding": "identity"}
    url = _SITE + path
    query = urlencode(params or {}).encode("ascii")
    data = None
    if path in _AUTH_ROUTES:
        if jar is None or not isinstance(chain, str) or not _CHAIN.fullmatch(chain) or cookies:
            raise ValueError("无效的网易云音乐扫码会话。")
        headers.update({"Content-Type": "application/x-www-form-urlencoded", "x-loginmethod": "QrCode",
                        "x-login-chain-id": chain, "x-os": "web", "nm-gcore-status": "1"})
        data = query
    else:
        if jar is not None or chain is not None:
            raise ValueError("无效的网易云音乐账号请求。")
        if query:
            url += "?" + query.decode("ascii")
        if cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies)

    request = Request(url, data=data, headers=headers)
    if jar is not None:
        jar.add_cookie_header(request)
    deadline = time.monotonic() + 18
    limit = 131_072 if path in _AUTH_ROUTES else _MAX_RESPONSE
    raw = bytearray()
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8) as response:
            if jar is not None:
                jar.extract_cookies(response, request)
            while len(raw) <= limit:
                if time.monotonic() > deadline:
                    raise NeteaseAuthError("网易云音乐连接超时，请重试。")
                chunk = response.read1(min(65_536, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
    except HTTPError as error:
        error.close()
        raise NeteaseAuthError("网易云音乐暂未接受请求，请稍后重试。") from None
    except (OSError, HTTPException):
        raise NeteaseAuthError("无法连接网易云音乐，请检查网络后重试。") from None
    if len(raw) > limit:
        raise NeteaseAuthError("网易云音乐响应过大，请重试。")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise NeteaseAuthError("网易云音乐返回了无法读取的数据。") from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise NeteaseAuthError("网易云音乐返回了无效的数据。")
    return payload


def netease_api_request(session, path, params=None):
    """Send an authenticated request to a fixed NetEase Music endpoint."""
    if not isinstance(session, NeteaseSession):
        raise NeteaseAuthError("请先登录网易云音乐。")
    if path not in _API_ROUTES:
        raise ValueError("无效的网易云音乐接口。")
    session = _session(session.user_id, dict(session._cookies))
    if path == _USER_PLAYLISTS:
        params = {**(params or {}), "uid": session.user_id}
    return _request(path, params, cookies=session._cookies)


def netease_search_request(query, offset, limit, *, session=None):
    """Read public songs on the fixed NetEase Music search route."""
    params = {"s": query, "type": 1, "offset": offset, "limit": limit, "total": "true"}
    if session is None:
        return _request(_SONG_SEARCH, params)
    return netease_api_request(session, _SONG_SEARCH, params)


def netease_public_playlist_request(path, params):
    """Read public playlist metadata or its track records without a session."""
    if path not in _PUBLIC_PLAYLIST_ROUTES:
        raise ValueError("无效的网易云音乐公开歌单接口。")
    return _request(path, params)


def _account(cookies):
    payload = _request(_ACCOUNT, cookies=cookies)
    profile, code = payload.get("profile"), payload.get("code")
    if code in (301, 401) or (code == 200 and profile is None):
        return None
    if code != 200 or not isinstance(profile, dict):
        raise NeteaseAuthError("网易云音乐无法确认登录状态，请稍后重试。")
    uid = profile.get("userId")
    if type(uid) is not int or not 0 < uid < 10 ** 20:
        raise NeteaseAuthError("无法确认当前网易云音乐账号。")
    name = profile.get("nickname")
    name = (" ".join(name.replace("\0", "").split()).encode("utf-8", "ignore").decode("utf-8")[:80]
            if isinstance(name, str) else "")
    return NeteaseAccount(uid, name or "网易云音乐用户")


def _authenticated_cookies(jar):
    request = Request(_SITE + _ACCOUNT)
    jar.add_cookie_header(request)
    parsed = SimpleCookie()
    try:
        parsed.load(request.get_header("Cookie", ""))
    except CookieError:
        raise NeteaseAuthError("网易云音乐返回了无效的登录会话，请重新扫码。") from None
    return _cookies({name: parsed[name].value for name in _AUTH_COOKIES if name in parsed})


class NeteaseAuth:
    """Open a first-party QR login and keep the resulting session on this Windows account."""

    def __init__(self, path):
        self.path = Path(path)

    def begin(self):
        # QR codes and state codes follow MusicUnlock v2.3.0 / the NetEase web client.
        chain = f"v1_unknown-{randbelow(1_000_000)}_web_login_{int(time.time() * 1000)}"
        jar = CookieJar(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
        payload = _request(_UNIQUE_KEY, {"type": 3}, chain=chain, jar=jar)
        key = payload.get("unikey")
        if payload.get("code") != 200 or not isinstance(key, str) or not _KEY.fullmatch(key):
            raise NeteaseAuthError("网易云音乐未返回有效的二维码，请稍后重试。")
        return NeteaseQR(_SITE + "/login?" + urlencode({"codekey": key}), key, chain, jar)

    def poll(self, qr):
        if not isinstance(qr, NeteaseQR) or not _KEY.fullmatch(qr._key):
            raise ValueError("请先获取有效的网易云音乐登录二维码。")
        payload = _request(_POLL, {"type": 3, "key": qr._key}, chain=qr._chain, jar=qr._jar)
        code = payload.get("code")
        state = {800: "expired", 801: "waiting", 802: "scanned"}.get(code)
        if state:
            return NeteasePoll(state)
        if code == 803:
            cookies = _authenticated_cookies(qr._jar)
            account = _account(cookies)
            if account is None:
                raise NeteaseAuthError("网易云音乐未确认登录，请重新扫码。")
            return NeteasePoll("success", NeteaseSession(account.uid, cookies))
        if code in (8821, 460, -460, -12):
            raise NeteaseAuthError("网易云音乐要求完成账号安全验证，请在官方应用确认后重新扫码。")
        raise NeteaseAuthError("网易云音乐返回了无效的扫码状态，请重新获取二维码。")

    def check(self, session):
        if not isinstance(session, NeteaseSession):
            return None
        session = _session(session.user_id, dict(session._cookies))
        account = _account(session._cookies)
        if account is not None and account.uid != session.user_id:
            raise NeteaseAuthError("网易云音乐账号与保存的会话不一致，请重新登录。")
        return account

    def load(self):
        try:
            with self.path.open("rb") as stream:
                protected = stream.read(_MAX_SESSION + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise NeteaseAuthError("无法打开已保存的网易云音乐会话，请重新登录。") from None
        try:
            if not 0 < len(protected) <= _MAX_SESSION:
                raise ValueError()
            values = json.loads(_dpapi(protected, decrypt=True))
            if not isinstance(values, dict) or values.get("version") != 1:
                raise ValueError()
            return _session(values.get("uid"), values.get("cookies"))
        except (OSError, ValueError, NeteaseAuthError):
            raise NeteaseAuthError("Windows 无法读取已保存的网易云音乐会话，请重新登录。") from None

    def save(self, session):
        session = _session(session.user_id, dict(session._cookies))
        temporary = None
        try:
            data = {"version": 1, "uid": session.user_id, "cookies": dict(session._cookies)}
            protected = _dpapi(json.dumps(data).encode("utf-8"))
            if not 0 < len(protected) <= _MAX_SESSION:
                raise ValueError()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, prefix="netease-session-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(protected)
            temporary.replace(self.path)
            temporary = None
        except (OSError, ValueError):
            raise NeteaseAuthError("Windows 无法保存网易云音乐会话。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise NeteaseAuthError("无法删除已保存的网易云音乐会话。") from None
