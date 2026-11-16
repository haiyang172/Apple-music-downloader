"""Export completed audio downloads as an extended M3U8 playlist."""

import math
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile


_AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}


def _relative_entry(audio, directory):
    try:
        entry = Path(os.path.relpath(audio, directory)).as_posix()
    except ValueError:
        # On Windows relpath can't cross drive letters or network share roots.
        return str(audio)
    if entry.startswith("#") or entry[0].isspace() or ":" in entry.split("/", 1)[0]:
        return "./" + entry
    return entry


def _playlist_entries(tasks, directory):
    seen = set()
    for task in tasks:
        if task.state != "complete":
            continue
        duration = task.duration
        seconds = round(duration) if duration and duration > 0 and math.isfinite(duration) else -1
        title = " ".join((task.audio_title or task.title or "").replace("\0", "").split())
        artist = " ".join((task.artist or "").replace("\0", "").split())
        for value in task.files:
            audio = Path(value)
            if not audio.is_absolute() or audio.suffix.lower() not in _AUDIO_EXTENSIONS:
                continue
            entry = _relative_entry(audio, directory)
            if entry.splitlines() != [entry] or "\0" in entry:
                continue
            try:
                info = audio.stat()
            except (OSError, ValueError):
                continue
            if not stat.S_ISREG(info.st_mode) or not info.st_size:
                continue
            identity = (info.st_dev, info.st_ino) if info.st_ino else os.path.normcase(os.path.abspath(audio))
            if identity in seen:
                continue
            seen.add(identity)
            name = title or audio.stem
            label = f"{artist} - {name}" if artist else name
            yield f"#EXTINF:{seconds},{label}\n{entry}\n"


def save_m3u8(path, tasks):
    """Save an ordered playlist to a confirmed path and return the number of audio files."""
    destination = Path(path).absolute()
    tracks = list(_playlist_entries(tasks, destination.parent))
    if not tracks:
        raise ValueError("所选任务没有可加入歌单的本地音频。")

    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent,
                                prefix=".amdl-playlist-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("#EXTM3U\n")
            stream.writelines(tracks)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(tracks)
