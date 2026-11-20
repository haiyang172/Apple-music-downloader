"""NetEase Music QR login and Windows-protected session storage."""

from base64 import b64encode
from dataclasses import dataclass, field
from hashlib import md5
from http.client import HTTPException
from http.cookiejar import CookieJar, DefaultCookiePolicy
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path
import re
from secrets import choice, randbelow
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
_PLAYER_V1 = "/api/song/enhance/player/url/v1"
_PLAYER_LEGACY = "/api/song/enhance/player/url"
_PUBLIC_PLAYLIST_ROUTES = frozenset({"/api/v6/playlist/detail", "/api/v3/song/detail"})
_AUTH_ROUTES = frozenset({_UNIQUE_KEY, _POLL})
_API_ROUTES = _PUBLIC_PLAYLIST_ROUTES | {_ACCOUNT, _USER_PLAYLISTS, _SONG_SEARCH, "/api/playlist/detail",
                                         "/api/song/detail", _PLAYER_LEGACY, _PLAYER_V1}
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
_CHAIN = re.compile(r"v1_unknown-[0-9]{1,6}_web_login_[0-9]{13,16}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}")
_AUTH_COOKIES = ("MUSIC_U", "__csrf")
_MAX_RESPONSE = 4 * 1024 * 1024
_MAX_SESSION = 32_768
_WEAPI_KEY = b"0CoJUm6Qyw8W8jud"
_WEAPI_IV = b"0102030405060708"
_EAPI_KEY = b"e82ckenh8dichen8"
# NetEase risk control rejects logins from overseas nodes; the community workaround
# is the official realIP parameter with a mainland address (NeteaseCloudMusicApi).
_REAL_IP = "116.25.146.177"
_WEAPI_RSA_MODULUS = int(
    "e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e"
    "417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7", 16)


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


def _aes_encrypt(data, key, *, mode="CBC", iv=None):
    """Encrypt whole AES blocks with Windows CNG; no optional package is needed."""
    import ctypes
    from ctypes import wintypes

    if len(key) != 16 or not data or len(data) % 16:
        raise ValueError("无效的网易云音乐请求数据。")
    if mode == "CBC" and (iv is None or len(iv) != 16):
        raise ValueError("无效的网易云音乐请求向量。")
    try:
        bcrypt = ctypes.WinDLL("bcrypt", use_last_error=True)
    except (AttributeError, OSError):
        raise NeteaseAuthError("Windows 无法准备网易云音乐音质请求。") from None
    handle, ulong, pointer = wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p
    signatures = {
        "BCryptOpenAlgorithmProvider": [ctypes.POINTER(handle), wintypes.LPCWSTR, wintypes.LPCWSTR, ulong],
        "BCryptSetProperty": [handle, wintypes.LPCWSTR, pointer, ulong, ulong],
        "BCryptGetProperty": [handle, wintypes.LPCWSTR, pointer, ulong, ctypes.POINTER(ulong), ulong],
        "BCryptGenerateSymmetricKey": [handle, ctypes.POINTER(handle), pointer, ulong, pointer, ulong, ulong],
        "BCryptEncrypt": [handle, pointer, ulong, pointer, pointer, ulong, pointer, ulong, ctypes.POINTER(ulong), ulong],
        "BCryptDestroyKey": [handle],
        "BCryptCloseAlgorithmProvider": [handle, ulong],
    }
    for name, arguments in signatures.items():
        operation = getattr(bcrypt, name)
        operation.argtypes, operation.restype = arguments, ctypes.c_long

    def checked(status):
        if status != 0:
            raise NeteaseAuthError("Windows 无法准备网易云音乐音质请求。")

    provider, key_handle = handle(), handle()
    key_buffer = ctypes.create_string_buffer(key, len(key))
    key_object = None
    try:
        checked(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(provider), "AES", None, 0))
        chaining = ctypes.create_unicode_buffer("ChainingModeCBC" if mode == "CBC" else "ChainingModeECB")
        checked(bcrypt.BCryptSetProperty(provider, "ChainingMode", chaining, ctypes.sizeof(chaining), 0))
        object_length, result_length = ulong(), ulong()
        checked(bcrypt.BCryptGetProperty(provider, "ObjectLength", ctypes.byref(object_length),
                                        ctypes.sizeof(object_length), ctypes.byref(result_length), 0))
        key_object = ctypes.create_string_buffer(object_length.value)
        checked(bcrypt.BCryptGenerateSymmetricKey(provider, ctypes.byref(key_handle), key_object,
                                                 len(key_object), key_buffer, len(key), 0))
        plaintext = ctypes.create_string_buffer(data, len(data))
        iv_buffer = ctypes.create_string_buffer(iv, len(iv)) if mode == "CBC" else None
        iv_size = len(iv) if mode == "CBC" else 0
        output = ctypes.create_string_buffer(len(data))
        checked(bcrypt.BCryptEncrypt(key_handle, plaintext, len(data), None, iv_buffer, iv_size, output,
                                     len(output), ctypes.byref(result_length), 0))
        if result_length.value != len(data):
            raise NeteaseAuthError("Windows 未能准备完整的网易云音乐音质请求。")
        return output.raw
    finally:
        if key_handle:
            bcrypt.BCryptDestroyKey(key_handle)
        if provider:
            bcrypt.BCryptCloseAlgorithmProvider(provider, 0)
        ctypes.memset(key_buffer, 0, len(key_buffer))
        if key_object is not None:
            ctypes.memset(key_object, 0, len(key_object))


def _aes_cbc_encrypt(data, key, iv):
    return _aes_encrypt(data, key, mode="CBC", iv=iv)


def _aes_ecb_encrypt(data, key):
    return _aes_encrypt(data, key, mode="ECB")


def _weapi_body(params):
    """Match the official web client's double AES-CBC and public-key RSA envelope."""
    secret = "".join(choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(16))

    def encrypt(text, key):
        raw = text.encode("utf-8")
        padding = 16 - len(raw) % 16
        return b64encode(_aes_cbc_encrypt(raw + bytes([padding]) * padding, key, _WEAPI_IV)).decode("ascii")

    text = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    encrypted = encrypt(encrypt(text, _WEAPI_KEY), secret.encode("ascii"))
    rsa_input = int.from_bytes(secret[::-1].encode("ascii"), "big")
    encrypted_key = format(pow(rsa_input, 65537, _WEAPI_RSA_MODULUS), "0256x")
    return urlencode({"params": encrypted, "encSecKey": encrypted_key}).encode("ascii")


def _eapi_body(path, params):
    """Match the current web client's AES-ECB eapi envelope for the player route."""
    text = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    message = f"nobody{path}use{text}md5forencrypt"
    digest = md5(message.encode("utf-8")).hexdigest()
    data = f"{path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode("utf-8")
    padding = 16 - len(data) % 16
    return _aes_ecb_encrypt(data + bytes([padding]) * padding, _EAPI_KEY).hex().upper().encode("ascii")


def _request(path, params=None, *, chain=None, jar=None, cookies=()):
    if path not in _API_ROUTES and path not in _AUTH_ROUTES:
        raise ValueError("无效的网易云音乐接口。")
    headers = {"User-Agent": _USER_AGENT, "Referer": _SITE + "/", "Origin": _SITE,
               "Accept": "application/json", "Accept-Encoding": "identity"}
    url = _SITE + path
    params = {**(params or {}), "realIP": _REAL_IP}
    query = urlencode(params).encode("ascii")
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
        if path == _PLAYER_V1:
            if (not isinstance(params, dict) or set(params) != {"ids", "level", "encodeType", "realIP"}
                    or not isinstance(params.get("ids"), str) or not re.fullmatch(r"\[[1-9][0-9]{0,19}\]", params["ids"])
                    or params.get("level") not in ("standard", "higher", "exhigh", "lossless", "hires")
                    or params.get("encodeType") != "flac"):
                raise ValueError("无效的网易云音乐音质请求。")
            # The current web client wraps this route in an AES-ECB eapi envelope.
            url = _SITE + "/eapi/song/enhance/player/url/v1"
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = _eapi_body(path, params)
        elif path == _PLAYER_LEGACY:
            # The legacy bitrate route now also requires an encrypted form POST.
            url = _SITE + "/weapi/song/enhance/player/url"
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = _weapi_body(params)
        elif query:
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


def _cookie_values(text):
    """Parse a browser cookie string or a bare MUSIC_U value into session cookies."""
    if not isinstance(text, str) or not 0 < len(text) <= 16384:
        raise ValueError("Cookie 内容无效。")
    stripped = text.strip()
    if "=" not in stripped:
        values = {"MUSIC_U": stripped}
    else:
        parsed = SimpleCookie()
        try:
            parsed.load(stripped)
        except CookieError:
            raise NeteaseAuthError("Cookie 格式无效，请粘贴 MUSIC_U=... 形式的完整 Cookie。") from None
        values = {name: morsel.value for name, morsel in parsed.items() if name in _AUTH_COOKIES}
    if not values.get("MUSIC_U"):
        raise NeteaseAuthError("Cookie 中缺少 MUSIC_U，请从已登录的浏览器复制。")
    for value in values.values():
        if not _COOKIE_VALUE.fullmatch(value):
            raise NeteaseAuthError("Cookie 内容包含无效字符，请重新复制。")
    return _cookies(values)


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

    def login_with_cookies(self, cookie_text):
        """Build a session from a browser's MUSIC_U cookie and persist it.

        The browser login page can complete risk-control verification that the
        QR poll cannot present, so this is the reliable fallback when the scan
        returns 需要安全验证.
        """
        cookies = _cookie_values(cookie_text)
        account = _account(cookies)
        if account is None:
            raise NeteaseAuthError("网易云音乐未接受这段 Cookie，请确认已在浏览器登录并复制完整。")
        session = NeteaseSession(account.uid, cookies)
        self.save(session)
        return session

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
