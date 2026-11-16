"""Resolve Apple Music links and execute downloads from the shared queue."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1
from mutagen.mp4 import MP4

import audio
from bili_catalog import VIDEO_BASE, clean_song_title, parse_video_link, resolve_video, suggest_folder
from bili_download import download_video
from bili_resume import forget_resume
from bili_sidecars import save_cover, save_danmaku, save_subtitle
from engine import Cancelled
from local_convert import convert_file
from netease_download import download_netease
from qq_download import download_qq
import settings
from storage import clean_name, destination_for, download_cache, publish, volume_identity
from youtube_download import download_youtube


SOURCE = "apple_music"
OPTION_FIELDS = ("folder", "category", "quality", "output", "mp3_bitrate")


def _link_parts(parsed, region=""):
    parts = parsed.path.strip("/").split("/")
    if parts and re.fullmatch(r"[a-z]{2}", parts[0], re.I):
        region = parts.pop(0).lower()
    if (len(parts) not in (2, 3) or parts[0] not in ("song", "album", "playlist")
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", parts[-1])
            or parts[-1] in (".", "..")):
        raise ValueError()
    return region, parts[0], parts[-1], unquote(parts[1]).replace("-", " ") if len(parts) == 3 else ""


def read_links(text):
    result = []
    for row, raw in enumerate(text.splitlines(), 1):
        link = raw.strip()
        if not link:
            continue
        try:
            parsed = urlsplit(link)
            _, kind, _, _ = _link_parts(parsed)
            if (parsed.scheme != "https" or parsed.netloc.lower() != "music.apple.com"
                    or any(char.isspace() or ord(char) < 32 or char == "\\" for char in link)):
                raise ValueError()
            tracks = parse_qs(parsed.query, keep_blank_values=True).get("i", []) if kind == "album" else []
            if tracks and (len(tracks) != 1 or not re.fullmatch(r"[0-9]+", tracks[0])):
                raise ValueError()
            query = f"i={tracks[0]}" if tracks else ""
            result.append(parsed._replace(netloc="music.apple.com", query=query, fragment="").geturl())
        except ValueError:
            raise ValueError(f"第 {row} 行不是有效的 Apple Music 歌曲、专辑或歌单链接。") from None
    if not result:
        raise ValueError("请至少粘贴一个 Apple Music 链接。")
    return list(dict.fromkeys(result))


def expand_link(url, catalog, default_region):
    parsed = urlsplit(url)
    region, kind, identifier, title = _link_parts(parsed, default_region)
    if kind == "song" or (kind == "album" and parsed.query):
        return [{"url": url, "title": title or url}]

    rows = catalog.tracks({"id": identifier, "type": kind}, region)
    if not rows:
        raise ValueError(f"{title or identifier} 没有可加入队列的曲目。")
    batch_key = f"catalog:{region}:{kind}:{identifier}"
    return [dict(row, batch_key=batch_key, batch_title=title or identifier,
                 position=position, total=len(rows))
            for position, row in enumerate(rows, 1)]


def _volume_key(path):
    identity = volume_identity(Path(path))
    return list(identity) if isinstance(identity, tuple) else identity


def download_options(preferences):
    destination_for(preferences["folder"], preferences["category"])
    return {**{key: preferences[key] for key in OPTION_FIELDS},
            "bili_mode": preferences.get("bili_mode", "video"),
            "bili_danmaku": preferences.get("bili_danmaku", False),
            "bili_subtitle": preferences.get("bili_subtitle", False),
            "bili_archive": preferences.get("bili_archive", False),
            "bili_auto_category": preferences.get("bili_auto_category", True),
            "bili_category_rules": 2,
            "youtube_mode": preferences.get("youtube_mode", "video"),
            "volume": _volume_key(preferences["folder"])}


def task_category(task):
    category = task.options["category"]
    if category or task.source != "bilibili" or not task.options.get("bili_auto_category", True):
        return category
    label = ""
    if task.options.get("bili_category_rules", 1) == 2:
        parent = ""
        try:
            video_id, _ = parse_video_link(task.url)
            upload = f"bilibili:{video_id}"
            if task.batch_key == upload or task.batch_key.startswith(upload + ":repeat:"):
                parent = task.batch_title
        except ValueError:
            pass
        label = suggest_folder(task.title, task.source_category, task.media_category, parent_title=parent)
    if not label:
        label = "音乐" if task.media_category == "music" else task.source_category or "B 站"
    return clean_name(label, 120)


def _target(task):
    options = task.options
    folder = options["folder"]
    destination = destination_for(folder, task_category(task))
    identity = _volume_key(folder)
    if identity != options["volume"]:
        raise OSError("原保存磁盘尚未连接。请接回原磁盘后继续。")
    return destination, tuple(identity) if isinstance(identity, list) else identity


def _account_config(engine, login, preferences, quality, stop, post):
    if preferences.get("native_account"):
        login.start(stop, lambda status: post("status", status))
        if not login.status()["logged_in"]:
            raise ValueError("Apple Music 登录已过期。请在「账户」重新登录。")
        config = login.engine_config()
        region = config.get("applemusicdecrypt-storefront") or preferences["region"]
        return {**config, "storefront": region, "applemusicdecrypt-storefront": region}

    token = settings.session(preferences)
    if not token:
        raise ValueError("请先在「账户」登录 Apple Music。")
    if quality in ("ALAC", "ATMOS"):
        engine.prepare_service(preferences["wrapper"], stop, lambda status: post("status", status))
    return {"media-user-token": token, "storefront": preferences["region"]}


def _record(store, task, post, state, *, error="", files=None, audio_details=None):
    current = store.set_state(task.id, state, error=error, files=files, audio_details=audio_details)
    if current is None:
        raise RuntimeError("找不到当前下载任务。")
    post("queue_task", current)


def _audio_result(path, filename, original=None, *, preserved=False):
    output = audio.audio_details(path)
    if output is None and original is None:
        return []
    return [{"filename": filename, "source": output if preserved and original is None else original, "output": output}]


def _download_task(store, task, target, disk, config, engine, stop, post):
    prefix = f"{task.position}/{task.total} · {task.title}" if task.position and task.total else task.title
    options = task.options
    files = []
    details = []
    saved = existing = 0
    last_error = ""

    def on_event(event):
        if event.get("event") == "progress":
            data = event.get("data", {})
            label = data.get("name") or data.get("phase")
            if label:
                post("status", prefix + " · " + str(label)[:75])
        elif event.get("event") == "warning":
            post("log", str(event.get("message", "")))

    post("status", prefix + " · 正在读取曲目")
    _record(store, task, post, "running")
    with tempfile.TemporaryDirectory(prefix="amdl-") as working:
        stage = Path(working)
        result = engine.download(config, task.url, options["quality"], stage, stop, on_event)
        errors = result["errors"]
        for artifact in result["artifacts"]:
            if stop.is_set():
                raise Cancelled()
            try:
                post("status", prefix + " · 正在检查并保存音频")
                source, stem, original = audio.prepare(engine.ffmpeg, stage, artifact, options["quality"],
                                                       options["output"], options["mp3_bitrate"], stop)
                published, written = publish(source, target, stem, artifact.get("track_id"), expected_volume=disk)
            except (Cancelled, audio.Cancelled):
                raise Cancelled() from None
            except (OSError, RuntimeError, ValueError) as error:
                last_error = str(error)
                errors += 1
                post("log", f"{prefix} · {last_error}")
                continue
            saved += int(written)
            existing += int(not written)
            files.append(str(published))
            details.extend(_audio_result(source, published.name, original))
            _record(store, task, post, "running", files=files, audio_details=details)
            post("log", ("已保存  " if written else "已存在  ") + published.name)

    if not files or errors:
        _record(store, task, post, "failed", error=last_error or "曲目未完整下载。", files=files, audio_details=details)
        return saved, existing, max(1, errors)
    _record(store, task, post, "complete", files=files, audio_details=details)
    return saved, existing, 0


def _refresh_bili_metadata(store, task, session, post):
    if task.audio_title is not None:
        return task
    post("status", f"正在读取 B 站作品信息 · {task.title}")
    try:
        row = resolve_video(task.url, session=session)["rows"][0]
    except (OSError, RuntimeError, ValueError):
        return task
    current = store.set_bilibili_metadata(task.id, row)
    if current is not None:
        post("queue_task", current)
        return current
    return task


def _write_bili_tags(path, title, artist):
    if path.suffix.casefold() == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=[title]))
        if artist:
            tags.add(TPE1(encoding=3, text=[artist]))
        tags.save(path)
    elif path.suffix.casefold() == ".flac":
        tags = FLAC(path)
        tags["title"] = title
        if artist:
            tags["artist"] = artist
        tags.save()
    else:
        tags = MP4(path)
        tags["\xa9nam"] = [title]
        if artist:
            tags["\xa9ART"] = [artist]
        tags.save()


def _bili_checkpoint(store, task, disk, *, create=False):
    namespace = sha256(os.fsencode(os.path.normcase(store.path.absolute()))).hexdigest()[:16]
    name = f"bili-{namespace}-{task.id}"
    return download_cache(task.options["folder"], name, disk, create=create)


def forget_bilibili_cache(store, task):
    if task.source != "bilibili" or task.options is None:
        return
    volume = task.options["volume"]
    disk = tuple(volume) if isinstance(volume, list) else volume
    try:
        checkpoint, check_volume = _bili_checkpoint(store, task, disk)
        forget_resume(checkpoint, check_volume=check_volume)
    except (OSError, ValueError):
        pass


def _save_bili_metadata(url, stage, stop, on_status, *, task, media, song_title):
    if stop.is_set():
        raise Cancelled()
    on_status("正在保存作品资料…")
    video_id, page = parse_video_link(url)
    metadata = {
        "version": 1,
        "source": "bilibili",
        "url": f"{VIDEO_BASE}{video_id}?p={page or 1}",
        "video_id": video_id,
        "part": page or 1,
        "video_title": task.title,
        "song_title": song_title,
        "artist": task.artist,
        "uploader": task.uploader,
        "partition": task.source_category,
        "duration_seconds": task.duration,
        "media_file": media.name,
    }
    path = stage / "metadata.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return path


def _download_bilibili(store, task, target, disk, engine, stop, post, session):
    prefix = f"{task.position}/{task.total} · {task.title}" if task.position and task.total else task.title
    _record(store, task, post, "running")
    status = lambda message: post("status", f"{prefix} · {message}")
    files = []
    saved = existing = 0
    attachment_errors = []
    mode = task.options.get("bili_mode", "video")
    song = ""
    if mode in ("audio", "mp3", "flac"):
        song = clean_song_title(task.title) if task.audio_title is None else task.audio_title

    def save(source, name):
        nonlocal saved, existing
        published, written = publish(source, target, name, str(task.id), expected_volume=disk)
        saved += int(written)
        existing += int(not written)
        files.append(str(published))
        post("log", ("已保存  " if written else "已存在  ") + published.name)
        return published

    checkpoint, check_volume = _bili_checkpoint(store, task, disk, create=True)
    with tempfile.TemporaryDirectory(prefix="work-", dir=checkpoint) as working:
        stage = Path(working)
        source = download_video(task.url, mode, stage, engine.ffmpeg, stop, status, session=session,
                                checkpoint=checkpoint, check_volume=check_volume)
        if stop.is_set():
            raise Cancelled()
        video_id, page = parse_video_link(task.url)
        multipart = task.total and task.total > 1 and task.batch_key.startswith(f"bilibili:{video_id}")
        name = f"{task.title} [P{page}]" if multipart and page is not None else task.title
        if song:
            artist = task.artist
            name = f"{artist} - {song}" if artist else song
            try:
                _write_bili_tags(source, song, artist)
            except (MutagenError, OSError, ValueError):
                post("log", f"{song} · 无法写入音频标签，仍会按识别出的歌名保存。")
        published = save(source, name)
        details = _audio_result(source, published.name, preserved=mode != "mp3")
        for option, label, fetch, kwargs in (
            ("bili_danmaku", "弹幕", save_danmaku, {}),
            ("bili_subtitle", "人工字幕", save_subtitle, {"session": session}),
            ("bili_archive", "封面", save_cover, {}),
            ("bili_archive", "作品资料", _save_bili_metadata, {"task": task, "media": published, "song_title": song or task.audio_title}),
        ):
            if not task.options.get(option):
                continue
            _record(store, task, post, "running", files=files, audio_details=details)
            try:
                sidecar = fetch(task.url, stage, stop, status, **kwargs)
                if stop.is_set():
                    raise Cancelled()
                if sidecar is None:
                    post("log", f"{task.title} · B 站未提供{label}。")
                else:
                    save(sidecar, published.stem)
            except Cancelled:
                raise
            except (OSError, RuntimeError, ValueError) as error:
                message = f"媒体已保存，{label}保存失败 · {error}"
                attachment_errors.append(message)
                post("log", message)
    _record(store, task, post, "failed" if attachment_errors else "complete",
            error="\n".join(attachment_errors), files=files, audio_details=details)
    if not attachment_errors:
        forget_bilibili_cache(store, task)
    return saved, existing, int(bool(attachment_errors))


def _convert_local(store, task, target, disk, engine, stop, post):
    source = Path(task.url)
    info = source.stat()
    options = task.options
    if (info.st_size, info.st_mtime_ns) != (options["local_size"], options["local_mtime"]):
        raise ValueError("原文件已修改，请从「本地转码」重新添加文件。")
    _record(store, task, post, "running")
    post("status", f"正在转码 · {task.title}")
    original = audio.audio_details(source)
    published, written = convert_file(source, options["local_format"], engine.ffmpeg, target, stop,
                                      mp3_bitrate=options["mp3_bitrate"], expected_volume=disk)
    details = _audio_result(published, published.name, original)
    _record(store, task, post, "complete", files=[str(published)], audio_details=details)
    post("log", ("已保存  " if written else "已存在  ") + published.name)
    return int(written), int(not written), 0


def _download_web(store, task, target, disk, engine, stop, post, session):
    prefix = f"{task.position}/{task.total} · {task.title}" if task.position and task.total else task.title
    _record(store, task, post, "running")

    def status(message):
        post("status", f"{prefix} · {message}")

    with tempfile.TemporaryDirectory(prefix=f"amdl-{task.source}-") as working:
        stage = Path(working)
        name = task.title
        preserved = True
        if task.source == "youtube":
            mode = task.options.get("youtube_mode", "video")
            source = download_youtube(task.url, mode, stage, engine.ffmpeg, stop, status)
            preserved = mode != "mp3"
        else:
            download = download_qq if task.source == "qq" else download_netease
            source = download(task.url, stage, engine.ffmpeg, stop, status, session=session,
                              expected_duration=task.duration)
            if task.artist:
                name = f"{task.artist} - {name}"
        if stop.is_set():
            raise Cancelled()
        published, written = publish(source, target, name, str(task.id), expected_volume=disk)
        details = _audio_result(source, published.name, preserved=preserved)
    _record(store, task, post, "complete", files=[str(published)], audio_details=details)
    post("log", ("已保存  " if written else "已存在  ") + published.name)
    return int(written), int(not written), 0


def run_tasks(store, tasks, preferences, engine, login, stop, post, *, sessions=None):
    """Process tracks sequentially and commit each outcome before notifying the UI."""
    saved = existing = errors = waiting = 0
    configs = {}
    account_errors = {}
    sessions = sessions or {}
    for task in tasks:
        if stop.is_set():
            break
        session = sessions.get(task.source)
        try:
            target, disk = _target(task)
            if task.source == "bilibili" and task.audio_title is None:
                updated = _refresh_bili_metadata(store, task, session, post)
                if task_category(updated) != task_category(task):
                    target, disk = _target(updated)
                task = updated
        except OSError as error:
            _record(store, task, post, "interrupted", error=str(error))
            waiting += 1
            continue
        except ValueError as error:
            _record(store, task, post, "failed", error=str(error))
            errors += 1
            continue

        try:
            if task.source == SOURCE:
                quality = task.options["quality"]
                if quality in account_errors:
                    raise RuntimeError(account_errors[quality])
                if quality not in configs:
                    try:
                        configs[quality] = _account_config(engine, login, preferences, quality, stop, post)
                    except Cancelled:
                        raise
                    except (OSError, RuntimeError, ValueError) as error:
                        account_errors[quality] = str(error)
                        raise
                added, reused, failed = _download_task(store, task, target, disk, configs[quality], engine, stop, post)
            elif task.source == "bilibili":
                added, reused, failed = _download_bilibili(store, task, target, disk, engine, stop, post, session)
            elif task.source in ("youtube", "netease", "qq"):
                added, reused, failed = _download_web(store, task, target, disk, engine, stop, post, session)
            elif task.source == "local":
                added, reused, failed = _convert_local(store, task, target, disk, engine, stop, post)
            else:
                raise ValueError("此来源尚未接入下载。")
            saved += added
            existing += reused
            errors += failed
        except Cancelled:
            _record(store, task, post, "interrupted")
            break
        except (OSError, RuntimeError, ValueError) as error:
            _record(store, task, post, "failed", error=str(error))
            post("log", f"{task.title} · {error}")
            errors += 1
    return saved, existing, errors, waiting
