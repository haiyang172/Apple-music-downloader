"""Persist verified Bilibili media prefixes on a caller-selected volume."""

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

from engine import Cancelled
from storage import _is_link, _plain_directory


_CHUNK = 256 * 1024
_CHECKPOINT_STEP = 8 * 1024 * 1024
_META_LIMIT = 2048
_MAX_AGE = 7 * 24 * 3600
_DIGEST = re.compile(r"[a-f0-9]{64}")
_ETAG = re.compile(r'"[\x21\x23-\x7e]{1,512}"')
_RANGE = re.compile(r"bytes (\d{1,12})-(\d{1,12})/(\d{1,12})")
_TYPES = {"audio/mp4", "video/mp4", "application/mp4", "application/octet-stream"}
_SLOTS = ("audio", "video")


@dataclass(frozen=True)
class _Checkpoint:
    identity: str
    etag: str
    total: int
    offset: int
    sha256: str


class _MediaChanged(RuntimeError):
    pass


def _stop(event):
    if event.is_set():
        raise Cancelled("已取消下载。")


def _directory(path, check_volume, create=False):
    if check_volume is not None:
        check_volume()
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("B 站续传缓存目录无效。")
    if create:
        _plain_directory(path.parent)
        path.mkdir(exist_ok=True)
    _plain_directory(path)
    return path


def _paths(directory, slot):
    if slot not in _SLOTS:
        raise ValueError("B 站媒体类型无效。")
    return directory / f"{slot}.m4s.part", directory / f"{slot}.json", directory / f"{slot}.json.new"


def _regular(path):
    item = path.lstat()
    if _is_link(item) or not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        raise ValueError("B 站续传缓存必须使用普通文件。")
    return item


def _open_regular(path, mode):
    before = _regular(path)
    opened = path.open(mode)
    after = os.fstat(opened.fileno())
    if not os.path.samestat(before, after) or after.st_nlink != 1:
        opened.close()
        raise ValueError("B 站续传缓存文件已更改。")
    return opened


def _delete(path):
    try:
        _regular(path)
    except FileNotFoundError:
        return
    path.unlink()


def _identity(scope, media_id, url):
    if not isinstance(scope, str) or not _DIGEST.fullmatch(scope):
        raise ValueError("B 站续传访问范围无效。")
    if not isinstance(media_id, str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,192}", media_id):
        raise ValueError("B 站续传媒体编号无效。")
    if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() or ord(c) < 33 or c in "\\\x7f" for c in url):
        raise ValueError("B 站媒体地址无效。")
    try:
        target = urlsplit(url)
        host = target.hostname or ""
    except ValueError:
        raise ValueError("B 站媒体地址无效。") from None
    if (target.scheme != "https" or target.netloc != host or not target.path or target.fragment
            or not (host.endswith((".bilivideo.com", ".bilivideo.cn")) or host == "upos-hz-mirrorakam.akamaized.net")):
        raise ValueError("B 站媒体地址无效。")
    return hashlib.sha256(f"{scope}\n{media_id}\n{host}\n{target.path}".encode("utf-8")).hexdigest()


def _load(part, meta):
    try:
        modified = _regular(meta).st_mtime
        with _open_regular(meta, "rb") as stream:
            raw = stream.read(_META_LIMIT + 1)
        size = _regular(part).st_size
    except FileNotFoundError:
        return None
    if len(raw) > _META_LIMIT or time.time() - modified > _MAX_AGE:
        return None
    try:
        data = _Checkpoint(**json.loads(raw))
    except (ValueError, TypeError, RecursionError):
        return None
    if (not all(isinstance(x, str) for x in (data.identity, data.etag, data.sha256))
            or not _DIGEST.fullmatch(data.identity) or not _DIGEST.fullmatch(data.sha256)
            or not _ETAG.fullmatch(data.etag) or type(data.offset) is not int or type(data.total) is not int
            or not 8 <= data.offset <= size <= data.total):
        return None
    return data


def _commit(file, paths, identity, tag, total, offset, digest, guard):
    guard()
    if not tag or total is None or offset < 8:
        return
    _, meta, temporary = paths
    file.flush()
    os.fsync(file.fileno())
    _delete(temporary)
    data = _Checkpoint(identity, tag, total, offset, digest.hexdigest())
    with temporary.open("x", encoding="ascii") as stream:
        json.dump(data.__dict__, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    guard()
    try:
        _regular(meta)
    except FileNotFoundError:
        pass
    temporary.replace(meta)


def _prefix(part, record, stop):
    digest = hashlib.sha256()
    remaining = record.offset
    with _open_regular(part, "rb") as stream:
        while remaining:
            _stop(stop)
            chunk = stream.read(min(remaining, _CHUNK))
            if not chunk:
                return None
            digest.update(chunk)
            remaining -= len(chunk)
    return digest if digest.hexdigest() == record.sha256 else None


def _response(open_response, start, end="", etag=""):
    headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
    if etag:
        headers["If-Range"] = etag
    try:
        return open_response(headers)
    except HTTPError as error:
        error.close()
        if etag and error.code == 416:
            raise _MediaChanged() from None
        if error.code in (401, 403, 404, 410):
            raise RuntimeError("B 站未提供可访问的完整音视频。") from None
        if error.code in (412, 429):
            raise RuntimeError("B 站拒绝了本次播放请求，请稍后重试。") from None
        raise RuntimeError("B 站媒体下载请求失败，请重试。") from None
    except (OSError, HTTPException):
        raise RuntimeError("无法连接 B 站媒体服务器，请检查网络后重试。") from None


def _size(response, limit, start=0, end=None):
    headers = response.headers
    if headers.get_content_type() not in _TYPES or headers.get("Content-Encoding") not in (None, "identity"):
        raise ValueError("B 站没有返回预期的音视频格式。")
    length = headers.get("Content-Length")
    if length is not None and not re.fullmatch(r"\d{1,12}", length):
        raise ValueError("B 站返回了无效的媒体长度。")
    length = None if length is None else int(length)
    if response.status == 206:
        match = _RANGE.fullmatch(headers.get("Content-Range", ""))
        if not match:
            raise ValueError("B 站返回了无效的媒体分段。")
        first, last, total = map(int, match.groups())
        if first != start or not first <= last < total or last != (total - 1 if end is None else end):
            raise ValueError("B 站未返回请求的完整媒体分段。")
        if length is not None and length != last - first + 1:
            raise ValueError("B 站返回了不完整的媒体分段。")
    elif response.status == 200 and start == 0 and end is None:
        total = length
    else:
        raise ValueError("B 站未返回请求的媒体分段。")
    if total is not None and not 8 <= total <= limit:
        raise ValueError("B 站媒体长度无效或超过下载上限。")
    return total


def _checked_range(response, record, limit, start, end=None):
    if response.status != 206 or response.headers.get("ETag") != record.etag:
        raise _MediaChanged()
    try:
        total = _size(response, limit, start, end)
    except ValueError:
        raise _MediaChanged() from None
    if total != record.total:
        raise _MediaChanged()


def _download(response, file, paths, guard, identity, tag, total, offset, digest, limit, stop, label, on_status):
    received, committed = offset, offset
    started = last_status = time.monotonic()

    def checkpoint():
        if received > committed:
            _commit(file, paths, identity, tag, total, received, digest, guard)

    while True:
        try:
            _stop(stop)
            if time.monotonic() - started > 1800:
                raise TimeoutError
            chunk = response.read(_CHUNK)
        except Cancelled:
            checkpoint()
            raise
        except (OSError, HTTPException):
            checkpoint()
            raise RuntimeError("B 站媒体传输中断，请重试。") from None
        if not chunk:
            break
        if received == 0 and chunk[4:8] != b"ftyp":
            raise RuntimeError("B 站返回的媒体格式无法识别。")
        if received + len(chunk) > (limit if total is None else total):
            raise RuntimeError("B 站媒体超过预期大小。")
        file.write(chunk)
        digest.update(chunk)
        received += len(chunk)
        if received - committed >= _CHECKPOINT_STEP:
            checkpoint()
            committed = received
        now = time.monotonic()
        if now - last_status >= 0.5:
            progress = f"{received * 100 // total}%" if total else f"{received / 1024 ** 2:.1f} MB"
            on_status(f"正在下载{label} · {progress}")
            last_status = now
    checkpoint()
    if received < 8 or (total is not None and received != total):
        raise RuntimeError("B 站媒体下载未完成，请重试。")
    _stop(stop)


def choose_resume_url(cache_dir, slot, scope, media_id, urls, *, check_volume=None):
    """Prefer the exact trusted origin/path of a checkpoint if currently offered."""
    candidates = list(urls)
    identities = {_identity(scope, media_id, url): url for url in candidates}
    if not candidates:
        raise ValueError("B 站没有提供可用的媒体地址。")
    try:
        directory = _directory(cache_dir, check_volume)
    except FileNotFoundError:
        return candidates[0]
    part, meta, _ = _paths(directory, slot)
    record = _load(part, meta)
    return identities.get(record.identity, candidates[0]) if record else candidates[0]


def resume_stream(cache_dir, slot, scope, media_id, url, open_response, stop, label, limit, on_status, *, check_volume=None):
    """Save a verified full stream, reusing safe persistent bytes when possible."""
    identity = _identity(scope, media_id, url)
    _stop(stop)
    directory = _directory(cache_dir, check_volume, create=True)

    def guard():
        _directory(directory, check_volume)

    paths = _paths(directory, slot)
    part, meta, temporary = paths
    record = _load(part, meta)
    digest = None
    if record and record.identity == identity and record.total <= limit:
        digest = _prefix(part, record, stop)
    if digest is not None:
        try:
            _stop(stop)
            if record.offset == record.total:
                with _response(open_response, 0, 0, record.etag) as response:
                    _checked_range(response, record, limit, 0, 0)
                _stop(stop)
                guard()
                on_status(f"已核对缓存{label}")
                return part
            with _response(open_response, record.offset, etag=record.etag) as response:
                _checked_range(response, record, limit, record.offset)
                _stop(stop)
                guard()
                on_status(f"继续下载{label} · {record.offset * 100 // record.total}%")
                with _open_regular(part, "r+b") as file:
                    file.seek(record.offset)
                    file.truncate()
                    _download(response, file, paths, guard, identity, record.etag, record.total,
                              record.offset, digest, limit, stop, label, on_status)
            return part
        except _MediaChanged:
            pass

    guard()
    for path in (part, meta, temporary):
        _delete(path)
    _stop(stop)
    on_status(f"正在下载{label}…")
    with _response(open_response, 0) as response:
        total = _size(response, limit)
        tag = response.headers.get("ETag", "")
        tag = tag if _ETAG.fullmatch(tag) else ""
        guard()
        with part.open("xb") as file:
            _download(response, file, paths, guard, identity, tag, total, 0, hashlib.sha256(), limit, stop, label, on_status)
    guard()
    return part


def forget_resume(cache_dir, *, check_volume=None):
    """Remove the fixed stream checkpoints after publishing the completed media."""
    try:
        directory = _directory(cache_dir, check_volume)
    except FileNotFoundError:
        return
    for slot in _SLOTS:
        _directory(directory, check_volume)
        for path in _paths(directory, slot):
            _delete(path)
    # A caller may have placed other work in the task directory.
    if not any(directory.iterdir()):
        directory.rmdir()
