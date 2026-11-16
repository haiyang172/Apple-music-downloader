"""Bilibili QR sign-in and per-user encrypted session storage."""

from dataclasses import dataclass, field
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from settings import _dpapi


_PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
_NAV = "https://api.bilibili.com/x/web-interface/nav"
_SITE = "https://www.bilibili.com"
_SCAN_PATHS = {
    ("account.bilibili.com", "/h5/account-h5/auth/scan-web"),
    ("passport.bilibili.com", "/h5-app/passport/login/scan"),
}
_COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
_KEY = re.compile(r"[A-Za-z0-9_-]{32}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}")
_MAX_SESSION = 32768
_MAX_RESPONSE = 512 * 1024


class BiliAuthError(RuntimeError):
    """The platform or the local credential store could not complete an operation."""


@dataclass(frozen=True)
class QRLogin:
    url: str = field(repr=False)
    key: str = field(repr=False)


@dataclass(frozen=True)
class BiliSession:
    cookies: tuple[tuple[str, str], ...] = field(repr=False)

    def cookie_header(self):
        return "; ".join(f"{name}={value}" for name, value in self.cookies)


@dataclass(frozen=True)
class LoginPoll:
    state: str
    session: BiliSession | None = None


@dataclass(frozen=True)
class BiliAccount:
    uid: int
    name: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _request(url, session=None):
    headers = {"Accept": "application/json", "Origin": _SITE, "Referer": _SITE + "/", "User-Agent": "Mozilla/5.0"}
    if session is not None:
        if url != _NAV:
            raise ValueError("会话只能用于 B 站账号校验。")
        headers["Cookie"] = session.cookie_header()
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=8) as response:
            raw = response.read(_MAX_RESPONSE + 1)
            cookies = response.headers.get_all("Set-Cookie", [])
    except HTTPError as error:
        error.close()
        raise BiliAuthError("B 站登录接口暂时无法访问，请稍后重试。") from None
    except (OSError, HTTPException):
        raise BiliAuthError("无法连接 B 站登录服务，请检查网络后重试。") from None
    if len(raw) > _MAX_RESPONSE:
        raise BiliAuthError("B 站返回的登录响应过大。")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BiliAuthError("无法读取 B 站登录响应。") from None
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise BiliAuthError("无法读取 B 站登录响应。")
    return payload, cookies


def _response_data(payload):
    data = payload.get("data")
    if payload["code"] != 0 or not isinstance(data, dict):
        raise BiliAuthError("B 站未返回有效的登录状态，请稍后重试。")
    return data


def _scan_url(url, key):
    if (not isinstance(url, str) or len(url) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in url)):
        return False
    try:
        target = urlsplit(url)
        query = parse_qs(target.query, keep_blank_values=True, max_num_fields=24)
    except ValueError:
        return False
    return (target.scheme == "https" and (target.netloc.lower(), target.path) in _SCAN_PATHS
            and not target.fragment and query.get("qrcode_key") == [key])


def _session(values):
    if not isinstance(values, dict) or not values.get("SESSDATA"):
        raise ValueError("登录响应缺少会话凭据。")
    cookies = []
    for name in _COOKIE_NAMES:
        if name in values:
            value = values[name]
            if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
                raise ValueError("登录响应包含无效的会话凭据。")
            cookies.append((name, value))
    return BiliSession(tuple(cookies))


def _session_from_headers(headers):
    values = {}
    for header in headers:
        if len(header) > 8192:
            continue
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except CookieError:
            continue
        for name, cookie in parsed.items():
            # Only use credentials explicitly scoped to the API as well as Passport.
            if (name in _COOKIE_NAMES and cookie["domain"].lower().lstrip(".") == "bilibili.com"
                    and cookie["path"] == "/" and cookie.value and cookie["max-age"] != "0"):
                values[name] = cookie.value
    try:
        return _session(values)
    except ValueError:
        raise BiliAuthError("B 站未发出可用的会话凭据，请重新扫码。") from None


class BiliAuth:
    """Use Bilibili's web QR flow without reading the browser or accepting pasted cookies."""

    def __init__(self, path):
        self.path = Path(path)

    def begin(self):
        payload, _ = _request(_PASSPORT + "generate")
        data = _response_data(payload)
        key, url = data.get("qrcode_key"), data.get("url")
        if not isinstance(key, str) or not _KEY.fullmatch(key) or not _scan_url(url, key):
            raise BiliAuthError("B 站返回的二维码资料无效，请重新获取。")
        return QRLogin(url, key)

    def poll(self, qr):
        if not isinstance(qr, QRLogin) or not _KEY.fullmatch(qr.key):
            raise ValueError("请先获取有效的 B 站登录二维码。")
        payload, cookies = _request(_PASSPORT + "poll?" + urlencode({"qrcode_key": qr.key, "source": "main_web"}))
        status = _response_data(payload).get("code")
        if type(status) is int:
            if status == 0:
                return LoginPoll("success", _session_from_headers(cookies))
            state = {86101: "waiting", 86090: "scanned", 86038: "expired"}.get(status)
            if state:
                return LoginPoll(state)
        raise BiliAuthError("无法确认 B 站二维码状态，请重新获取。")

    def check(self, session):
        payload, _ = _request(_NAV, session)
        if payload["code"] == -101:
            return None
        data = _response_data(payload)
        if data.get("isLogin") is False:
            return None
        uid, name = data.get("mid"), data.get("uname")
        if data.get("isLogin") is not True or type(uid) is not int or uid <= 0 or not isinstance(name, str):
            raise BiliAuthError("无法确认当前 B 站账号。")
        return BiliAccount(uid, " ".join(name.split())[:80] or "B 站用户")

    def load(self):
        try:
            with self.path.open("rb") as source:
                protected = source.read(_MAX_SESSION + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise BiliAuthError("无法打开本机保存的 B 站会话，请重新登录。") from None
        try:
            if not 0 < len(protected) <= _MAX_SESSION:
                raise ValueError()
            data = json.loads(_dpapi(protected, decrypt=True))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError()
            return _session(data.get("cookies"))
        except (OSError, ValueError, UnicodeError):
            raise BiliAuthError("Windows 无法读取已保存的 B 站会话，请重新登录。") from None

    def save(self, session):
        temporary = None
        try:
            validated = _session(dict(session.cookies))
            payload = json.dumps({"version": 1, "cookies": dict(validated.cookies)}, ensure_ascii=True).encode("utf-8")
            protected = _dpapi(payload)
            if not 0 < len(protected) <= _MAX_SESSION:
                raise ValueError()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, prefix="bili-session-", delete=False) as output:
                temporary = Path(output.name)
                output.write(protected)
            temporary.replace(self.path)
        except (OSError, ValueError):
            raise BiliAuthError("Windows 无法保存 B 站会话。") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise BiliAuthError("无法删除本机保存的 B 站会话。") from None
