"""Run the installed apmyx backend and validate downloaded Apple Music audio."""

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


BRIDGE = Path("_internal/backend/bin/bridge/bridge.exe")
CODECS = {"AAC": "aac", "ALAC": "alac", "ATMOS": "eac3"}
SERVICE_PORTS = (10020, 20020)
POLL_INTERVAL = 0.2
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ERRORS = {
    "authentication_required": "请检查 Apple Music 的登录状态。ALAC 和杜比全景声还需要本地服务已登录。",
    "wrapper_unavailable": "无法连接本地音频服务，请检查服务状态。",
    "incompatible_wrapper": "本地音频服务与下载组件版本不兼容，请运行 amdl 安装程序更新。",
    "invalid_endpoint": "本地音频服务的地址或访问密钥无效，请重新启动 amdl。",
    "invalid_storefront": "请在账户页面选择有效的 Apple Music 地区。",
    "unsupported_storefront": "本地登录会话未提供所选地区，请改用账号所属的 Apple Music 地区。",
    "unsupported_quality": "这首歌没有所选音质，请选择其他音质。",
    "no_tracks": "Apple Music 未返回可下载的曲目，请检查链接和账户地区。",
    "network_error": "Apple Music 请求超时，请检查网络后重试。",
    "missing_dependency": "下载组件不完整，请重新运行 amdl 安装程序。",
    "tagging_failed": "无法写入歌曲标签。",
}


class Cancelled(RuntimeError):
    """The user cancelled a download or service startup."""


def _check_cancelled(stop):
    if stop.is_set():
        raise Cancelled("已取消。")


def _end_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _safe_text(value, secrets):
    if not isinstance(value, (str, int)):
        return ""
    value = str(value)
    for secret in secrets:
        value = value.replace(secret, "[已隐藏]")
    return " ".join(value.split())[:240]


def _settings_secrets(settings):
    return tuple(
        value for key, value in settings.items()
        if ("token" in key or key.endswith("-api-key")) and isinstance(value, str) and value
    )


def _wrapper_settings(config):
    wrapper = config.get("wrapper") or "legacy"
    if wrapper == "legacy":
        token = config.get("media-user-token", "")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("请先在账户页面登录 Apple Music。")
        return {"wrapper": wrapper, "media-user-token": token.strip(), "alac-max": 192}
    if wrapper != "applemusicdecrypt":
        raise ValueError("不支持所选的本地音频服务。")
    endpoint = config.get("applemusicdecrypt-url")
    match = (
        re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/?", endpoint)
        if isinstance(endpoint, str) else None
    )
    if not match or int(match[1]) > 65535:
        raise ValueError("下载组件只能连接此电脑上的 amdl 登录服务。")
    key = config.get("applemusicdecrypt-api-key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", key):
        raise ValueError("本地音频服务的访问密钥无效，请重新启动 amdl。")
    return {
        "wrapper": wrapper,
        "applemusicdecrypt-url": f"http://127.0.0.1:{int(match[1])}",
        "applemusicdecrypt-api-key": key,
        "alac-max": 192000,  # Wrapper Lite uses Hz; the legacy adapter uses kHz.
    }


def _apple_url(value):
    invalid = "请输入 Apple Music 中复制的歌曲、专辑或歌单链接。"
    try:
        url = urlsplit(value.strip())
    except (AttributeError, ValueError):
        raise ValueError(invalid) from None
    route = re.fullmatch(r"/(?:[a-z]{2}/)?(song|album|playlist)/[^/\s]+(?:/[^/\s]+)?/?", url.path)
    if url.scheme != "https" or url.netloc.casefold() != "music.apple.com" or route is None:
        raise ValueError(invalid)
    song = parse_qs(url.query, keep_blank_values=True).get("i")
    if route.group(1) == "album" and song:
        if len(song) != 1 or not re.fullmatch(r"[0-9]+", song[0]):
            raise ValueError(invalid)
        return url._replace(query=f"i={song[0]}", fragment="").geturl()
    return url._replace(fragment="").geturl()


def _read_events(process, payload, queue):
    try:
        try:
            with process.stdin:
                process.stdin.write(payload)
        except OSError:
            pass
        with process.stdout:
            for line in process.stdout:
                queue.put(line)
    except OSError:
        pass
    finally:
        queue.put(None)


def _open_service_ports():
    ports = set()
    for port in SERVICE_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=POLL_INTERVAL):
                ports.add(port)
        except OSError:
            continue
    return ports


class Engine:
    def __init__(self, root: Path, backend_home: Path):
        self.root = Path(root).resolve()
        self.backend_home = Path(backend_home).resolve()
        self.ffmpeg = self.backend_home / "tools/ffmpeg.exe"
        self._service = None
        self._service_lock = threading.Lock()

    def read_config(self) -> dict:
        """Import only Apple Music account settings from a local apmyx installation."""
        local_appdata = os.environ.get("LOCALAPPDATA")
        candidates = [self.backend_home / "config.yaml"]
        if local_appdata:
            candidates.append(Path(local_appdata) / "Programs/apmyx/config.yaml")
        path = next((path for path in candidates if path.is_file()), None)
        if path is None:
            return {}
        try:
            import yaml

            raw = path.read_bytes()
            try:
                contents = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                contents = raw.decode("gb18030")
            try:
                config = yaml.safe_load(contents)
            except yaml.YAMLError:
                raise ValueError("invalid configuration") from None
        except (ImportError, OSError, UnicodeError, ValueError):
            raise RuntimeError("无法读取 apmyx 登录配置，请在 amdl 的账户页面登录。") from None
        if not isinstance(config, dict):
            return {}
        return {
            key: config[key].strip()
            for key in ("media-user-token", "storefront", "language")
            if isinstance(config.get(key), str) and config[key].strip()
        }

    def service_ready(self) -> bool:
        return _open_service_ports() == set(SERVICE_PORTS)

    def _service_command(self, selected):
        if selected.strip():
            path = Path(selected.strip().strip('"')).expanduser()
            if not path.is_absolute() or path.suffix.casefold() != ".exe" or not path.is_file():
                raise ValueError("请选择本地服务的可执行文件（.exe）。")
            return [str(path)], path.parent

        for base in (self.root, self.root / "runtime", self.root.parent):
            launcher = base / "wsl1/LxRunOffline.exe"
            if launcher.is_file():
                return [str(launcher), "r", "-n", "u22-amdl", "-c", "cd && ./wrapper"], launcher.parent
        wsl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/wsl.exe"
        if wsl.is_file():
            return [str(wsl), "-d", "u22-amdl", "--", "sh", "-lc", "cd && ./wrapper"], self.root
        raise RuntimeError("找不到本地音频服务。请安装 amdl 组件或在设置中选择服务程序。")

    def prepare_service(self, command, stop, on_status) -> None:
        with self._service_lock:
            _check_cancelled(stop)
            ports = _open_service_ports()
            if ports == set(SERVICE_PORTS):
                on_status("本地音频服务已启动。")
                return
            if not ports and (self._service is None or self._service.poll() is not None):
                args, cwd = self._service_command(command or "")
                on_status("正在启动本地音频服务…")
                try:
                    self._service = subprocess.Popen(
                        args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW,
                    )
                except OSError:
                    raise RuntimeError("无法启动本地音频服务，请检查组件安装和设置。") from None

            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                _check_cancelled(stop)
                if self.service_ready():
                    on_status("本地音频服务已启动。")
                    return
                if self._service is not None and self._service.poll() is not None and not ports:
                    break
                stop.wait(POLL_INTERVAL)
            raise RuntimeError("本地音频服务未就绪。请检查 WSL 的 u22-amdl 环境，或在设置中选择服务程序。")

    def close_service(self) -> None:
        """Stop only a service process launched by this Engine instance."""
        with self._service_lock:
            if self._service is not None:
                _end_process(self._service)
                self._service = None

    def _child_environment(self, wrapper):
        env = dict(os.environ)
        tools = [self.backend_home / "tools", self.root / "wsl1/deps-win", self.root.parent / "wsl1/deps-win"]
        env["PATH"] = os.pathsep.join([*(str(path) for path in tools if path.is_dir()), env.get("PATH", "")])
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if wrapper == "applemusicdecrypt":
            bypass = ",".join(value for value in (env.get("no_proxy"), env.get("NO_PROXY"), "127.0.0.1") if value)
            env.update(NO_PROXY=bypass, no_proxy=bypass)
        return env

    def _request(self, operation, settings, url, quality, work, stop, on_event):
        _check_cancelled(stop)
        work.mkdir()
        request_id = uuid4().hex
        wrapper = settings["wrapper"]
        payload = json.dumps({
            "version": 1, "request_id": request_id, "operation": operation,
            "wrapper": wrapper, "quality": quality, "settings": settings,
            "url": url, "work_dir": str(work),
        }, allow_nan=False)
        secrets = _settings_secrets(settings)
        try:
            process = subprocess.Popen(
                [str(self.backend_home / BRIDGE)], cwd=self.backend_home, env=self._child_environment(wrapper),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
            )
        except OSError:
            raise RuntimeError("找不到或无法启动下载组件，请运行 amdl 安装程序。") from None

        queue = Queue()
        reader = threading.Thread(target=_read_events, args=(process, payload, queue), daemon=True)
        reader.start()
        metadata, complete, failure = None, None, None
        artifacts = []
        track_errors = 0
        limit = time.monotonic() + (180 if operation == "probe" else 3 * 60 * 60)
        idle_limit = time.monotonic() + 240
        try:
            while True:
                _check_cancelled(stop)
                if time.monotonic() >= min(limit, idle_limit):
                    raise RuntimeError("下载组件响应超时，请检查网络或本地服务。")
                try:
                    line = queue.get(timeout=POLL_INTERVAL)
                except Empty:
                    if process.poll() is not None:
                        idle_limit = min(idle_limit, time.monotonic() + 5)
                    continue
                if line is None:
                    while process.poll() is None:
                        _check_cancelled(stop)
                        if time.monotonic() >= min(limit, idle_limit):
                            raise RuntimeError("下载组件未正常退出。")
                        stop.wait(POLL_INTERVAL)
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("request_id") != request_id:
                    continue
                idle_limit = time.monotonic() + 240
                kind, data = event.get("event"), event.get("data")
                if kind == "metadata" and isinstance(data, dict):
                    metadata = data
                elif kind == "artifact" and isinstance(data, dict):
                    artifacts.append(data)
                elif kind == "complete":
                    complete = event
                elif kind == "error":
                    code = event.get("code")
                    failure = ERRORS.get(code) if isinstance(code, str) else None
                elif kind == "progress" and isinstance(data, dict):
                    if wrapper == "applemusicdecrypt" and data.get("type") == "track_error":
                        track_errors += 1
                    public = {key: _safe_text(data.get(key), secrets) for key in ("phase", "name", "track_id")}
                    for key, alias in (("percent", "percent"), ("phase_percent", "phase_percent"),
                                       ("tracknum", "track_num"), ("totaltracks", "total_tracks")):
                        value = data.get(key, data.get(alias))
                        if type(value) in (int, float):
                            public[key] = value
                    on_event({"event": "progress", "data": public})
        finally:
            _end_process(process)
            reader.join(timeout=1)

        success = complete is not None and complete.get("success") is True and process.returncode == 0
        if operation == "probe" and (not success or metadata is None):
            raise RuntimeError(failure or "无法读取曲目信息，请检查链接、地区和 Apple Music 登录。")
        if operation == "download" and (complete is None or not artifacts):
            raise RuntimeError(failure or "下载没有返回歌曲，请检查 Apple Music 登录、网络和所选音质。")
        counts = complete.get("result") or {}
        errors = sum(counts.get(key, 0) for key in ("Error", "Unavailable", "NotSong")
                     if isinstance(counts, dict) and type(counts.get(key)) is int and counts[key] > 0)
        errors = max(errors, track_errors)
        if not success:
            errors = max(1, errors)
            on_event({"event": "warning", "message": failure or "有部分曲目未能下载。"})
        return metadata, artifacts, errors

    def _measure(self, path, stop):
        _check_cancelled(stop)
        if not self.ffmpeg.is_file():
            raise RuntimeError("找不到 FFmpeg，请重新安装 amdl 组件。")
        try:
            process = subprocess.Popen(
                [str(self.ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-xerror", "-loglevel", "info",
                 "-progress", "pipe:1", "-i", str(path), "-map", "0:a:0", "-c:a", "copy", "-f", "null", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW,
            )
        except OSError:
            raise RuntimeError("无法启动 FFmpeg 验证下载文件。") from None
        deadline = time.monotonic() + 90
        try:
            while True:
                _check_cancelled(stop)
                if time.monotonic() >= deadline:
                    raise RuntimeError("验证音频时长超时。")
                try:
                    progress, report = process.communicate(timeout=POLL_INTERVAL)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            _end_process(process)

        stream = re.search(
            r"^\s*Stream #0:\d+[^:\r\n]*: Audio:\s*(\w+)(?:\s+\(([^)]*)\))?",
            report.partition("Output #0")[0], re.M,
        )
        times = re.findall(r"^out_time_us=(\d+)$", progress, re.M)
        if process.returncode or not stream or not times:
            raise RuntimeError("无法验证下载音频的格式和时长。")
        return stream[1].casefold(), (stream[2] or "").casefold(), int(times[-1]) / 1_000_000

    def _verified_artifacts(self, candidates, metadata, output, quality, stop, on_event, secrets=()):
        tracks = {}
        for row in metadata.get("tracks", []):
            if not isinstance(row, dict) or not isinstance(row.get("trackData"), dict):
                continue
            data = row["trackData"]
            attrs = data.get("attributes")
            if not isinstance(attrs, dict):
                continue
            duration_ms = attrs.get("durationInMillis")
            if data.get("id") and type(duration_ms) is int and duration_ms > 0:
                tracks[str(data["id"])] = attrs

        found, errors, seen = [], 0, set()
        for artifact in candidates:
            _check_cancelled(stop)
            track_id = str(artifact.get("track_id") or "")
            attrs = tracks.get(track_id, {})
            name = _safe_text(attrs.get("name"), secrets) or "歌曲"
            expected = attrs.get("durationInMillis", 0) / 1000
            try:
                path = (output / str(artifact.get("path") or "")).resolve()
                if path in seen:
                    continue
                seen.add(path)
                if not path.is_relative_to(output) or not path.is_file():
                    raise RuntimeError("文件不在有效的下载目录中。")
                if expected <= 0:
                    raise RuntimeError("找不到 Apple Music 曲目时长。")
                codec, profile, duration = self._measure(path, stop)
                if codec != CODECS[quality] or (quality == "AAC" and profile != "lc"):
                    raise RuntimeError("实际音频编码与所选音质不同。")
                if abs(duration - expected) > max(1.2, min(3, expected * 0.015)):
                    raise RuntimeError(f"文件时长 {duration:.1f} 秒，曲目应为 {expected:.1f} 秒。请检查账号或服务登录。")
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                errors += 1
                on_event({"event": "warning", "message": f"{name}：{exc}"})
                continue

            found.append({
                "path": str(path), "track_id": track_id, "duration": duration,
                "expected_duration": expected, "codec": codec, "title": name,
                "artist": _safe_text(attrs.get("artistName"), secrets),
                "album": _safe_text(attrs.get("albumName"), secrets),
            })
        return found, errors

    def download(self, config, url, quality, stage, stop, on_event) -> dict:
        """Download one Apple Music link into a caller-owned temporary directory."""
        _check_cancelled(stop)
        quality = quality.upper()
        if quality == "AAC-LC":
            quality = "AAC"
        if quality not in CODECS:
            raise ValueError("请选择 AAC-LC、ALAC 或杜比全景声。")
        url = _apple_url(url)
        wrapper = _wrapper_settings(config)
        lite = wrapper["wrapper"] == "applemusicdecrypt"
        storefront = (config.get("applemusicdecrypt-storefront") if lite else None) or config.get("storefront")
        if lite and not storefront:
            url_region = urlsplit(url).path.strip("/").split("/", 1)[0]
            if re.fullmatch(r"[a-z]{2}", url_region):
                storefront = url_region
        storefront = storefront or "us"
        if not isinstance(storefront, str) or not re.fullmatch(r"[a-z]{2}", storefront.strip().lower()):
            raise ValueError("Apple Music 地区应使用两位字母代码，例如 cn 或 au。")
        storefront = storefront.strip().lower()
        stage = Path(stage).resolve(strict=True)
        output = stage / "files"
        output.mkdir()
        settings = {
            **wrapper, "storefront": storefront,
            "language": str(config.get("language") or "en-US"),
            "get-m3u8-from-device": False, "aac-type": "aac-lc",
            "decrypt-m3u8-port": "127.0.0.1:10020", "get-m3u8-port": "127.0.0.1:20020",
            "atmos-max": 2768, "alac-fix": True,
            "aac-save-folder": str(output), "alac-save-folder": str(output),
            "atmos-save-folder": str(output), "mv-save-folder": str(output),
            "artist-folder-format": "", "album-folder-format": "", "playlist-folder-format": "",
            "song-file-format": "{SongId} {SongName}", "playlist-song-file-format": "{SongId} {SongName}",
            "save-cover-file": False, "save-artist-cover": False, "save-lrc-file": False,
        }
        if lite:
            settings["applemusicdecrypt-storefront"] = storefront
        on_event({"event": "progress", "data": {"phase": "正在读取曲目信息"}})
        metadata, _, _ = self._request("probe", settings, url, quality, stage / "probe", stop, on_event)
        settings["get-m3u8-from-device"] = not lite and quality != "AAC"
        _, candidates, errors = self._request("download", settings, url, quality, stage / "work", stop, on_event)
        on_event({"event": "progress", "data": {"phase": "正在验证歌曲"}})
        artifacts, rejected = self._verified_artifacts(candidates, metadata, output, quality, stop, on_event,
                                                       _settings_secrets(settings))
        return {"artifacts": artifacts, "errors": errors + rejected}
