"""Publish verified audio to a selected folder without replacing existing files."""

import ctypes
import hashlib
import itertools
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]')
_DEVICE = re.compile(
    r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])",
    re.IGNORECASE,
)
_AUDIO_EXTENSIONS = {".m4a", ".flac", ".mp3", ".wav", ".opus"}


def clean_name(text, limit=80):
    """Make a Windows-safe filename component without adding an extension."""
    if limit < 1:
        raise ValueError("文件名长度限制必须大于零。")
    name = _UNSAFE.sub("_", "" if text is None else str(text)).strip(" .")
    # Windows counts UTF-16 code units, including two for supplementary characters.
    name = name.encode("utf-16-le")[: limit * 2].decode("utf-16-le", "ignore").rstrip(" .")
    if not name:
        return "Unknown"[:limit]
    if _DEVICE.fullmatch(name.split(".", 1)[0].rstrip(" ")):
        return ("_" + name).encode("utf-16-le")[: limit * 2].decode("utf-16-le", "ignore").rstrip(" .")
    return name


def _plain_directory(path):
    for directory in (*reversed(path.parents), path):
        info = directory.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"保存路径必须是普通目录：{directory}")


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _absolute(path):
    if not path:
        raise ValueError("请先选择保存目录。")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("请选择完整的保存路径。")
    return path


def destination_for(folder, category=""):
    """Return the selected folder or one direct child for a named category."""
    folder = _absolute(folder)
    try:
        _plain_directory(folder)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"找不到保存目录：{folder}") from error
    if category == "":
        return folder
    if not isinstance(category, str) or category != clean_name(category, 120):
        raise ValueError("请填写一个有效的分类文件夹名。")
    destination = folder / category
    try:
        _plain_directory(destination)
    except FileNotFoundError:
        pass
    return destination


def volume_identity(path):
    """Identify the mounted volume of an existing path."""
    path = _absolute(path)
    info = path.stat()
    if os.name != "nt":
        return info.st_dev
    if path.drive.startswith("\\\\"):
        return (path.anchor.casefold(), info.st_dev)

    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_root = kernel.GetVolumePathNameW
    get_root.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_root.restype = wintypes.BOOL
    root = ctypes.create_unicode_buffer(32768)
    if not get_root(str(path), root, len(root)):
        raise ctypes.WinError(ctypes.get_last_error())

    get_name = kernel.GetVolumeNameForVolumeMountPointW
    get_name.argtypes = get_root.argtypes
    get_name.restype = wintypes.BOOL
    name = ctypes.create_unicode_buffer(128)
    mount = name.value if get_name(root.value, name, len(name)) else root.value
    return (mount.casefold(), info.st_dev)


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _names(stem, track_id, extension):
    yield f"{stem}{extension}"
    suffix = clean_name(track_id, 32) if track_id else ""
    if suffix:
        yield f"{stem} [{suffix}]{extension}"
    for number in itertools.count(2):
        label = f"{suffix}-{number}" if suffix else str(number)
        yield f"{stem} [{label}]{extension}"


def publish(source, destination, stem, track_id, *, expected_volume=None):
    """Return (path, newly_written) after copying an already verified audio file."""
    source = Path(source)
    source_info = source.lstat()
    extension = source.suffix.lower()
    if extension not in _AUDIO_EXTENSIONS or _is_link(source_info) or not stat.S_ISREG(source_info.st_mode) or source_info.st_size == 0:
        raise ValueError(f"下载文件无效：{source}")

    destination = _absolute(destination)
    if not destination.is_dir():
        destination_for(destination.parent, destination.name)
    volume_base = destination if destination == destination.parent else destination.parent
    if expected_volume is None:
        expected_volume = volume_identity(volume_base)

    def check_volume():
        _plain_directory(volume_base)
        if volume_identity(volume_base) != expected_volume:
            raise OSError("下载盘已更换，请重新选择保存位置。")

    check_volume()
    destination.mkdir(exist_ok=True)
    _plain_directory(destination)
    source_digest = None
    temporary = None
    try:
        for name in _names(clean_name(stem, 155), track_id, extension):
            check_volume()
            _plain_directory(destination)
            target = destination / name
            try:
                existing = target.lstat()
            except FileNotFoundError:
                existing = None
            while existing is None:
                if temporary is None:
                    with tempfile.NamedTemporaryFile(
                        dir=destination, prefix=".download-", suffix=".part", delete=False,
                    ) as output:
                        temporary = Path(output.name)
                        with source.open("rb") as audio:
                            shutil.copyfileobj(audio, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                check_volume()
                _plain_directory(destination)
                try:
                    if os.name == "nt":
                        temporary.rename(target)
                    else:
                        os.link(temporary, target)
                    return target, True
                except FileExistsError:
                    try:
                        existing = target.lstat()
                    except FileNotFoundError:
                        continue
            if (
                stat.S_ISREG(existing.st_mode)
                and not _is_link(existing)
                and existing.st_size == source_info.st_size
            ):
                if source_digest is None:
                    source_digest = _digest(temporary or source)
                if _digest(target) == source_digest:
                    return target, False
    finally:
        if temporary is not None:
            try:
                check_volume()
                _plain_directory(destination)
                temporary.unlink(missing_ok=True)
            except (OSError, ValueError):
                # The disk may have been disconnected or changed during the copy.
                pass
