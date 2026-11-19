"""Reversible, local-only audio tag batches; never transcode audio.

TagEditStore(directory) is cheap to construct. read_audio(path) returns six text
fields (title, artist, album, genre, date, tracknumber), path, duration, codec and
a SHA256 fingerprint. apply([{"path": path, "expected": fingerprint,
"changes": {field: text}}], stop=None, on_status=None) changes only named fields;
empty text deletes a field. expected is optional but recommended for previews.
undo(id, ...) restores exact original bytes. list_history() only lists records;
interrupted transactions are recovered by explicit apply/undo operations.
Call file-reading and transaction methods on a worker, not the UI thread.

Successful records contain id, state (applied/undone), created_at, updated_at,
items (path/changes/before/after), and error. Failed operations retain a journal
as cancelled, rolled_back, failed, or conflict. TagEditError and TagEditConflict
carry .record when available; engine.Cancelled is used for cancellation. Undo
is available for applied batches. Files changed externally are never forced
back. Recovery rolls an interrupted apply back, and finishes an interrupted
undo, only while verified file hashes match. Keep the history directory: its
full-file backups are required for exact undo after restarting the app.

Supported suffixes: .mp3, .m4a, .flac, .wav, .opus. Files, history and every
parent must be ordinary paths, without links/reparse points or shared hardlinks.
Dates accept YYYY, YYYY-MM, YYYY-MM-DD (ID3v2.3 requires year or full date).
Track numbers accept n or n/total, 1..65535. A lone n preserves an existing
total; explicit empty removes the track-number field. No existing unrelated
tags, lyrics, pictures, native codecs or encoded audio packets are replaced.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import threading
from uuid import uuid4

from mutagen import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import TIT2, TPE1, TALB, TCON, TDRC, TYER, TDAT, TRCK
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE

from engine import Cancelled
from storage import _is_link, _plain_directory


FIELDS = ("title", "artist", "album", "genre", "date", "tracknumber")
_TYPES = {".mp3": MP3, ".m4a": MP4, ".flac": FLAC, ".wav": WAVE, ".opus": OggOpus}
_ID3_FIELDS = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "genre": "TCON", "tracknumber": "TRCK"}
_ID3_CLASSES = {"title": TIT2, "artist": TPE1, "album": TALB, "genre": TCON, "tracknumber": TRCK}
_MP4_FIELDS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "genre": "\xa9gen", "date": "\xa9day", "tracknumber": "trkn"}
_STATES = {"preparing", "applying", "applied", "undoing", "undone", "cancelled", "rolled_back", "failed", "conflict"}
_HEX = re.compile(r"[a-f0-9]{64}")
_ID = re.compile(r"[a-f0-9]{32}")
_MAX_ITEMS = 200
_MAX_TEXT = 4096
_MAX_JOURNAL = 2 * 1024 * 1024
_MAX_METADATA = 32 * 1024 * 1024
_MAX_FILE = 8 * 1024 ** 3
_CHUNK = 1024 * 1024


class TagEditError(RuntimeError):
    """Invalid audio, unsafe paths, persistence or validation failure."""

    def __init__(self, message, record=None):
        super().__init__(message)
        self.record = record


class TagEditConflict(TagEditError):
    """The original/current file changed; no conflicting file was overwritten."""


def _stop(stop):
    if stop is not None and stop.is_set():
        raise Cancelled("已取消标签修改。")


def _notify(callback, text):
    if callback is not None:
        callback(text)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(value):
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise TagEditError("请选择完整的本地文件路径。")
    path = Path(os.path.abspath(value))
    if len(str(path)) > 32767 or any(ord(char) < 32 for char in str(path)):
        raise TagEditError("本地文件路径无效。")
    return path


def _regular(path):
    _plain_directory(path.parent)
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TagEditError("标签编辑只支持普通文件，不能使用链接或共享硬链接。")
    if not 0 < info.st_size <= _MAX_FILE:
        raise TagEditError("文件为空或超过 8 GB 标签编辑上限。")
    return info


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


@contextmanager
def _opened(path):
    before = _regular(path)
    with path.open("rb") as stream:
        if not os.path.samestat(before, os.fstat(stream.fileno())):
            raise TagEditConflict("文件已更改，请重新读取后再试。")
        yield stream, before
        after = _regular(path)
        if _identity(before) != _identity(after) or _identity(before) != _identity(os.fstat(stream.fileno())):
            raise TagEditConflict("读取期间文件已更改，请重新读取后再试。")


def _hash_stream(stream, stop=None, size=None):
    digest = hashlib.sha256()
    remaining = size
    while remaining is None or remaining:
        _stop(stop)
        chunk = stream.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
        if not chunk:
            if remaining:
                raise TagEditError("文件内容不完整。")
            break
        digest.update(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return digest.hexdigest()


def _hash(path, stop=None):
    with _opened(path) as (stream, _):
        return _hash_stream(stream, stop)


def _read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise TagEditError("音频容器不完整。")
    return data


def _payload(path, stop=None):
    """Hash native encoded payload, excluding only the container's editable tags."""
    digest = hashlib.sha256()
    suffix = path.suffix.lower()
    with _opened(path) as (stream, info):
        def region(size):
            digest.update(struct.pack(">Q", size))
            remaining = size
            while remaining:
                _stop(stop)
                chunk = _read_exact(stream, min(_CHUNK, remaining))
                digest.update(chunk)
                remaining -= len(chunk)

        if suffix == ".mp3":
            header = _read_exact(stream, min(10, info.st_size))
            start, end = 0, info.st_size
            if header[:3] == b"ID3":
                if len(header) != 10 or any(value & 128 for value in header[6:10]):
                    raise TagEditError("MP3 标签头无效。")
                start = 10 + sum(value << shift for value, shift in zip(header[6:10], (21, 14, 7, 0)))
                if start > _MAX_METADATA:
                    raise TagEditError("音频标签过大。")
            if end >= 128:
                stream.seek(-128, 2)
                if stream.read(3) == b"TAG":
                    end -= 128
            if not start < end:
                raise TagEditError("MP3 没有完整音频数据。")
            stream.seek(start)
            region(end - start)
        elif suffix == ".flac":
            if _read_exact(stream, 4) != b"fLaC":
                raise TagEditError("无法识别原生 FLAC 容器。")
            metadata = 0
            while True:
                block = _read_exact(stream, 4)
                length = int.from_bytes(block[1:], "big")
                metadata += length + 4
                if metadata > _MAX_METADATA:
                    raise TagEditError("音频标签过大。")
                data = _read_exact(stream, length)
                if block[0] & 127 == 0:
                    digest.update(data)  # STREAMINFO remains byte-for-byte native.
                if block[0] & 128:
                    break
            region(info.st_size - stream.tell())
        elif suffix == ".m4a":
            found = False
            while stream.tell() < info.st_size:
                position = stream.tell()
                length, kind = struct.unpack(">I4s", _read_exact(stream, 8))
                header_size = 8
                if length == 1:
                    length = struct.unpack(">Q", _read_exact(stream, 8))[0]
                    header_size = 16
                elif length == 0:
                    length = info.st_size - position
                if length < header_size or position + length > info.st_size:
                    raise TagEditError("M4A 容器长度无效。")
                if kind == b"moov" and length > _MAX_METADATA:
                    raise TagEditError("音频标签或索引过大。")
                if kind == b"mdat":
                    found = True
                    region(length - header_size)
                else:
                    stream.seek(position + length)
            if not found:
                raise TagEditError("M4A 缺少音频数据。")
        elif suffix == ".wav":
            header = _read_exact(stream, 12)
            if header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise TagEditError("只支持标准 RIFF/WAVE 音频。")
            found = False
            while stream.tell() + 8 <= info.st_size:
                kind, size = struct.unpack("<4sI", _read_exact(stream, 8))
                if stream.tell() + size > info.st_size:
                    raise TagEditError("WAV 容器长度无效。")
                if kind.lower() == b"id3 ":
                    if size > _MAX_METADATA:
                        raise TagEditError("音频标签过大。")
                    stream.seek(size, 1)
                else:
                    digest.update(kind)
                    region(size)
                    found |= kind == b"data"
                if size & 1:
                    _read_exact(stream, 1)
            if not found:
                raise TagEditError("WAV 缺少音频数据。")
        else:
            packet = bytearray()
            serial = None
            packets = 0
            while stream.tell() < info.st_size:
                header = _read_exact(stream, 27)
                if header[:5] != b"OggS\x00":
                    raise TagEditError("Opus 容器无效。")
                page_serial = header[14:18]
                if serial is None:
                    serial = page_serial
                if serial != page_serial:
                    raise TagEditError("暂不支持串接或多流 Opus 文件。")
                for size in _read_exact(stream, header[26]):
                    packet.extend(_read_exact(stream, size))
                    if len(packet) > _MAX_METADATA:
                        raise TagEditError("Opus 标签或数据包过大。")
                    if size < 255:
                        if packets != 1:
                            digest.update(struct.pack(">Q", len(packet)))
                            digest.update(packet)
                        elif not packet.startswith(b"OpusTags"):
                            raise TagEditError("Opus 标签包无效。")
                        if packets == 0 and not packet.startswith(b"OpusHead"):
                            raise TagEditError("音频的实际编码不是 Opus。")
                        packets += 1
                        packet.clear()
            if packet or packets < 3:
                raise TagEditError("Opus 音频数据不完整。")
    return digest.hexdigest()


def _load(path):
    container = _TYPES.get(path.suffix.lower())
    if container is None:
        raise TagEditError("只支持 MP3、M4A、FLAC、WAV 和 Opus 文件。")
    try:
        with _opened(path) as (stream, _):
            track = container(stream, translate=False) if container in (MP3, WAVE) else container(stream)
            if isinstance(track, MP3) and track.info.layer != 3:
                raise TagEditError("音频的实际编码不是 MP3。")
            if not math.isfinite(track.info.length) or track.info.length <= 0:
                raise TagEditError("无法读取有效的音频时长。")
            return track
    except (MutagenError, ValueError, EOFError, struct.error) as error:
        raise TagEditError("无法读取音频文件或标签。") from error


def _first(value):
    if value is None:
        return ""
    values = value.text if hasattr(value, "text") else value
    if isinstance(values, (list, tuple)):
        return str(values[0]) if values else ""
    return str(values)


def _metadata(path, track, fingerprint):
    tags = track.tags or {}
    values = {field: "" for field in FIELDS}
    if isinstance(track, (MP3, WAVE)):
        for field, key in _ID3_FIELDS.items():
            values[field] = _first(tags.get(key))
        values["date"] = _first(tags.get("TDRC")) or _first(tags.get("TYER"))
        if "TDRC" not in tags and values["date"] and tags.get("TDAT"):
            daymonth = _first(tags["TDAT"])
            if re.fullmatch(r"\d{4}", daymonth):
                values["date"] += "-" + daymonth[2:] + "-" + daymonth[:2]
        codec = "MP3" if isinstance(track, MP3) else "WAV"
    elif isinstance(track, MP4):
        for field, key in _MP4_FIELDS.items():
            if field != "tracknumber":
                values[field] = _first(tags.get(key))
        numbers = tags.get("trkn", [])
        if numbers:
            number, total = numbers[0]
            values["tracknumber"] = str(number) + (f"/{total}" if total else "")
        codec = {"mp4a.40.2": "AAC", "alac": "ALAC"}.get(track.info.codec, track.info.codec)
    else:
        for field in FIELDS:
            values[field] = _first(tags.get(field))
        total = _first(tags.get("tracktotal")) or _first(tags.get("totaltracks"))
        if total and values["tracknumber"] and "/" not in values["tracknumber"]:
            values["tracknumber"] += "/" + total
        codec = "FLAC" if isinstance(track, FLAC) else "Opus"
    if any(len(text) > _MAX_TEXT or "\x00" in text for text in values.values()):
        raise TagEditError("现有标签文本过长或含无效字符。")
    return dict(path=str(path), **values, duration=float(track.info.length), codec=codec, fingerprint=fingerprint)


def _changes(value):
    if not isinstance(value, dict) or not value or any(key not in FIELDS for key in value):
        raise TagEditError("请选择至少一个有效标签字段。")
    result = {}
    for key, text in value.items():
        if (not isinstance(text, str) or len(text) > _MAX_TEXT or "\x00" in text
                or any(0xD800 <= ord(char) <= 0xDFFF for char in text)):
            raise TagEditError("标签必须是最多 4096 个字符的有效文本。")
        text = text.strip()
        if key == "date" and text:
            if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", text):
                raise TagEditError("日期请填写 YYYY、YYYY-MM 或 YYYY-MM-DD。")
            try:
                datetime.fromisoformat(text + ("-01-01" if len(text) == 4 else "-01" if len(text) == 7 else ""))
            except ValueError:
                raise TagEditError("日期无效。") from None
        if key == "tracknumber" and text:
            if not re.fullmatch(r"[1-9]\d{0,4}(?:/[1-9]\d{0,4})?", text):
                raise TagEditError("曲序请填写 n 或 n/总数。")
            numbers = list(map(int, text.split("/")))
            if max(numbers) > 65535 or len(numbers) == 2 and numbers[0] > numbers[1]:
                raise TagEditError("曲序应在 1–65535 内，且不超过总数。")
        result[key] = text
    return result


def _snapshot(track, changes):
    tags = track.tags or {}
    if isinstance(track, (MP3, WAVE)):
        ignored = {_ID3_FIELDS[key] for key in changes if key in _ID3_FIELDS}
        if "date" in changes:
            ignored.update(("TDRC", "TYER", "TDAT", "TIME"))
        values = {key: repr(value) for key, value in tags.items() if key.split(":", 1)[0] not in ignored}
        values["_unknown"] = repr(getattr(track.tags, "unknown_frames", []))
    elif isinstance(track, MP4):
        ignored = {_MP4_FIELDS[key] for key in changes}
        values = {key: repr(value) for key, value in tags.items() if key not in ignored}
    else:
        ignored = set(changes)
        if "/" in changes.get("tracknumber", ""):
            ignored.update(("tracktotal", "totaltracks"))
        values = {key.casefold(): repr(value) for key, value in tags.items() if key.casefold() not in ignored}
        if isinstance(track, FLAC):
            values["_pictures"] = repr([picture.write() for picture in track.pictures])
    return values


def _legacy_tail(path, changes):
    if path.suffix.lower() != ".mp3":
        return None
    with path.open("rb") as source:
        if source.seek(0, 2) < 128:
            return None
        source.seek(-128, 2)
        tail = bytearray(source.read(128))
    if tail[:3] != b"TAG":
        return None
    for key, start, end in (("title", 3, 33), ("artist", 33, 63), ("album", 63, 93), ("date", 93, 97)):
        if key in changes:
            text = changes[key][:4] if key == "date" else changes[key]
            tail[start:end] = text.encode("latin1", "replace")[:end-start].ljust(end-start, b"\0")
    if "tracknumber" in changes:
        number = int(changes["tracknumber"].split("/")[0]) if changes["tracknumber"] else 0
        tail[125:127] = bytes((0, number if number < 256 else 0))
    if "genre" in changes:
        from mutagen._constants import GENRES
        tail[127] = next((index for index, label in enumerate(GENRES[:255])
                         if label.casefold() == changes["genre"].casefold()), 255)
    return bytes(tail)


def _edit(path, changes):
    track = _load(path)
    snapshot = _snapshot(track, changes)
    properties = (type(track), track.info.length, getattr(track.info, "sample_rate", None), getattr(track.info, "channels", None))
    legacy = _legacy_tail(path, changes)
    if track.tags is None:
        track.add_tags()
    tags = track.tags
    if isinstance(track, (MP3, WAVE)):
        version = 3 if tags.version[:2] == (2, 3) else 4
        encoding = 1 if version == 3 else 3
        for key, text in changes.items():
            if key == "date":
                for frame in ("TDRC", "TYER", "TDAT", "TIME"):
                    tags.delall(frame)
                if text and version == 3:
                    if len(text) == 7:
                        raise TagEditError("ID3v2.3 日期请填写年份或完整年月日。")
                    tags.add(TYER(encoding=encoding, text=[text[:4]]))
                    if len(text) == 10:
                        tags.add(TDAT(encoding=encoding, text=[text[8:10] + text[5:7]]))
                elif text:
                    tags.add(TDRC(encoding=encoding, text=[text]))
            else:
                tags.delall(_ID3_FIELDS[key])
                if text:
                    tags.add(_ID3_CLASSES[key](encoding=encoding, text=[text]))
        track.save(path, v2_version=version, v1=0)
        if legacy is not None:
            with path.open("ab") as output:
                output.write(legacy)
    elif isinstance(track, MP4):
        for key, text in changes.items():
            atom = _MP4_FIELDS[key]
            old = tags.get(atom)
            tags.pop(atom, None)
            if text:
                if key == "tracknumber":
                    numbers = list(map(int, text.split("/")))
                    total = numbers[1] if len(numbers) == 2 else old[0][1] if old else 0
                    tags[atom] = [(numbers[0], total)]
                else:
                    tags[atom] = [text]
        track.save(path)
    else:
        for key, text in changes.items():
            if key in tags:
                del tags[key]
            if text:
                if key == "tracknumber" and "/" in text:
                    number, total = text.split("/")
                    tags[key] = [number]
                    for field in ("tracktotal", "totaltracks"):
                        if field in tags or field == "tracktotal":
                            tags[field] = [total]
                else:
                    tags[key] = [text]
        track.save(path)
    result = _load(path)
    actual = (type(result), result.info.length, getattr(result.info, "sample_rate", None), getattr(result.info, "channels", None))
    if properties != actual or snapshot != _snapshot(result, changes):
        raise TagEditError("写入验证失败：原音轨或未选择的标签发生变化。")
    return result


class TagEditStore:
    """One private journal directory, serialized across threads and processes."""

    def __init__(self, directory):
        self.directory = _path(directory)
        self._mutex = threading.RLock()

    def _directory(self):
        for path in (*reversed(self.directory.parents), self.directory):
            if not path.exists():
                try:
                    path.mkdir()
                except FileExistsError:
                    pass
            _plain_directory(path)

    @contextmanager
    def _locked(self):
        with self._mutex:
            self._directory()
            lock = self.directory / "store.lock"
            try:
                with lock.open("xb") as output:
                    output.write(b"0")
            except FileExistsError:
                pass
            before = _regular(lock)
            with lock.open("r+b") as stream:
                if not os.path.samestat(before, os.fstat(stream.fileno())):
                    raise TagEditConflict("标签历史目录已更改。")
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise TagEditError("另一项标签操作正在进行，请稍后重试。") from None
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _folder(self, batch_id):
        if not isinstance(batch_id, str) or _ID.fullmatch(batch_id) is None:
            raise TagEditError("标签修改批次编号无效。")
        folder = self.directory / batch_id
        _plain_directory(folder)
        return folder

    def _save(self, record):
        folder = self._folder(record["id"])
        record["updated_at"] = _now()
        encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_JOURNAL:
            raise TagEditError("本次标签修改记录过大。")
        target, temporary = folder / "journal.json", folder / "journal.new"
        if temporary.exists():
            _regular(temporary)
            temporary.unlink()
        if target.exists():
            _regular(target)
        with temporary.open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)

    def _read_record(self, batch_id):
        folder = self._folder(batch_id)
        path = folder / "journal.json"
        with _opened(path) as (source, _):
            raw = source.read(_MAX_JOURNAL + 1)
        if len(raw) > _MAX_JOURNAL:
            raise TagEditError("标签历史记录过大。")
        try:
            record = json.loads(raw)
            assert record["version"] == 1 and record["id"] == batch_id and record["state"] in _STATES
            assert isinstance(record["items"], list) and 1 <= len(record["items"]) <= _MAX_ITEMS
            assert all(isinstance(record[key], str) and len(record[key]) <= 512 for key in ("created_at", "updated_at", "error"))
            seen = set()
            for index, item in enumerate(record["items"]):
                path = _path(item["path"])
                assert str(path) == item["path"] and path.suffix.lower() in _TYPES
                assert path != self.directory and not path.is_relative_to(self.directory)
                key = os.path.normcase(str(path))
                assert key not in seen
                seen.add(key)
                assert item["changes"] == _changes(item["changes"])
                assert item["backup"] == f"{index:04d}.original{path.suffix.lower()}"
                assert item["staged"] == f"{index:04d}.edited{path.suffix.lower()}"
                for field in ("original_sha256", "result_sha256"):
                    assert item.get(field) is None or isinstance(item[field], str) and _HEX.fullmatch(item[field])
                assert item.get("identity") is None or isinstance(item["identity"], list) and len(item["identity"]) == 4 and all(type(value) is int for value in item["identity"])
            return record
        except (ValueError, KeyError, TypeError, AssertionError, RecursionError) as error:
            raise TagEditError("标签历史记录无效，已保留文件供检查。") from error

    def _copy(self, source, target, stop=None):
        with _opened(source) as (original, info), target.open("xb") as output:
            digest = hashlib.sha256()
            while True:
                _stop(stop)
                chunk = original.read(_CHUNK)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, target, follow_symlinks=False)
        return digest.hexdigest(), _identity(info)

    def _install(self, source, path, expected, desired, stop=None, identity=None):
        if _hash(source, stop) != desired:
            raise TagEditConflict("标签备份或暂存文件已更改，未覆盖原文件。")
        _plain_directory(path.parent)
        if shutil.disk_usage(path.parent).free < _regular(source).st_size + 1024 * 1024:
            raise TagEditError("音频所在磁盘空间不足，无法安全暂存标签修改。")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".amdl-tags-", suffix=path.suffix, dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
            temporary.unlink()
            copied, _ = self._copy(source, temporary, stop)
            if copied != desired:
                raise TagEditConflict("标签暂存文件已更改。")
            with _opened(path) as (current, info):
                if identity is not None and _identity(info) != identity:
                    raise TagEditConflict("原文件身份已更改，请重新读取。")
                if _hash_stream(current, stop) != expected:
                    raise TagEditConflict("文件已被其他程序修改，未覆盖后续更改。")
            _stop(stop)
            if _identity(_regular(path)) != _identity(info):
                raise TagEditConflict("原文件在保存前已更改。")
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                _regular(temporary)
                temporary.unlink()

    def _restore(self, record, *, undo=False):
        folder = self._folder(record["id"])
        failures = []
        for item in reversed(record["items"]):
            if not item.get("result_sha256"):
                continue
            try:
                path = _path(item["path"])
                current = _hash(path)
                if current == item["original_sha256"]:
                    continue
                if current != item["result_sha256"]:
                    raise TagEditConflict("文件已被其他程序修改。")
                self._install(folder / item["backup"], path, item["result_sha256"], item["original_sha256"])
            except (OSError, ValueError, RuntimeError) as error:
                failures.append(f"{Path(item['path']).name}：{error}")
        record["state"] = "conflict" if failures else "undone" if undo else "rolled_back"
        record["error"] = "；".join(failures)[:500]
        self._save(record)
        return not failures

    def _history(self, recover=False, stop=None):
        records = []
        for folder in self.directory.iterdir():
            _stop(stop)
            if _ID.fullmatch(folder.name) is None:
                continue
            try:
                record = self._read_record(folder.name)
                if recover and record["state"] in ("applying", "undoing"):
                    self._restore(record, undo=record["state"] == "undoing")
                elif recover and record["state"] == "preparing":
                    record.update(state="failed", error="上次操作在准备期间中断，原文件未修改。")
                    self._save(record)
                records.append(record)
            except (OSError, ValueError, RuntimeError):
                records.append(dict(id=folder.name, state="failed", created_at="", updated_at="", items=[],
                                    error="标签历史记录无法验证，未修改音频文件。"))
        return sorted(records, key=lambda record: (record["created_at"], record["id"]), reverse=True)

    def list_history(self):
        """Return newest-first durable records without changing original audio."""
        with self._locked():
            return self._history()

    def read_audio(self, path):
        """Read supported plain audio plus its full-file SHA256 preview guard."""
        path = _path(path)
        _payload(path)
        before = _identity(_regular(path))
        fingerprint = _hash(path)
        track = _load(path)
        if before != _identity(_regular(path)):
            raise TagEditConflict("读取期间文件已更改，请重试。")
        return _metadata(path, track, fingerprint)

    def apply(self, edits, stop=None, on_status=None):
        """Stage/validate the whole batch, then replace; roll back on any failure."""
        if not isinstance(edits, (list, tuple)) or not 1 <= len(edits) <= _MAX_ITEMS:
            raise TagEditError("每次请选择 1–200 个音频文件。")
        _stop(stop)
        items, seen = [], set()
        for index, edit in enumerate(edits):
            if not isinstance(edit, dict) or "path" not in edit:
                raise TagEditError("标签修改项目无效。")
            path = _path(edit["path"])
            if path.suffix.lower() not in _TYPES:
                raise TagEditError("只支持 MP3、M4A、FLAC、WAV 和 Opus 文件。")
            if path.is_relative_to(self.directory) or self.directory.is_relative_to(path.parent):
                raise TagEditError("标签备份目录必须与音频目录分开。")
            key = os.path.normcase(str(path))
            if key in seen:
                raise TagEditError("同一批次不能重复选择同一个文件。")
            seen.add(key)
            expected = edit.get("expected")
            if expected is not None and (not isinstance(expected, str) or _HEX.fullmatch(expected) is None):
                raise TagEditError("音频预览指纹无效，请重新读取。")
            items.append(dict(path=str(path), changes=_changes(edit.get("changes")), expected=expected,
                              backup=f"{index:04d}.original{path.suffix.lower()}", staged=f"{index:04d}.edited{path.suffix.lower()}",
                              original_sha256=None, result_sha256=None, identity=None, before=None, after=None))
        with self._locked():
            self._history(recover=True, stop=stop)
            required = 32 * 1024 * 1024
            targets = {}
            for item in items:
                path = _path(item["path"])
                size = _regular(path).st_size
                growth = 65536 + sum(len(text.encode("utf-8")) for text in item["changes"].values())
                required += 2 * (size + growth)
                targets[path.parent] = max(targets.get(path.parent, 0), size + growth + 1024 * 1024)
            if shutil.disk_usage(self.directory).free < required:
                raise TagEditError("标签备份磁盘空间不足；需要同时保存整批原文件和修改暂存文件。")
            if any(shutil.disk_usage(path).free < size for path, size in targets.items()):
                raise TagEditError("音频所在磁盘空间不足，无法安全暂存标签修改。")
            record = dict(version=1, id=uuid4().hex, state="preparing", created_at=_now(), updated_at=_now(), items=items, error="")
            folder = self.directory / record["id"]
            folder.mkdir()
            self._save(record)
            try:
                for index, item in enumerate(items, 1):
                    _stop(stop)
                    path = _path(item["path"])
                    _notify(on_status, f"正在准备标签 · {index}/{len(items)} · {path.name}")
                    backup, stage = folder / item["backup"], folder / item["staged"]
                    fingerprint, identity = self._copy(path, backup, stop)
                    if item["expected"] is not None and fingerprint != item["expected"]:
                        raise TagEditConflict("文件自预览后已更改，请重新读取后再修改。")
                    item.update(original_sha256=fingerprint, identity=identity)
                    payload = _payload(backup, stop)
                    item["before"] = _metadata(path, _load(backup), fingerprint)
                    self._copy(backup, stage, stop)
                    track = _edit(stage, item["changes"])
                    if _payload(stage, stop) != payload:
                        raise TagEditError("写入验证失败：原生音频数据发生变化。")
                    item["result_sha256"] = _hash(stage, stop)
                    item["after"] = _metadata(path, track, item["result_sha256"])
                    self._save(record)
                # Preflight every original before the first replacement.
                for item in items:
                    path = _path(item["path"])
                    if _identity(_regular(path)) != item["identity"] or _hash(path, stop) != item["original_sha256"]:
                        raise TagEditConflict("原文件在准备期间已更改，未开始替换。")
                record["state"] = "applying"
                self._save(record)
                for index, item in enumerate(items, 1):
                    _stop(stop)
                    _notify(on_status, f"正在保存标签 · {index}/{len(items)} · {Path(item['path']).name}")
                    self._install(folder / item["staged"], _path(item["path"]), item["original_sha256"],
                                  item["result_sha256"], stop, item["identity"])
                _stop(stop)
                record["state"] = "applied"
                self._save(record)
                return record
            except BaseException as error:
                if record["state"] == "applying":
                    safe = self._restore(record)
                else:
                    safe = True
                    record.update(state="cancelled" if isinstance(error, Cancelled) else "failed", error=str(error)[:500])
                    self._save(record)
                if not safe:
                    raise TagEditConflict("操作中断；部分文件存在后续更改，未覆盖。请检查历史记录。", record) from error
                if isinstance(error, Cancelled):
                    raise
                if isinstance(error, TagEditError):
                    error.record = record
                    raise
                if isinstance(error, (OSError, ValueError, MutagenError)):
                    raise TagEditError("标签修改失败，已安全恢复原文件。", record) from error
                raise

    def undo(self, batch_id, stop=None, on_status=None):
        """Restore original bytes only if every current file matches this batch."""
        _stop(stop)
        with self._locked():
            self._history(recover=True, stop=stop)
            record = self._read_record(batch_id)
            if record["state"] != "applied":
                raise TagEditError("只有已完成的标签修改可以撤销。", record)
            folder = self._folder(batch_id)
            try:
                for item in record["items"]:
                    _stop(stop)
                    if _hash(_path(item["path"]), stop) != item["result_sha256"]:
                        raise TagEditConflict("文件已被其他程序修改，未覆盖后续更改。", record)
                    if _hash(folder / item["backup"], stop) != item["original_sha256"]:
                        raise TagEditConflict("原始备份已更改，不能安全撤销。", record)
            except (OSError, ValueError, TagEditConflict) as error:
                record.update(state="conflict", error=str(error)[:500])
                self._save(record)
                raise TagEditConflict("无法安全撤销；原文件和历史备份均已保留。", record) from error
            _stop(stop)
            record["state"] = "undoing"
            self._save(record)
            _notify(on_status, "正在恢复原始文件；完成前请勿关闭程序…")
            # Once undo starts, finish the small transaction even if cancellation
            # arrives. This prevents cancellation from leaving half a batch undone.
            if not self._restore(record, undo=True):
                raise TagEditConflict("部分文件在撤销期间发生变化，未覆盖；请检查历史记录。", record)
            return record

    def recover(self, stop=None, on_status=None):
        """Finish interrupted apply/undo rollback and mark abandoned prepares failed."""
        _notify(on_status, "正在检查并恢复中断的标签批次…")
        with self._locked():
            return self._history(recover=True, stop=stop)
