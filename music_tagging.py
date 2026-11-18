"""Write audio metadata atomically without re-encoding audio frames."""

from base64 import b64decode, b64encode
import os
from pathlib import Path
import shutil
import stat
import struct
from tempfile import NamedTemporaryFile

from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, PictureType, TIT2, TPE1, USLT
from mutagen.mp3 import MP3, MPEGInfo
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE

from storage import _is_link


_COVER_BYTES = 4 * 1024 * 1024
_COVER_DIMENSION = 4096
_COVER_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_LYRICS_BYTES = 1024 * 1024
_CONTAINERS = {".flac": FLAC, ".m4a": MP4, ".mp4": MP4, ".wav": WAVE, ".opus": OggOpus}
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
    dimensions = _cover_dimensions(mime, data)
    if not dimensions or any(not 1 <= size <= _COVER_DIMENSION for size in dimensions):
        raise ValueError("封面格式无效或尺寸超出 4096 像素。")
    return mime, data


def _cover_dimensions(mime, data):
    if mime == "image/png":
        png = (len(data) >= 45 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
               and data.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82"))
        return struct.unpack_from(">II", data, 16) if png else None
    return _jpeg_dimensions(data) if mime == "image/jpeg" else None


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
    _write_mp3_tags(path, title, artist, cover, None)


def _write_mp3_tags(path, title, artist, cover, lyrics):
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
            if lyrics is not None:
                tags.add(USLT(encoding=encoding, lang="und", desc="", text=lyrics))
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


def _lyrics_text(value):
    if isinstance(value, Path):
        info = _regular_file(value)
        if not 0 < info.st_size <= _LYRICS_BYTES:
            raise ValueError("歌词文件大小无效或超过 1 MB。")
        with value.open("rb") as source:
            if not os.path.samestat(info, os.fstat(source.fileno())):
                raise ValueError("歌词文件已更改，请重试。")
            encoded = source.read(_LYRICS_BYTES + 1)
        if len(encoded) != info.st_size:
            raise ValueError("歌词文件大小已更改，请重试。")
        try:
            value = encoded.decode("utf-8-sig")
        except UnicodeError:
            raise ValueError("歌词必须是 UTF-8 文本。") from None
    if not isinstance(value, str) or "\x00" in value or len(value.encode("utf-8")) > _LYRICS_BYTES:
        raise ValueError("歌词必须是最多 1 MB 的文本。")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _picture(cover):
    mime, data = cover
    picture = Picture()
    picture.type = PictureType.COVER_FRONT
    picture.mime = mime
    picture.desc = "Front Cover"
    picture.data = data
    if mime == "image/png":
        picture.width, picture.height = struct.unpack_from(">II", data, 16)
    else:
        picture.height, picture.width = _jpeg_dimensions(data)
    return picture


def _update_container(track, title, artist, cover, lyrics):
    if track.tags is None:
        track.add_tags()
    if isinstance(track, WAVE):
        tags = track.tags
        version = 3 if tags.version[:2] == (2, 3) else 4
        encoding = 1 if version == 3 else 3
        if title:
            tags.add(TIT2(encoding=encoding, text=[title]))
        if artist:
            tags.add(TPE1(encoding=encoding, text=[artist]))
        if lyrics is not None:
            tags.add(USLT(encoding=encoding, lang="und", desc="", text=lyrics))
        if cover is not None:
            _attach_front_cover(tags, encoding, cover)
        track.save(v2_version=version)
        return
    if isinstance(track, MP4):
        if title:
            track["\xa9nam"] = [title]
        if artist:
            track["\xa9ART"] = [artist]
        if lyrics is not None:
            track["\xa9lyr"] = [lyrics]
        if cover is not None:
            mime, data = cover
            image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            track["covr"] = [MP4Cover(data, imageformat=image_format), *track.get("covr", [])[1:]]
        track.save()
        return
    if title:
        track["title"] = [title]
    if artist:
        track["artist"] = [artist]
    if lyrics is not None:
        track["lyrics"] = [lyrics]
    if cover is not None and isinstance(track, FLAC):
        others = [picture for picture in track.pictures if picture.type != PictureType.COVER_FRONT]
        track.clear_pictures()
        for picture in others:
            track.add_picture(picture)
        track.add_picture(_picture(cover))
    elif cover is not None:
        pictures = []
        for encoded in track.get("metadata_block_picture", []):
            try:
                picture = Picture(b64decode(encoded, validate=True))
            except (MutagenError, ValueError, TypeError):
                pictures.append(encoded)
            else:
                if picture.type != PictureType.COVER_FRONT:
                    pictures.append(encoded)
        track["metadata_block_picture"] = [*pictures, b64encode(_picture(cover).write()).decode("ascii")]
    track.save()


def _write_tags(path, title, artist, cover, lyrics):
    if path.suffix.lower() == ".mp3":
        _write_mp3_tags(path, title, artist, cover, lyrics)
        return
    container = _CONTAINERS.get(path.suffix.lower())
    if container is None:
        raise ValueError("这个音频容器不支持标签写入。")
    info = _regular_file(path)
    scratch = None
    try:
        with path.open("rb") as source:
            if not os.path.samestat(info, os.fstat(source.fileno())):
                raise ValueError("音频文件已更改，请重试。")
            with NamedTemporaryFile(prefix=".audio-tags-", suffix=path.suffix, dir=path.parent, delete=False) as target:
                scratch = Path(target.name)
                shutil.copyfileobj(source, target, 128 * 1024)
        track = container(scratch)
        _update_container(track, title, artist, cover, lyrics)
        current = _regular_file(path)
        if not os.path.samestat(info, current) or (info.st_size, info.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
            raise ValueError("音频文件已更改，请重试。")
        os.replace(scratch, path)
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


def write_music_tags(path, title, artist, *, cover=None, lyrics=None):
    """Write supported audio tags and optional front art or UTF-8 lyrics."""
    path = Path(path)
    title, artist = _text(title), _text(artist)
    cover = _cover_data(cover) if cover is not None else None
    lyrics = _lyrics_text(lyrics) if lyrics is not None else None
    if title or artist or cover is not None or lyrics is not None:
        _write_tags(path, title, artist, cover, lyrics)


def copy_front_cover(source, output):
    """Keep a native front cover when converting to another audio container."""
    source, output = Path(source), Path(output)
    suffix = source.suffix.lower()
    container = MP3 if suffix == ".mp3" else _CONTAINERS.get(suffix)
    if container is None:
        return
    _regular_file(source)
    track = container(source)
    cover = None
    if isinstance(track, (MP3, WAVE)) and track.tags is not None:
        cover = next(((picture.mime, picture.data) for picture in track.tags.getall("APIC")
                      if picture.type == PictureType.COVER_FRONT), None)
    elif isinstance(track, MP4):
        covers = track.get("covr", [])
        if covers:
            mime = "image/png" if covers[0].imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
            cover = mime, bytes(covers[0])
    elif isinstance(track, FLAC):
        cover = next(((picture.mime, picture.data) for picture in track.pictures
                      if picture.type == PictureType.COVER_FRONT), None)
    elif isinstance(track, OggOpus):
        for encoded in track.get("metadata_block_picture", []):
            try:
                picture = Picture(b64decode(encoded, validate=True))
            except (MutagenError, ValueError, TypeError):
                continue
            if picture.type == PictureType.COVER_FRONT:
                cover = picture.mime, picture.data
                break
    if cover is None:
        return
    mime, data = cover
    if not 0 < len(data) <= _COVER_BYTES:
        return
    dimensions = _cover_dimensions(mime, data)
    if dimensions and all(1 <= size <= _COVER_DIMENSION for size in dimensions):
        _write_tags(output, "", "", cover, None)
