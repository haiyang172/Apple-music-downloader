"""Resolve and save accessible YouTube videos with the bundled worker."""

import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event
import time
from urllib.parse import parse_qs, urlsplit

from engine import Cancelled, _end_process


_ROOT = Path(__file__).resolve().parent
_BASE = "https://www.youtube.com"
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{2,120}")
_PLAYLIST_LIMIT = 500
_SEARCH_LIMIT = 500
_SEARCH_PAGE_SIZE = 50
_SEARCH_TEMPLATE = ("%(.{id,title,channel,uploader,availability,live_status,is_live,is_upcoming,age_limit,has_drm,"
                    "ie_key,duration,playlist_index})j")
_OUTPUT_LIMIT = 16 * 1024 * 1024
_ACCESS_ERROR = "YouTube 未提供可访问的完整视频。请在浏览器中确认链接。"
_LINK_ERROR = "请输入 YouTube 视频、Shorts 或播放列表链接。"
_FORMATS = {
    "video": (".mp4", ("-f", "bv[ext=mp4][vcodec^=avc]+ba[ext=m4a]/b[ext=mp4][vcodec^=avc]",
                        "--merge-output-format", "mp4")),
    "audio": (".m4a", ("-f", "ba[ext=m4a][acodec^=mp4a]")),
    "mp3": (".mp3", ("-f", "ba[ext=m4a][acodec^=mp4a]", "-x", "--audio-format", "mp3", "--audio-quality", "2")),
}


def _parse(value):
    if not isinstance(value, str):
        raise ValueError(_LINK_ERROR)
    value = value.strip()
    if 0 < len(value) <= 4096 and not any(char.isspace() or ord(char) < 33 or char in "\\\x7f" for char in value):
        try:
            target = urlsplit(value)
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=40)
            host = target.hostname
        except ValueError:
            host = None
        if host in _HOSTS and target.scheme == "https" and target.netloc.lower() == host:
            if host != "youtu.be" and target.path in ("/playlist", "/playlist/"):
                ids = query.get("list", ())
                if len(ids) == 1 and _PLAYLIST_ID.fullmatch(ids[0]):
                    return "playlist", ids[0], f"{_BASE}/playlist?list={ids[0]}"
            if host == "youtu.be":
                candidate = target.path.strip("/")
            elif target.path in ("/watch", "/watch/"):
                ids = query.get("v", ())
                candidate = ids[0] if len(ids) == 1 else ""
            else:
                match = re.fullmatch(r"/(?:shorts|live|embed|v)/([^/]+)/?", target.path)
                candidate = match[1] if match else ""
            if _VIDEO_ID.fullmatch(candidate):
                return "video", candidate, _watch(candidate)
    raise ValueError(_LINK_ERROR)


def _watch(video_id):
    return f"{_BASE}/watch?v={video_id}"


def _worker(*, search=False):
    installed = _ROOT / "backend" / "web"
    source = _ROOT / "installer" / ".tools"
    python = _ROOT / "runtime" / "python.exe"
    worker, runtime = installed / "yt-dlp.pyz", installed / "qjs.exe"
    if not worker.is_file() or not runtime.is_file():
        worker, runtime = source / "yt-dlp-2026.10.26.pyz", source / "qjs-v0.18.0-windows-x86_64.exe"
    if not worker.is_file() or not runtime.is_file():
        raise RuntimeError("YouTube 下载组件不完整，请运行新版安装程序。")
    if not python.is_file():
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
    extractors = "youtube:search" if search else "youtube,youtube:tab,youtube:playlist"
    return [str(python), "-I", "-S", str(worker),
            "--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--no-js-runtimes",
            "--js-runtimes", "quickjs:" + str(runtime), "--no-cache-dir", "--no-exec",
            "--use-extractors", extractors, "--encoding", "utf-8",
            "--socket-timeout", "10", "--retries", "1", "--extractor-retries", "1", "--fragment-retries", "1"]


def _stop(stop):
    if stop.is_set():
        raise Cancelled("已取消 YouTube 任务。")


def _public_error(stderr):
    stderr = stderr.lower()
    if any(text in stderr for text in (b"http error 429", b"http error 403", b"not a bot", b"sign in to confirm")):
        return "YouTube 暂时拒绝访问，请在浏览器中确认视频后重试。"
    if b"timed out" in stderr or b"unable to download webpage" in stderr:
        return "YouTube 请求失败，请检查网络后重试。"
    if b"requested format is not available" in stderr or b"only images are available" in stderr:
        return "YouTube 未提供所选格式，请换一种输出格式。"
    if b"ffmpeg" in stderr and b"not found" in stderr:
        return "无法找到 FFmpeg，请运行新版安装程序。"
    return _ACCESS_ERROR


def _run(arguments, directory, stop, timeout, on_status=None, *, search=False):
    _stop(stop)
    command = _worker(search=search) + list(arguments)
    with TemporaryDirectory(prefix="worker-", dir=directory) as temporary, TemporaryFile() as errors:
        log = Path(temporary) / "output"
        with log.open("wb") as output, log.open("rb") as reader:
            try:
                process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=errors,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                raise RuntimeError("无法启动 YouTube 下载组件。") from None
            with process:
                deadline = time.monotonic() + timeout
                pending = b""
                try:
                    while process.poll() is None:
                        if stop.wait(0.15):
                            _stop(stop)
                        if time.monotonic() >= deadline:
                            raise RuntimeError("YouTube 请求超时，请稍后重试。")
                        if log.stat().st_size > _OUTPUT_LIMIT:
                            raise RuntimeError("YouTube 返回的信息过大。")
                        if on_status is not None:
                            chunk = reader.read(65536)
                            lines = (pending + chunk).split(b"\n")
                            pending = lines.pop()
                            for line in lines:
                                match = re.fullmatch(rb"amdl\s+(\d+(?:\.\d+)?%)\s*", line)
                                if match:
                                    on_status("正在下载 YouTube · " + match[1].decode("ascii"))
                finally:
                    if process.poll() is None:
                        _end_process(process)
            _stop(stop)
            if process.returncode:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 65536))
                raise RuntimeError(_public_error(errors.read(65536)))
            reader.seek(0)
            raw = reader.read(_OUTPUT_LIMIT + 1)
    if len(raw) > _OUTPUT_LIMIT:
        raise RuntimeError("YouTube 返回的信息过大。")
    return raw


def _label(value, fallback=""):
    return " ".join(value.split())[:300] or fallback if isinstance(value, str) else fallback


def _eligible(item, *, flat=False):
    permitted = (None, "public", "unlisted") if flat else ("public", "unlisted")
    return (
        item.get("availability") in permitted
        and item.get("live_status") not in ("is_live", "is_upcoming", "post_live")
        and not item.get("is_live") and not item.get("is_upcoming") and not item.get("has_drm")
        and (item.get("age_limit") is None or item.get("age_limit") == 0)
        and item.get("title") not in ("[Private video]", "[Deleted video]")
    )


def _row(item, video_id, position=None, total=None, batch_key="", batch_title=""):
    duration = item.get("duration")
    return {
        "source": "youtube", "id": video_id, "url": _watch(video_id), "title": _label(item.get("title"), video_id),
        "artist": _label(item.get("uploader") or item.get("channel")), "position": position, "total": total,
        "duration": int(duration) if type(duration) in (int, float) and 0 < duration < 8640000 else 0,
        "batch_key": batch_key, "batch_title": batch_title,
    }


def _resolved(payload, kind, identifier):
    if not isinstance(payload, dict) or payload.get("id") != identifier:
        raise RuntimeError("YouTube 返回的内容与请求链接不符。")
    title = _label(payload.get("title"), identifier)
    if kind == "video":
        if not _eligible(payload) or payload.get("_type") == "playlist":
            raise RuntimeError(_ACCESS_ERROR)
        return {"title": title, "rows": [_row(payload, identifier)]}

    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("YouTube 未提供可读取的公开播放列表。")
    count = payload.get("playlist_count")
    total = count if type(count) is int and count >= len(entries) else len(entries)
    rows = []
    for position, entry in enumerate(entries[:_PLAYLIST_LIMIT], 1):
        if not isinstance(entry, dict) or not _eligible(entry, flat=True):
            continue
        video_id = entry.get("id")
        if isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id) and entry.get("ie_key") in (None, "Youtube"):
            rows.append(_row(entry, video_id, position, total, f"youtube:{identifier}", title))
    if not rows:
        raise RuntimeError("播放列表没有可加入队列的公开视频。")
    result = {"title": title, "rows": rows}
    warnings = []
    if total > _PLAYLIST_LIMIT:
        warnings.append(f"仅显示前 {_PLAYLIST_LIMIT} 项")
    skipped = min(_PLAYLIST_LIMIT, len(entries)) - len(rows)
    if skipped:
        warnings.append(f"已跳过 {skipped} 个不可用视频")
    if warnings:
        result["warning"] = " · ".join(warnings)
    return result


def resolve_youtube(value, stop=None):
    """Resolve a single YouTube video, or the first 500 entries in an explicit playlist."""
    kind, identifier, url = _parse(value)
    flags = ["--dump-single-json"]
    if kind == "playlist":
        flags.extend(("--yes-playlist", "--flat-playlist", "--playlist-items", f"1:{_PLAYLIST_LIMIT + 1}"))
    else:
        flags.append("--no-playlist")
    with TemporaryDirectory(prefix="amdl-youtube-") as temporary:
        raw = _run([*flags, "--", url], temporary, stop if stop is not None else Event(), 90)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("YouTube 返回的信息无法读取。") from None
    return _resolved(payload, kind, identifier)


def search_youtube(query, *, stop=None, offset=0, limit=50):
    """Search a bounded range of public YouTube video results."""
    query = " ".join(query.split()) if isinstance(query, str) else ""
    if not 0 < len(query) <= 120 or any(ord(char) < 32 or char == "\x7f" or "\ud800" <= char <= "\udfff" for char in query):
        raise ValueError("请输入 1–120 字的 YouTube 搜索关键词。")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= _SEARCH_PAGE_SIZE:
        raise ValueError("YouTube 搜索分页参数无效。")
    stop = stop if stop is not None else Event()
    _stop(stop)
    result = {"source": "youtube", "title": query, "rows": [], "total": None, "next_offset": None}
    if offset >= _SEARCH_LIMIT:
        return result

    end = min(offset + limit, _SEARCH_LIMIT)
    last = min(end + 1, _SEARCH_LIMIT)
    flags = ["--flat-playlist", "--skip-download", "--print", _SEARCH_TEMPLATE,
             "--playlist-items", f"{offset + 1}:{last}", "--", f"ytsearch{last}:{query}"]
    with TemporaryDirectory(prefix="amdl-youtube-search-") as temporary:
        raw = _run(flags, temporary, stop, 90, search=True)

    by_position = {}
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            raise RuntimeError("YouTube 返回的搜索结果无法读取。") from None
        if not isinstance(item, dict):
            continue
        position = item.get("playlist_index")
        if type(position) is not int or not offset < position <= last:
            continue
        if position > end:
            result["next_offset"] = end
            continue
        video_id = item.get("id")
        if (item.get("ie_key") == "Youtube" and isinstance(video_id, str) and _VIDEO_ID.fullmatch(video_id)
                and item.get("availability") != "unlisted"
                and _eligible(item, flat=True)):
            by_position.setdefault(position, _row(item, video_id, position))
    _stop(stop)
    result["rows"] = [by_position[position] for position in sorted(by_position)]
    return result


def download_youtube(url, mode, stage, ffmpeg, stop, on_status):
    """Download one canonical YouTube video and return a confined staged media path."""
    kind, identifier, url = _parse(url)
    if kind != "video":
        raise ValueError("请先展开 YouTube 播放列表并选择视频。")
    if mode not in _FORMATS:
        raise ValueError("不支持所选 YouTube 输出格式。")
    suffix, format_flags = _FORMATS[mode]
    stage = Path(stage).resolve(strict=True)
    target = stage / ("staged-media" + suffix)
    if target.exists() or target.is_symlink():
        raise RuntimeError("暂存目录内已存在输出文件。")
    on_status("正在读取 YouTube 播放信息…")
    with TemporaryDirectory(prefix="youtube-", dir=stage) as temporary:
        flags = ["--quiet", "--newline", "--progress", "--progress-delta", "0.5",
                 "--progress-template", "download:amdl %(progress._percent_str)s",
                 "--print", "after_move:%(filepath)j", "--no-playlist", "--age-limit", "0",
                 "--abort-on-unavailable-fragments", "--max-filesize", "4G",
                 "--ffmpeg-location", str(Path(ffmpeg).resolve(strict=True)),
                 "--output", "media.%(ext)s"]
        for availability in ("public", "unlisted"):
            criteria = f"id={identifier} & availability={availability} & !is_live & !is_upcoming & !has_drm & live_status!=?post_live"
            flags.extend(("--match-filters", criteria))
        raw = _run([*flags, *format_flags, "--", url], temporary, stop, 3600, on_status)
        expected = Path(temporary) / ("media" + suffix)
        paths = [line for line in raw.splitlines() if line.startswith(b'"')]
        if not paths:
            raise RuntimeError(_ACCESS_ERROR)
        try:
            reported = Path(json.loads(paths[0]))
            if not reported.is_absolute():
                reported = Path(temporary) / reported
            if (len(paths) != 1 or reported != expected or expected.is_symlink() or not expected.is_file()
                    or expected.stat().st_size == 0):
                raise ValueError
        except (OSError, TypeError, ValueError):
            raise RuntimeError("YouTube 未返回完整的暂存文件。") from None
        _stop(stop)
        expected.rename(target)
    return target
