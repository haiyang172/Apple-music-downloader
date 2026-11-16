"""QQ Music QR login and Windows-protected session storage."""

from dataclasses import dataclass, field
from http.client import HTTPException
from http.cookiejar import CookieJar, DefaultCookiePolicy
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
from secrets import randbelow, token_hex
from struct import unpack_from
from tempfile import NamedTemporaryFile
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from settings import _dpapi


# QQ Music's web QR sequence is documented in MusicUnlock v2.3.0's QqMusicApi.kt.
_QR = "https://ssl.ptlogin2.qq.com/ptqrshow"
_POLL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
_LOGIN_REF = "https://xui.ptlogin2.qq.com/"
_LOGIN_JUMP = "https://graph.qq.com/oauth2.0/login_jump"
_AUTHORIZE = "https://graph.qq.com/oauth2.0/authorize"
_MUSIC = "https://y.qq.com"
_MUSICU = "https://u.y.qq.com/cgi-bin/musicu.fcg"
_COLLECTED = "https://c.y.qq.com/fav/fcgi-bin/fcg_get_profile_order_asset.fcg"
_HOMEPAGE = "https://c6.y.qq.com/rsc/fcgi-bin/fcg_get_profile_homepage.fcg"
_RETURN = _MUSIC + "/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/"
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_QQ_CHECK = frozenset({"ssl.ptlogin2.graph.qq.com", "ptlogin2.graph.qq.com",
                       "ssl.ptlogin2.qq.com", "ptlogin2.qq.com"})
_CALLBACK = re.compile(r"ptuiCB\(\s*'([0-9]{1,3})'\s*,\s*'(?:[^'\\]|\\.)*'\s*,\s*'((?:[^'\\]|\\.)*)'"
                       r"(?:\s*,\s*'(?:[^'\\]|\\.)*'){2,4}\s*\)\s*;?\s*\Z")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}")
_MUSIC_ID = re.compile(r"[A-Za-z0-9]{1,64}")
_OAUTH_CODE = re.compile(r"[A-Za-z0-9._~-]{1,512}")
_MODULE = re.compile(r"[A-Za-z0-9_.]{1,100}")
_MAX_SESSION = 32_768
_MAX_RESPONSE = 4 * 1024 * 1024
_REDIRECT = {301, 302, 303, 307, 308}
_LOGIN_MODULE = "QQConnectLogin.LoginServer"
_PLAYLIST_MODULE = "music.musicasset.PlaylistBaseRead"
QQ_AUTH_EXPIRED_CODES = frozenset({1000, 104400, 104401})


class QqAuthError(RuntimeError):
    """QQ Music or the Windows credential store rejected an operation."""


@dataclass(frozen=True)
class QqQR:
    image: bytes = field(repr=False)
    _signature: str = field(repr=False)


@dataclass(frozen=True)
class QqSession:
    musicid: str = field(repr=False)
    _key: str = field(repr=False)
    _name: str = field(repr=False)


@dataclass(frozen=True)
class QqAccount:
    musicid: str = field(repr=False)
    name: str


@dataclass(frozen=True)
class QqPoll:
    state: str
    session: QqSession | None = field(default=None, repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _jar():
    return CookieJar(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))


def _request(url, referer, *, jar=None, cookie=None, data=None, content_type=None, redirects=False,
             limit=_MAX_RESPONSE):
    headers = {"User-Agent": _USER_AGENT, "Referer": referer, "Accept-Encoding": "identity"}
    if cookie:
        if url != _MUSICU and not any(url.startswith(endpoint + "?") for endpoint in (_POLL, _COLLECTED, _HOMEPAGE)):
            raise ValueError("QQ 登录凭据只能发送到官方登录或音乐接口。")
        headers["Cookie"] = cookie
    if data is not None:
        headers["Content-Type"] = content_type
    if url == _MUSICU:
        headers["Origin"] = _MUSIC
    request = Request(url, data=data, headers=headers)
    if jar is not None:
        jar.add_cookie_header(request)
    deadline = time.monotonic() + 16
    body = bytearray()
    try:
        with build_opener(_NoRedirect()).open(request, timeout=7) as response:
            if jar is not None:
                jar.extract_cookies(response, request)
            while len(body) <= limit:
                if time.monotonic() > deadline:
                    raise QqAuthError("QQ 音乐请求超时，请重试。")
                block = response.read1(min(65_536, limit + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
    except HTTPError as response:
        with response:
            if redirects and response.code in _REDIRECT:
                if jar is not None:
                    jar.extract_cookies(response, request)
                return b"", response.headers.get("Location")
        raise QqAuthError("QQ 音乐暂未接受登录请求，请稍后重试。") from None
    except (OSError, HTTPException):
        raise QqAuthError("无法连接 QQ 音乐，请检查网络后重试。") from None
    if len(body) > limit:
        raise QqAuthError("QQ 音乐响应过大，请重试。")
    return bytes(body), None


def _jar_cookie(jar, url, name):
    request = Request(url)
    jar.add_cookie_header(request)
    cookies = SimpleCookie()
    try:
        cookies.load(request.get_header("Cookie", ""))
    except CookieError:
        return None
    value = cookies[name].value if name in cookies else ""
    return value if _COOKIE_VALUE.fullmatch(value) else None


def _hash33(value, seed=0):
    for char in value:
        seed = (seed * 33 + ord(char)) & 0x7FFFFFFF
    return seed


def _trusted_url(base, value, *, validation=False):
    if not isinstance(value, str) or len(value) > 4096 or any(
            ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
        raise QqAuthError("QQ 音乐返回了无效的登录跳转，请重试。")
    try:
        target = urlsplit(urljoin(base, value))
    except ValueError:
        raise QqAuthError("QQ 音乐返回了无效的登录跳转，请重试。") from None
    host, path = target.netloc.lower(), target.path
    allowed = (host in _QQ_CHECK and path == "/check_sig") or (
        host == "graph.qq.com" and path == "/oauth2.0/login_jump")
    if not validation:
        allowed = host == "y.qq.com" and path == "/portal/wx_redirect.html"
    # Some official check_sig links have a legacy http scheme. Use HTTPS for all requests.
    if not allowed or target.scheme not in ("http", "https") or target.fragment:
        raise QqAuthError("QQ 音乐返回了无效的登录跳转，请重试。")
    return target._replace(scheme="https").geturl()


def _session(musicid, key, name):
    if type(musicid) is int:
        musicid = str(musicid)
    if (not isinstance(musicid, str) or not _MUSIC_ID.fullmatch(musicid) or musicid == "0"
            or not isinstance(key, str) or not _COOKIE_VALUE.fullmatch(key)):
        raise QqAuthError("QQ 音乐未返回有效的会话，请重新扫码。")
    label = " ".join(name.replace("\0", "").split())[:80] if isinstance(name, str) else ""
    return QqSession(musicid, key, label or "QQ 音乐用户")


def _json_object(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise QqAuthError("QQ 音乐返回了无法读取的会话数据。") from None
    if not isinstance(payload, dict):
        raise QqAuthError("QQ 音乐未返回有效的会话数据。")
    return payload


def _code(payload):
    value = payload.get("code")
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,9}", value):
        return int(value)
    return value if type(value) is int else None


def _musicu(module, method, params, common, *, cookie=None):
    if not (_MODULE.fullmatch(module) and _MODULE.fullmatch(method)):
        raise ValueError("无效的 QQ 音乐接口名称。")
    body = {"comm": common, module: {"module": module, "method": method, "param": params}}
    raw, _ = _request(_MUSICU, _MUSIC + "/", cookie=cookie, data=json.dumps(body).encode("utf-8"),
                      content_type="application/json;charset=UTF-8")
    payload = _json_object(raw)
    result = payload.get(module)
    if not isinstance(result, dict):
        if _code(payload) in QQ_AUTH_EXPIRED_CODES:
            return {"code": _code(payload)}
        raise QqAuthError("QQ 音乐未返回有效的会话数据。")
    return result


def _credential(session):
    if not isinstance(session, QqSession):
        raise QqAuthError("请先登录 QQ 音乐。")
    session = _session(session.musicid, session._key, session._name)
    musicid, key = session.musicid, session._key
    login_type = "1" if key.startswith("W_X") else "2"
    cookie = f"uin={musicid}; qqmusic_key={key}; qm_keyst={key}; tmeLoginType={login_type}"
    if login_type == "1":
        cookie += f"; wxuin={musicid}"
    return musicid, key, login_type, cookie


def qq_music_request(session, module, method, params, *, common=None):
    """Send an authenticated request to QQ Music's fixed gateway and return its module."""
    musicid, key, login_type, cookie = _credential(session)
    protocol = {"ct": "11", "tmeAppID": "qqmusic", "format": "json", "inCharset": "utf-8", "outCharset": "utf-8"}
    if common:
        protocol.update({name: value for name, value in common.items() if name in {"ct", "cv", "v", "platform"}})
    protocol.update({"uin": musicid, "uid": musicid, "qq": musicid, "authst": key, "tmeLoginType": login_type})
    return _musicu(module, method, params, protocol, cookie=cookie)


def qq_profile_request(session, kind, *, start=0, end=79):
    """Read the signed-in account's QQ Music homepage or collected playlists."""
    musicid, _, _, cookie = _credential(session)
    params = {"ct": 20, "userid": musicid, "format": "json"}
    if kind == "collected":
        if type(start) is not int or type(end) is not int or not 0 <= start <= end < start + 100:
            raise ValueError("无效的 QQ 音乐收藏歌单分页范围。")
        url = _COLLECTED
        params.update({"cid": 205360956, "reqtype": 3, "sin": start, "ein": end})
    elif kind == "homepage":
        url = _HOMEPAGE
        params.update({"cv": 4747474, "cid": 205360838})
    else:
        raise ValueError("无效的 QQ 音乐账号资料类别。")
    raw, _ = _request(url + "?" + urlencode(params), _MUSIC + "/", cookie=cookie)
    return _json_object(raw)


def qq_public_playlist_request(params):
    """Read a page of an anonymously accessible QQ Music playlist."""
    protocol = {"ct": "11", "cv": 13020508, "v": 13020508, "tmeAppID": "qqmusic", "format": "json", "uid": "0"}
    return _musicu("music.srfDissInfo.DissInfo", "CgiGetDiss", params, protocol)


def _finish_login(jar, location):
    url = _trusted_url("https://ssl.ptlogin2.graph.qq.com/", location, validation=True)
    for _ in range(5):
        _, redirect = _request(url, _LOGIN_REF, jar=jar, redirects=True, limit=131_072)
        key = _jar_cookie(jar, _AUTHORIZE, "p_skey")
        if key:
            break
        if not redirect:
            raise QqAuthError("QQ 登录未完成，请重新扫码。")
        url = _trusted_url(url, redirect, validation=True)
    else:
        raise QqAuthError("QQ 登录未完成，请重新扫码。")

    state = token_hex(16)
    form = {"response_type": "code", "client_id": 100497308, "redirect_uri": _RETURN, "scope": "get_user_info",
            "state": state, "switch": "", "from_ptlogin": 1, "src": 1, "update_auth": 1, "openapi": "1010_1030",
            "g_tk": _hash33(key, 5381), "auth_time": int(time.time() * 1000), "ui": token_hex(16)}
    _, redirect = _request(_AUTHORIZE, _LOGIN_JUMP, jar=jar, redirects=True, data=urlencode(form).encode("ascii"),
                           content_type="application/x-www-form-urlencoded", limit=131_072)
    target = _trusted_url(_MUSIC + "/", redirect)
    try:
        query = parse_qs(urlsplit(target).query, max_num_fields=16)
    except ValueError:
        raise QqAuthError("QQ 音乐授权未完成，请重新扫码。") from None
    code = query.get("code", [])
    if query.get("state") != [state] or len(code) != 1 or not _OAUTH_CODE.fullmatch(code[0]):
        raise QqAuthError("QQ 音乐授权未完成，请重新扫码。")
    common = {"cv": 13020508, "v": 13020508, "ct": "11", "tmeAppID": "qqmusic", "format": "json",
              "inCharset": "utf-8", "outCharset": "utf-8", "uid": "0", "tmeLoginType": "2"}
    result = _musicu(_LOGIN_MODULE, "QQLogin", {"code": code[0]}, common)
    data = result.get("data")
    if _code(result) != 0 or not isinstance(data, dict):
        raise QqAuthError("QQ 音乐授权未完成，请重新扫码。")
    return _session(data.get("musicid") or data.get("openId") or data.get("openid"),
                    data.get("musickey"), data.get("nickname") or data.get("nick"))


class QqAuth:
    def __init__(self, path):
        self.path = Path(path)

    def begin(self):
        params = {"appid": 716027609, "e": 2, "l": "M", "s": 3, "d": 72, "v": 4,
                  "t": f"0.{randbelow(1_000_000_000):09}", "daid": 383, "pt_3rd_aid": 100497308}
        jar = _jar()
        image, _ = _request(_QR + "?" + urlencode(params), _LOGIN_REF, jar=jar, limit=131_072)
        signature = _jar_cookie(jar, _POLL, "qrsig")
        if not signature or not image.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") or len(image) < 33:
            raise QqAuthError("QQ 返回了无效的登录二维码，请重试。")
        if not all(1 <= value <= 512 for value in unpack_from(">II", image, 16)):
            raise QqAuthError("QQ 返回了无效的登录二维码，请重试。")
        return QqQR(image, signature)

    def poll(self, qr):
        if not isinstance(qr, QqQR) or not _COOKIE_VALUE.fullmatch(qr._signature):
            raise ValueError("请先获取有效的 QQ 登录二维码。")
        params = {"u1": _LOGIN_JUMP, "ptqrtoken": _hash33(qr._signature), "ptredirect": 0, "h": 1,
                  "t": 1, "g": 1, "from_ui": 1, "ptlang": 2052, "action": f"0-0-{int(time.time()*1000)}",
                  "js_ver": 20102616, "js_type": 1, "login_sig": "", "pt_uistyle": 40, "aid": 716027609,
                  "daid": 383, "pt_3rd_aid": 100497308, "has_onekey": 1}
        jar = _jar()
        raw, _ = _request(_POLL + "?" + urlencode(params), _LOGIN_REF, jar=jar,
                          cookie=f"qrsig={qr._signature}", limit=16_384)
        callback = _CALLBACK.fullmatch(raw.decode("utf-8", "replace"))
        if callback:
            code, location = callback.groups()
            state = {"65": "expired", "66": "waiting", "67": "scanned"}.get(code)
            if state:
                return QqPoll(state)
            if code == "0":
                return QqPoll("success", _finish_login(jar, location))
        raise QqAuthError("QQ 返回了无效的扫码状态，请重新获取二维码。")

    def check(self, session):
        if not isinstance(session, QqSession):
            return None
        result = qq_music_request(session, _PLAYLIST_MODULE, "GetPlaylistByUin", {"uin": session.musicid})
        code = _code(result)
        if code in QQ_AUTH_EXPIRED_CODES:
            return None
        data = result.get("data")
        if code != 0 or not isinstance(data, dict) or not isinstance(data.get("v_playlist"), list):
            raise QqAuthError("QQ 音乐无法确认登录状态，请稍后重试。")
        return QqAccount(session.musicid, session._name)

    def load(self):
        try:
            with self.path.open("rb") as stream:
                protected = stream.read(_MAX_SESSION + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise QqAuthError("无法打开已保存的 QQ 音乐会话，请重新登录。") from None
        try:
            if not 0 < len(protected) <= _MAX_SESSION:
                raise ValueError()
            values = json.loads(_dpapi(protected, decrypt=True))
            if not isinstance(values, dict) or values.get("version") != 1:
                raise ValueError()
            return _session(values.get("musicid"), values.get("key"), values.get("name"))
        except (ValueError, OSError, QqAuthError):
            raise QqAuthError("Windows 无法读取已保存的 QQ 音乐会话，请重新登录。") from None

    def save(self, session):
        session = _session(session.musicid, session._key, session._name)
        temporary = None
        try:
            data = {"version": 1, "musicid": session.musicid, "key": session._key, "name": session._name}
            protected = _dpapi(json.dumps(data).encode("utf-8"))
            if not 0 < len(protected) <= _MAX_SESSION:
                raise ValueError()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.path.parent, prefix="qq-session-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(protected)
            temporary.replace(self.path)
            temporary = None
        except (ValueError, OSError):
            raise QqAuthError("Windows 无法保存 QQ 音乐会话。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise QqAuthError("无法删除已保存的 QQ 音乐会话。") from None
