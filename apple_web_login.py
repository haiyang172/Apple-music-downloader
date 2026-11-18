"""Connect an isolated Apple Music web login to the local library client."""

import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4
import winreg

from engine import Cancelled
from music_library import LibraryClient


_MAX_LINE = 16 * 1024
_LOGIN_TIMEOUT = 15 * 60
_CLEAR_TIMEOUT = 60
_TOKEN = re.compile(r"[\x21-\x7e]{50,8192}")
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PIPE_RETRY_ERRORS = (2, 121, 231)
_PIPE_CLOSED_ERRORS = (109, 232, 233)
RUNTIME_INSTALLER_SIZE = 215116512
RUNTIME_INSTALLER_SHA256 = "d3cbe8fedb104282d745fff106993efead51ae94f5595a4e75804446a974f9c3"
_RUNTIME_KEY = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def runtime_available():
    """Check Microsoft's per-user and 32-bit machine runtime registrations."""
    for registry, access in (
        (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
    ):
        try:
            with winreg.OpenKey(registry, _RUNTIME_KEY, 0, access) as key:
                version, _kind = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if isinstance(version, str) and re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,10}){3}", version):
            if any(int(component) for component in version.split(".")):
                return True
    return False


def ensure_runtime(root, stop, on_status):
    """Install the pinned bundled WebView2 runtime if Windows has none."""
    if stop.is_set():
        raise Cancelled("已取消 Apple Music 资料库授权。")
    if runtime_available():
        return
    installer = Path(root).resolve() / "apple-web-login" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    on_status("正在准备 Apple Music 登录所需的浏览器组件…")
    digest = sha256()
    try:
        with installer.open("rb") as source:
            if os.fstat(source.fileno()).st_size != RUNTIME_INSTALLER_SIZE:
                raise ValueError()
            for block in iter(lambda: source.read(1024 * 1024), b""):
                if stop.is_set():
                    raise Cancelled("已取消 Apple Music 资料库授权。")
                digest.update(block)
    except (OSError, ValueError):
        raise RuntimeError("找不到完整的网页登录组件，请重新运行安装包。") from None
    if digest.hexdigest() != RUNTIME_INSTALLER_SHA256:
        raise RuntimeError("网页登录组件校验失败，请重新运行安装包。")
    on_status("正在安装 Apple Music 登录所需的浏览器组件…")
    try:
        process = subprocess.Popen(
            [str(installer), "/silent", "/install"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        raise RuntimeError("无法安装网页登录组件，请重新运行安装包。") from None
    deadline = time.monotonic() + 180
    try:
        while process.poll() is None:
            if stop.is_set():
                raise Cancelled("已取消 Apple Music 资料库授权。")
            if time.monotonic() >= deadline:
                raise RuntimeError("网页登录组件安装超时，请重新运行安装包。")
            stop.wait(0.1)
        if stop.is_set():
            raise Cancelled("已取消 Apple Music 资料库授权。")
        if not runtime_available():
            raise RuntimeError("网页登录组件尚未就绪，请重新运行安装包。")
    finally:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                if process.poll() is None:
                    raise
            process.wait(timeout=5)


def _pipe_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    api.WaitNamedPipeW.restype = wintypes.BOOL
    api.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    api.CreateFileW.restype = wintypes.HANDLE
    api.PeekNamedPipe.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    api.PeekNamedPipe.restype = wintypes.BOOL
    api.ReadFile.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    )
    api.ReadFile.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _close_helper(process):
    if process.poll() is not None:
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL

    @callback_type
    def close_window(window, _parameter):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process.pid:
            user32.PostMessageW(window, 0x0010, 0, 0)
        return True

    user32.EnumWindows(close_window, 0)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                raise
        process.wait(timeout=5)


def _check_wait(process, stop, deadline):
    if stop.is_set():
        raise Cancelled("已取消 Apple Music 资料库授权。")
    if time.monotonic() >= deadline:
        raise RuntimeError("Apple Music 授权等待超时，请重新打开登录窗口。")
    result = process.poll()
    if result == 0:
        raise Cancelled("已关闭 Apple Music 授权窗口。")
    if result is not None:
        raise RuntimeError("Apple Music 登录组件已关闭，请重新打开。")


def _open_pipe(api, name, process, stop, deadline):
    while True:
        _check_wait(process, stop, deadline)
        if api.WaitNamedPipeW(name, 100):
            handle = api.CreateFileW(name, 0x80000000, 0, None, 3, 0x00110000, None)
            if handle != _INVALID_HANDLE:
                return handle
        if ctypes.get_last_error() not in _PIPE_RETRY_ERRORS:
            raise RuntimeError("无法连接 Apple Music 登录窗口。")
        stop.wait(0.05)


def _read_message(api, handle, pending):
    if b"\n" not in pending:
        available = wintypes.DWORD()
        if not api.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() in _PIPE_CLOSED_ERRORS:
                raise Cancelled("Apple Music 授权窗口已关闭。")
            raise RuntimeError("无法读取 Apple Music 授权结果。")
        if available.value:
            buffer = ctypes.create_string_buffer(min(available.value, 4096))
            received = wintypes.DWORD()
            if not api.ReadFile(handle, buffer, len(buffer), ctypes.byref(received), None):
                raise RuntimeError("无法读取 Apple Music 授权结果。")
            pending += buffer.raw[:received.value]
    if b"\n" not in pending:
        if len(pending) > _MAX_LINE:
            raise RuntimeError("Apple Music 授权响应无效。")
        return None, pending
    line, pending = pending.split(b"\n", 1)
    if len(line) > _MAX_LINE:
        raise RuntimeError("Apple Music 授权响应无效。")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise RuntimeError("Apple Music 授权响应无效。") from None
    if not isinstance(message, dict):
        raise RuntimeError("Apple Music 授权响应无效。")
    return message, pending


def _run_helper(root, profile, region, stop, on_status, *, clear=False):
    if os.name != "nt":
        raise RuntimeError("Apple Music 网页登录需要 Windows。")
    if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}", region):
        raise ValueError("Apple Music 地区应为两个小写字母。")
    folder = Path(profile)
    if not folder.is_absolute():
        raise ValueError("Apple Music 登录目录应为完整路径。")
    helper = Path(root).resolve() / "apple-web-login" / "AppleMusicLogin.exe"
    if not helper.is_file():
        raise RuntimeError("找不到 Apple Music 登录组件，请重新运行安装包。")
    if stop.is_set():
        raise Cancelled("已取消 Apple Music 资料库授权。")
    ensure_runtime(root, stop, on_status)
    pipe_name = "amdl-apple-login-" + uuid4().hex
    command = [str(helper), "--pipe", pipe_name, "--profile", str(folder.resolve()), "--region", region]
    if clear:
        command.append("--clear-profile")
    api = _pipe_api()
    on_status("正在清除 Apple Music 网页登录…" if clear else "正在打开 Apple Music 官方登录页…")
    try:
        process = subprocess.Popen(
            command, cwd=helper.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        raise RuntimeError("无法启动 Apple Music 登录组件，请重新运行安装包。") from None
    handle = None
    deadline = time.monotonic() + (_CLEAR_TIMEOUT if clear else _LOGIN_TIMEOUT)
    try:
        handle = _open_pipe(api, "\\\\.\\pipe\\" + pipe_name, process, stop, deadline)
        pending = b""
        while True:
            if stop.is_set():
                raise Cancelled("已取消 Apple Music 资料库授权。")
            if time.monotonic() >= deadline:
                raise RuntimeError("Apple Music 授权等待超时，请重新打开登录窗口。")
            message, pending = _read_message(api, handle, pending)
            if message is None:
                stop.wait(0.05)
                continue
            kind = message.get("type")
            if kind == "ready":
                on_status("请在 Apple 页面完成登录，然后连接资料库。")
            elif kind == "cancelled":
                raise Cancelled("已取消 Apple Music 资料库授权。")
            elif kind == "error":
                raise RuntimeError("Apple Music 登录未完成，请重新打开授权窗口。")
            elif kind == "cleared" and clear:
                return None
            elif kind == "token" and not clear:
                token = message.get("value")
                if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                    raise RuntimeError("Apple Music 返回的资料库授权无效。")
                return token
            else:
                raise RuntimeError("Apple Music 授权响应无效。")
    finally:
        if handle is not None:
            api.CloseHandle(handle)
        _close_helper(process)


def connect_library(root, profile, region, catalog, stop, on_status):
    """Return a verified token for immediate encrypted storage by the caller."""
    token = _run_helper(root, profile, region, stop, on_status)
    if stop.is_set():
        raise Cancelled("已取消 Apple Music 资料库授权。")
    on_status("正在验证 Apple Music 资料库授权…")
    LibraryClient(token, region, catalog=catalog).items("song", limit=1)
    if stop.is_set():
        raise Cancelled("已取消 Apple Music 资料库授权。")
    return token


def clear_profile(root, profile, stop):
    """Clear the app-owned Apple website profile after an explicit logout."""
    if not Path(profile).exists():
        return
    _run_helper(root, profile, "us", stop, lambda _status: None, clear=True)
