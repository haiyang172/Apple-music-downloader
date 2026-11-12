"""Manage the private, local Wrapper Lite account service on Windows."""

import ctypes
from ctypes import wintypes
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import zipfile


VERSION = "v3.0.4"
ASSET = f"wrapper-{VERSION}-windows-x86_64.zip"
ASSET_URL = f"https://github.com/itouakirai/wrapper/releases/download/{VERSION}/{ASSET}"
ASSET_SIZE = 158_565_274
ASSET_SHA256 = "b5e3c4ce7263f76ac9ae43f0da9bfa9279ae5e1a0d660d13b6967e3e0158e9c8"
KERNEL = "vmlinuz-lite-qemu"
INITRD = "lite-initramfs.cpio.gz"
QEMU = "qemu-system-x86_64.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHUNK = 1024 * 1024


def _cancelled(stop):
    if stop.is_set():
        raise RuntimeError("已取消启动本地登录服务。")


def _has_assets(root):
    return all((root / file).is_file() for file in (KERNEL, INITRD, f"bin/{QEMU}"))


def _verify(archive, stop):
    digest = hashlib.sha256()
    count = 0
    with archive.open("rb") as stream:
        for data in iter(lambda: stream.read(CHUNK), b""):
            _cancelled(stop)
            digest.update(data)
            count += len(data)
    if count != ASSET_SIZE or digest.hexdigest() != ASSET_SHA256:
        raise RuntimeError("Wrapper Lite 下载校验失败，请重新获取官方组件。")


def _download(target, stop, on_status):
    request = Request(ASSET_URL, headers={"User-Agent": "AMDL-Studio"})
    count = 0
    last_percent = -1
    with urlopen(request, timeout=30) as response, target.open("xb") as stream:
        for data in iter(lambda: response.read(CHUNK), b""):
            _cancelled(stop)
            count += len(data)
            if count > ASSET_SIZE:
                raise RuntimeError("Wrapper Lite 下载大小超出预期。")
            stream.write(data)
            percent = min(100, count * 100 // ASSET_SIZE)
            if percent // 5 != last_percent // 5:
                on_status(f"正在获取 Wrapper Lite · {percent}%")
                last_percent = percent


def _extract(archive, destination, stop):
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            _cancelled(stop)
            parts = PurePosixPath(item.filename).parts
            if not parts or parts[0] != ASSET.removesuffix(".zip"):
                continue
            if item.is_dir():
                continue
            if parts[1:] == ("LICENSE",):
                relative = Path("LICENSE")
            elif len(parts) >= 3 and parts[1] == "qemu":
                relative = Path(*parts[2:])
            else:
                continue
            if any(part in (".", "..") or ":" in part or "\\" in part for part in relative.parts):
                raise RuntimeError("Wrapper Lite 压缩包包含无效路径。")
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in (0, stat.S_IFREG):
                raise RuntimeError("Wrapper Lite 压缩包包含不支持的文件类型。")
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise RuntimeError("Wrapper Lite 压缩包包含无效路径。")
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(item) as source, target.open("xb") as output:
                for data in iter(lambda: source.read(CHUNK), b""):
                    _cancelled(stop)
                    output.write(data)
    if not _has_assets(destination):
        raise RuntimeError("Wrapper Lite 压缩包缺少 QEMU 或启动镜像。")


def _local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def _owns_listener(port, pid):
    """Check the IPv4 listening socket owner before sending any credential."""
    table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    table.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                      wintypes.ULONG, ctypes.c_int, wintypes.ULONG)
    table.restype = wintypes.DWORD
    size = wintypes.DWORD()
    owner_listeners = 3
    table(None, ctypes.byref(size), False, socket.AF_INET, owner_listeners, 0)
    if not 4 <= size.value <= 8 * 1024 * 1024:
        return False
    for _ in range(2):
        buffer = ctypes.create_string_buffer(size.value)
        result = table(buffer, ctypes.byref(size), False, socket.AF_INET, owner_listeners, 0)
        if result == 122:
            continue
        if result != 0:
            return False
        count, = struct.unpack_from("=I", buffer)
        address = int.from_bytes(socket.inet_aton("127.0.0.1"), "little")
        for index in range(min(count, (len(buffer) - 4) // 24)):
            _, local_address, local_port, _, _, owner = struct.unpack_from("=6I", buffer, 4 + index * 24)
            if owner == pid and local_address == address and socket.ntohs(local_port & 0xffff) == port:
                return True
        return False
    return False


class LoginService:
    def __init__(self, root: Path, data: Path):
        self.root = Path(root).resolve()
        self.directory = Path(data).resolve() / "wrapper-lite"
        self.cache = self.directory / VERSION
        self.lock = threading.RLock()
        self.process = None
        self.port = None
        self.bearer = None
        self.login_id = None

    def _prepare(self, stop, on_status):
        self.directory.mkdir(parents=True, exist_ok=True)
        if _has_assets(self.cache):
            return self.cache
        bundled = self.root / "wrapper-lite" / ASSET
        with tempfile.TemporaryDirectory(prefix="setup-", dir=self.directory) as scratch:
            scratch = Path(scratch)
            if bundled.is_file():
                archive = bundled
            else:
                archive = scratch / ASSET
                _download(archive, stop, on_status)
            on_status("正在校验并安装 Wrapper Lite…")
            _verify(archive, stop)
            extracted = scratch / "assets"
            _extract(archive, extracted, stop)
            try:
                extracted.rename(self.cache)
            except FileExistsError:
                if not _has_assets(self.cache):
                    raise RuntimeError("Wrapper Lite 组件目录不完整，请移除后重试。") from None
        return self.cache

    def _api(self, method, path, payload=None, timeout=3):
        if self.process is None or self.process.poll() is not None or self.port is None:
            raise RuntimeError("本地登录服务未运行。")
        if not _owns_listener(self.port, self.process.pid):
            raise RuntimeError("等待本地登录服务监听。")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request(method, path, body, {
                "Authorization": f"Bearer {self.bearer}", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("本地登录服务拒绝请求或尚未就绪。")
            value = json.loads(response.read(1024 * 1024))
            if not isinstance(value, dict) or type(value.get("code")) is not int:
                raise RuntimeError("本地登录服务返回了无效状态。")
            return value
        except (OSError, ValueError, http.client.HTTPException):
            raise RuntimeError("无法连接本地登录服务。") from None
        finally:
            connection.close()

    def start(self, stop, on_status):
        with self.lock:
            _cancelled(stop)
            if self.status()["running"]:
                return self.status()
            if os.name != "nt":
                raise RuntimeError("本地登录服务需要 Windows x64。")
            self.close()
            try:
                assets = self._prepare(stop, on_status)
            except (OSError, zipfile.BadZipFile):
                raise RuntimeError("无法获取或安装 Wrapper Lite，请检查网络和可用空间。") from None
            _cancelled(stop)
            self.port = _local_port()
            self.bearer = secrets.token_urlsafe(32)
            disk = self.directory / "data.img"
            if not disk.exists():
                with disk.open("xb") as stream:
                    stream.truncate(512 * CHUNK)
            on_status("正在启动本地登录服务…")
            # The short-lived management token reaches QEMU through fw_cfg.
            # Do not use the upstream kernel-argument fallback (base64 is readable).
            with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", newline="\n", delete=False,
                                             prefix="fw-", dir=self.directory) as secret:
                secret.write(f"--api-token\n{self.bearer}\n")
                secret_path = Path(secret.name)
            binary = assets / "bin"
            args = [
                str(binary / QEMU), "-L", str(binary), "-accel", "tcg,thread=multi,tb-size=128",
                "-cpu", "Westmere", "-m", "1024", "-smp", "2", "-kernel", str(assets / KERNEL),
                "-initrd", str(assets / INITRD), "-append",
                "console=ttyS0 quiet loglevel=3 net.ifnames=0 biosdevname=0 panic=-1",
                "-display", "none", "-monitor", "none", "-serial", "none", "-no-reboot",
                "-nic", f"user,model=e1000,hostfwd=tcp:127.0.0.1:{self.port}-:12340",
                "-drive", "file=data.img,format=raw,if=virtio,cache=writeback",
                "-fw_cfg", f"name=opt/wrapper/args,file={secret_path.name}",
            ]
            try:
                self.process = subprocess.Popen(
                    args, cwd=self.directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                )
                deadline = time.monotonic() + 80
                while time.monotonic() < deadline:
                    _cancelled(stop)
                    if self.process.poll() is not None:
                        raise RuntimeError("Wrapper Lite 启动失败，请检查组件或磁盘空间。")
                    try:
                        check = self._api("GET", "/api/overview", timeout=1)
                        overview = check.get("data")
                        if check["code"] == 0 and isinstance(overview, dict) and overview.get("authRequired"):
                            info = self.status()
                            if info["running"]:
                                return info
                    except RuntimeError:
                        pass
                    stop.wait(0.4)
                raise RuntimeError("本地登录服务启动超时。")
            except (OSError, RuntimeError):
                self.close()
                raise
            finally:
                secret_path.unlink(missing_ok=True)

    def status(self):
        try:
            result = self._api("GET", "/status", timeout=2)
            data = result.get("data")
        except RuntimeError:
            data = None
        running = isinstance(data, dict) and result["code"] == 0 and data.get("ready") is True
        if not running:
            return {"running": False, "logged_in": False}
        regions = data.get("regions")
        regions = [r for r in regions if isinstance(r, str) and re.fullmatch(r"[a-z]{2}", r)] if isinstance(regions, list) else []
        count = data.get("clientCount")
        return {"running": True, "logged_in": type(count) is int and count > 0, "regions": regions}

    def login(self, username, password, code=None):
        with self.lock:
            username = username.strip()
            if code is None:
                if not username or not password or len(username) > 320 or len(password) > 1024:
                    raise ValueError("请输入 Apple 账户和密码。")
                result = self._api("POST", "/api/accounts/login", {
                    "username": username, "password": password,
                }, timeout=95)
            else:
                if not self.login_id:
                    raise ValueError("请重新发起登录，获取新的验证码。")
                code = code.strip()
                if not re.fullmatch(r"[0-9]{6}", code):
                    raise ValueError("请输入 6 位 Apple 验证码。")
                result = self._api("POST", f"/api/logins/{self.login_id}/code", {"code": code}, timeout=95)
            data = result.get("data")
            if result["code"] != 0 or not isinstance(data, dict):
                self.login_id = None
                raise RuntimeError("Apple 登录未完成，请检查账户、密码或验证码。")
            identifier = data.get("loginId")
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", identifier):
                raise RuntimeError("本地登录服务没有返回有效会话。")
            self.login_id = identifier
            state = str(data.get("state", "")).lower()
            if state == "running":
                deadline = time.monotonic() + 20
                while state == "running" and time.monotonic() < deadline:
                    time.sleep(0.3)
                    poll = self._api("GET", f"/api/logins/{identifier}")
                    state = str((poll.get("data") or {}).get("state", "")).lower()
            if state == "need2fa":
                return {"logged_in": False, "needs_code": True}
            self.login_id = None
            if state == "success":
                return {"logged_in": True, "needs_code": False}
            raise RuntimeError("Apple 登录未完成，请检查账户、密码或验证码。")

    def logout(self):
        with self.lock:
            if not self.status()["running"]:
                if not (self.directory / "data.img").is_file():
                    return
                self.start(threading.Event(), lambda _: None)
            result = self._api("GET", "/api/accounts")
            accounts = result.get("data")
            if result["code"] != 0 or not isinstance(accounts, list):
                raise RuntimeError("无法读取本地 Apple Music 会话。")
            for account in accounts:
                identifier = account.get("id") if isinstance(account, dict) else None
                if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9-]{1,80}", identifier):
                    reply = self._api("DELETE", f"/api/accounts/{identifier}", timeout=20)
                    if reply["code"] != 0:
                        raise RuntimeError("无法清除本地 Apple Music 会话。")
            self.login_id = None

    def engine_config(self):
        with self.lock:
            state = self.status()
            if not state["logged_in"]:
                raise RuntimeError("请先在账户页面完成 Apple Music 登录。")
            return {
                "wrapper": "applemusicdecrypt",
                "applemusicdecrypt-url": f"http://127.0.0.1:{self.port}",
                "applemusicdecrypt-api-key": self.bearer,
                "applemusicdecrypt-storefront": (state["regions"] or [""])[0],
            }

    def close(self):
        with self.lock:
            process = self.process
            if process is not None and process.poll() is None:
                try:
                    self._api("POST", "/api/system/shutdown", timeout=2)
                    process.wait(timeout=8)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            self.process = None
            self.port = None
            self.bearer = None
            self.login_id = None
