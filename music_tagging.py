"""Add archive metadata to a staged MP3 without re-encoding the audio."""

import os
from pathlib import Path
import shutil
import stat
import struct
from tempfile import NamedTemporaryFile

from mutagen.id3 import APIC, ID3, ID3NoHeaderError, PictureType, TIT2, TPE1
from mutagen.mp3 import MPEGInfo

from storage import _is_link


_COVER_BYTES = 4 * 1024 * 1024
_COVER_DIMENSION = 4096
_COVER_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_JPEG_FRAMES = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _regular_file(path):
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("音频与封面必须是普通文件。")
    return info


def _jpeg_dimensions(data):
    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        return None
    offset = 2
    dimensions = None
    while offset + 4 < len(data) and data[offset] == 0xFF:
        offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset + 2 >= len(data):
            return None
        marker = data[offset]
        offset += 1
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if marker in _JPEG_FRAMES and length >= 8:
            dimensions = struct.unpack_from(">HH", data, offset + 3)
        if marker == 0xDA:
            return dimensions
        offset += length
    return None


def _cover_data(path):
    path = Path(path)
    mime = _COVER_EXTENSIONS.get(path.suffix.lower())
    if not mime:
        raise ValueError("封面必须是 PNG 或 JPEG 文件。")
    info = _regular_file(path)
    if not 0 < info.st_size <= _COVER_BYTES:
        raise ValueError("封面文件大小无效或超过 4 MB。")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise ValueError("封面文件已更改，请重试。")
        data = stream.read(_COVER_BYTES + 1)
    if len(data) != info.st_size:
        raise ValueError("封面文件大小已更改，请重试。")
    if mime == "image/png":
        png = (len(data) >= 45 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
               and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"))
        dimensions = struct.unpack_from(">II", data, 16) if png else None
    else:
        dimensions = _jpeg_dimensions(data)
    if not dimensions or any(not 1 <= size <= _COVER_DIMENSION for size in dimensions):
        raise ValueError("封面格式无效或尺寸超出 4096 像素。")
    return mime, data


def _text(value):
    return " ".join(value.replace("\0", " ").split())[:500] if isinstance(value, str) else ""


def _attach_front_cover(tags, encoding, cover):
    mime, data = cover
    for picture in tags.getall("APIC"):
        if picture.type == PictureType.COVER_FRONT:
            del tags[picture.HashKey]
    frame = APIC(encoding=encoding, mime=mime, type=PictureType.COVER_FRONT, desc="Front Cover", data=data)
    suffix = 2
    while frame.HashKey in tags:
        frame.desc = f"Front Cover {suffix}"
        suffix += 1
    tags.add(frame)


def write_mp3_tags(path, title, artist, *, cover=None):
    """Write known title and artist fields, optionally replacing the front cover."""
    path = Path(path)
    if path.suffix.lower() != ".mp3":
        raise ValueError("只有 MP3 文件支持这项标签写入。")
    title, artist = _text(title), _text(artist)
    if not title and not artist and cover is None:
        return
    cover = _cover_data(cover) if cover is not None else None
    info = _regular_file(path)
    scratch = None
    try:
        with path.open("rb") as source:
            if not os.path.samestat(info, os.fstat(source.fileno())):
                raise ValueError("MP3 文件已更改，请重试。")
            if MPEGInfo(source).layer != 3:
                raise ValueError("音频文件的实际编码不是 MP3。")
            source.seek(0)
            try:
                tags = ID3(source, translate=False)
            except ID3NoHeaderError:
                tags = ID3()
            version = 3 if tags.version[:2] == (2, 3) else 4
            if version == 3:
                tags.update_to_v23()
            else:
                tags.update_to_v24()
            encoding = 1 if version == 3 else 3
            if title:
                tags.add(TIT2(encoding=encoding, text=[title]))
            if artist:
                tags.add(TPE1(encoding=encoding, text=[artist]))
            if cover is not None:
                _attach_front_cover(tags, encoding, cover)
            with NamedTemporaryFile(prefix=".mp3-tags-", suffix=".mp3", dir=path.parent, delete=False) as target:
                scratch = Path(target.name)
                source.seek(0)
                shutil.copyfileobj(source, target, 128 * 1024)
        tags.save(scratch, v2_version=version)
        current = _regular_file(path)
        if not os.path.samestat(info, current) or (info.st_size, info.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
            raise ValueError("MP3 文件已更改，请重试。")
        os.replace(scratch, path)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
