"""Find and remove byte-identical duplicate audio files after explicit review.

scan_folder() walks a directory tree, fingerprints each supported audio file
and returns groups of files whose full contents are identical. delete_duplicates()
removes only the files the user marked, re-checking each fingerprint first so a
file that changed since the scan is never deleted.
"""

import hashlib
import os
from pathlib import Path

from engine import Cancelled

SUFFIXES = frozenset({".mp3", ".m4a", ".flac", ".wav", ".opus"})
_CHUNK = 1024 * 1024
_MAX_FILE = 4 * 1024 ** 3


def _stop(stop):
    if stop is not None and stop.is_set():
        raise Cancelled("已取消曲库去重。")


def fingerprint(path, stop=None):
    """Return the SHA256 of a file's full contents, or None if unusable."""
    path = Path(path)
    try:
        size = path.stat().st_size
        if not 0 < size <= _MAX_FILE:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            remaining = size
            while remaining:
                _stop(stop)
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def scan_folder(root, stop=None, on_status=None):
    """Return duplicate groups plus scan totals for a directory tree."""
    root = Path(root)
    by_hash = {}
    scanned = 0
    for directory, _subdirs, names in os.walk(root):
        for name in names:
            path = Path(directory) / name
            if path.suffix.lower() not in SUFFIXES:
                continue
            _stop(stop)
            value = fingerprint(path, stop)
            if value is None:
                continue
            scanned += 1
            by_hash.setdefault(value, []).append(path)
            if on_status is not None and scanned % 20 == 0:
                on_status(f"已扫描 {scanned} 个音频文件…")
    groups = []
    for value, paths in by_hash.items():
        if len(paths) > 1:
            size = paths[0].stat().st_size
            groups.append({"fingerprint": value, "size": size,
                           "files": [str(path) for path in sorted(paths, key=lambda p: str(p).lower())]})
    groups.sort(key=lambda group: (-group["size"], group["files"][0].lower()))
    duplicates = sum(len(group["files"]) - 1 for group in groups)
    return {"groups": groups, "scanned": scanned, "duplicates": duplicates}


def delete_duplicates(entries, stop=None, on_status=None):
    """Delete each (path, fingerprint) entry only while its contents are unchanged."""
    removed, failures = [], []
    for index, (path, expected) in enumerate(entries, 1):
        _stop(stop)
        path = Path(path)
        if on_status is not None:
            on_status(f"正在删除重复文件 · {index}/{len(entries)} · {path.name}")
        try:
            if fingerprint(path, stop) != expected:
                failures.append(f"{path.name}：文件已更改，未删除。")
                continue
            os.remove(path)
            removed.append(str(path))
        except OSError as error:
            failures.append(f"{path.name}：{error}")
    return removed, failures


def format_size(size):
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
