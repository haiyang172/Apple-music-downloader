"""Private flat-metadata iterator process; commands and replies are bounded JSON."""

from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from youtube_download import _eligible, _label, _parse, _public_error, _row, _VIDEO_ID


_PAGE_SIZE = 50
_COMMAND_BYTES = 512
_UNSET = object()


def _clean_label(value, fallback=""):
    value = _label(value, fallback)
    return value.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


def _positive_position(value):
    return value if type(value) is int and 0 < value < 10 ** 10 else None


def _preserve_positions(extractor):
    """Retain renderer positions discarded by the pinned flat extractor."""
    original = extractor._extract_video

    def extract_video(renderer):
        result = original(renderer)
        if isinstance(result, dict) and isinstance(renderer, dict):
            text = extractor._get_text(renderer, "index")
            position = int(text) if isinstance(text, str) and re.fullmatch(r"[0-9]{1,10}", text) else None
            if not _positive_position(position):
                endpoint = renderer.get("navigationEndpoint")
                endpoint = endpoint.get("watchEndpoint") if isinstance(endpoint, dict) else None
                index = endpoint.get("index") if isinstance(endpoint, dict) else None
                position = index + 1 if type(index) is int and 0 <= index < 10 ** 10 else None
            if _positive_position(position):
                result["playlist_index"] = position
        return result

    extractor._extract_video = extract_video
    original_lockup = extractor._extract_lockup_view_model

    def extract_lockup(renderer):
        result = original_lockup(renderer)
        if isinstance(result, dict) and isinstance(renderer, dict):
            endpoint = renderer
            for key in ("rendererContext", "commandContext", "onTap", "innertubeCommand", "watchEndpoint"):
                endpoint = endpoint.get(key) if isinstance(endpoint, dict) else None
            index = endpoint.get("index") if isinstance(endpoint, dict) else None
            if type(index) is int and 0 <= index < 10 ** 10:
                result["playlist_index"] = index + 1
        return result

    extractor._extract_lockup_view_model = extract_lockup


class _Paginator:
    def __init__(self, payload, *, search, identifier, fallback, offset=0, digest=""):
        if not isinstance(payload, dict) or payload.get("_type") not in ("playlist", "multi_video"):
            raise RuntimeError("YouTube 未提供可读取的公开列表。")
        if not search and payload.get("id") != identifier:
            raise RuntimeError("YouTube 返回的播放列表与请求链接不符。")
        entries = payload.get("entries")
        if entries is None or isinstance(entries, (str, bytes, dict)):
            raise RuntimeError("YouTube 未提供可读取的公开列表。")
        self.entries = iter(entries)
        self.search, self.identifier = search, identifier
        self.title = _clean_label(payload.get("title"), fallback)
        count = payload.get("playlist_count")
        self.total = count if type(count) is int and count >= 0 else None
        self.cursor = 0
        self.prefix = sha256()
        self.pending = _UNSET
        self.finished = False
        self.last_position = 0
        for _ in range(offset):
            try:
                item = next(self.entries)
            except StopIteration:
                raise RuntimeError("YouTube 列表已变化，请重新查找后继续。") from None
            self.cursor += 1
            self._hash(self.prefix, item, self.cursor)
            if isinstance(item, dict):
                self.last_position = max(self.last_position, _positive_position(item.get("playlist_index")) or 0)
        if offset and self.prefix.hexdigest() != digest:
            raise RuntimeError("YouTube 列表已变化，请重新查找后继续。")

    @staticmethod
    def _hash(prefix, item, cursor):
        identifier = item.get("id") if isinstance(item, dict) else None
        identifier = identifier[:128] if isinstance(identifier, str) else None
        position = _positive_position(item.get("playlist_index")) if isinstance(item, dict) else None
        prefix.update(json.dumps([cursor, identifier, position], ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        prefix.update(b"\n")

    def page(self, limit):
        rows, skipped = [], 0
        cursor, prefix = self.cursor, self.prefix.copy()
        for _ in range(limit):
            if self.finished:
                break
            try:
                item = self.pending if self.pending is not _UNSET else next(self.entries)
                self.pending = _UNSET
            except StopIteration:
                self.finished = True
                break
            cursor += 1
            self._hash(prefix, item, cursor)
            if (not isinstance(item, dict) or not _eligible(item, flat=True)
                    or (self.search and item.get("availability") == "unlisted")):
                skipped += 1
                continue
            video_id = item.get("id")
            if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id) or item.get("ie_key") not in (None, "Youtube"):
                skipped += 1
                continue
            position = _positive_position(item.get("playlist_index"))
            if not self.search and position is None:
                raise RuntimeError("YouTube 未提供可确认的播放列表位置，请刷新后重试。")
            position = position or cursor
            self.last_position = max(self.last_position, position)
            row = _row(item, video_id, position, self.total,
                       "" if self.search else f"youtube:{self.identifier}", "" if self.search else self.title)
            row["title"], row["artist"] = _clean_label(row["title"], video_id), _clean_label(row["artist"])
            rows.append(row)
        if not self.finished:
            try:
                self.pending = next(self.entries)
            except StopIteration:
                self.finished = True
        self.cursor, self.prefix = cursor, prefix
        if self.finished and self.total is None:
            self.total = max(cursor, self.last_position)
        if self.total is not None:
            self.total = max(self.total, cursor, self.last_position)
            for item in rows:
                item["total"] = self.total
        result = {"source": "youtube", "title": self.title, "rows": rows, "total": self.total}
        warnings = []
        if skipped:
            warnings.append(f"已跳过 {skipped} 个不可用视频")
        if self.finished and self.total is not None and cursor < self.total:
            warnings.append(f"平台返回 {cursor} 个可识别位置，共 {self.total} 项")
        if warnings:
            result["warning"] = " · ".join(warnings)
        return {"result": result, "cursor": cursor, "digest": prefix.hexdigest(), "more": not self.finished}


class _SilentLogger:
    def debug(self, _message):
        pass

    warning = error = debug


def _open_iterator(archive, mode, value, offset, digest):
    sys.path.insert(0, str(archive))
    import yt_dlp
    from yt_dlp.globals import plugin_dirs

    plugin_dirs.value = []
    search = mode == "search"
    if search:
        from youtube_browse import _query
        value = _query(value)
        identifier, url = "", "ytsearchall:" + value
    else:
        kind, identifier, url = _parse(value)
        if kind != "playlist":
            raise ValueError("请提供 YouTube 播放列表链接。")
    options = yt_dlp.parse_options([
        "--ignore-config", "--no-plugin-dirs", "--no-remote-components", "--no-js-runtimes",
        "--no-cache-dir", "--no-exec", "--encoding", "utf-8", "--socket-timeout", "10",
        "--retries", "1", "--extractor-retries", "1", "--flat-playlist", "--lazy-playlist",
        "--skip-download", "--use-extractors", "youtube,youtube:search,youtube:tab,youtube:playlist",
        "--extractor-args", "youtube:raise_incomplete_data=true", "--", url,
    ]).ydl_opts
    options.update({"quiet": True, "no_warnings": True, "ignoreerrors": False, "logger": _SilentLogger()})
    downloader = yt_dlp.YoutubeDL(options)
    try:
        if not search:
            _preserve_positions(downloader.get_info_extractor("YoutubeTab"))
        payload = downloader.extract_info(url, download=False, process=False)
        paginator = _Paginator(payload, search=search, identifier=identifier, fallback=value, offset=offset, digest=digest)
    except BaseException:
        downloader.close()
        raise
    return downloader, paginator


def main():
    if len(sys.argv) != 6:
        return 2
    archive, mode, raw_offset, digest, value = sys.argv[1:]
    archive = Path(archive)
    if (not archive.is_file() or archive.suffix != ".pyz" or mode not in ("playlist", "search")
            or not re.fullmatch(r"[0-9]{1,12}", raw_offset)
            or (digest and not re.fullmatch(r"[0-9a-f]{64}", digest))):
        return 2
    offset = int(raw_offset)
    if bool(offset) != bool(digest):
        return 2
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    paginator = downloader = None
    try:
        with open(os.devnull, "w", encoding="utf-8") as silence:
            while True:
                line = sys.stdin.buffer.readline(_COMMAND_BYTES + 1)
                if not line:
                    return 0
                if len(line) > _COMMAND_BYTES or not line.endswith(b"\n"):
                    return 2
                try:
                    command = json.loads(line)
                except (ValueError, UnicodeError):
                    return 2
                if (not isinstance(command, dict) or set(command) != {"op", "id", "limit"}
                        or command.get("op") != "next" or type(command.get("id")) is not int
                        or not 1 <= command["id"] < 10 ** 12 or type(command.get("limit")) is not int
                        or not 1 <= command["limit"] <= _PAGE_SIZE):
                    return 2
                try:
                    with redirect_stdout(silence), redirect_stderr(silence):
                        if paginator is None:
                            downloader, paginator = _open_iterator(archive, mode, value, offset, digest)
                        reply = paginator.page(command["limit"])
                    reply["id"] = command["id"]
                except (Exception, SystemExit) as error:
                    own_errors = {"YouTube 未提供可读取的公开列表。", "YouTube 返回的播放列表与请求链接不符。",
                                  "YouTube 列表已变化，请重新查找后继续。",
                                  "YouTube 未提供可确认的播放列表位置，请刷新后重试。"}
                    if isinstance(error, RuntimeError) and str(error) in own_errors:
                        message = str(error)
                    else:
                        message = _public_error(str(error).encode("utf-8", "ignore"))
                    print(json.dumps({"id": command["id"], "error": message}, ensure_ascii=False), flush=True)
                    return 1
                print(json.dumps(reply, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
    finally:
        if downloader is not None:
            downloader.close()


if __name__ == "__main__":
    raise SystemExit(main())
