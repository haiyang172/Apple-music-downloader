"""Per-user preferences and Windows-protected Apple Music session storage."""

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import tempfile


DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "AMDL Studio" / "data"
FILE = DATA / "settings.json"
DEFAULTS = {
    "folder": "",
    "category": "",
    "quality": "AAC",
    "output": "original",
    "mp3_bitrate": 320,
    "region": "au",
    "wrapper": "",
    "auto_service": True,
}


class DataBlob(ctypes.Structure):
    _fields_ = (("size", wintypes.DWORD), ("data", ctypes.c_void_p))


def _dpapi(data, decrypt=False):
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    incoming = DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    outgoing = DataBlob()
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = (ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob))
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError("Windows 无法读取或保护此账户的登录会话。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def load():
    if not FILE.exists():
        return dict(DEFAULTS)
    try:
        saved = json.loads(FILE.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError("无法读取设置文件。") from None
    result = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if type(saved.get(key)) is type(default):
            result[key] = saved[key]
    if isinstance(saved.get("session"), str):
        result["session"] = saved["session"]
    return result


def save(values):
    data = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    if values.get("session"):
        data["session"] = values["session"]
    FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=FILE.parent,
                                         prefix="prefs-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def protect_session(token):
    token = token.strip()
    if len(token) < 50 or len(token) > 8192 or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise ValueError("请输入 Apple Music 的完整会话令牌（media-user-token）。")
    return base64.b64encode(_dpapi(token.encode("utf-8"))).decode("ascii")


def session(values):
    if not values.get("session"):
        return ""
    try:
        return _dpapi(base64.b64decode(values["session"], validate=True), decrypt=True).decode("utf-8")
    except (OSError, ValueError, UnicodeError):
        raise ValueError("此 Windows 账户无法读取已保存的会话，请重新配置。") from None
