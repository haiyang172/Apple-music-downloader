"""Playlist update tracking: queue only tracks a playlist batch has not seen before.

PlaylistWatch keeps a small journal under the app data directory. Each batch is
keyed by the same stable batch_key the queue uses, and stores the set of
canonical song URLs that were queued before. diff() splits a freshly resolved
playlist into new rows and the complete current URL set; commit() persists the
complete set, so later syncs only enqueue tracks added since.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from shared_music import _parse_link

_MAX_BATCHES = 512
_MAX_TRACKS = 20_000
_MAX_FILE = 16 * 1024 * 1024
_BATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9:.@_-]{0,199}")
_SONG = re.compile(r"[1-9][0-9]{0,19}")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_url(value):
    try:
        provider, kind, _, url = _parse_link(value)
    except ValueError:
        raise ValueError("歌单曲目链接无效。") from None
    if provider not in ("qq", "netease") or kind != "song":
        raise ValueError("追更只支持 QQ 音乐和网易云音乐的单曲。")
    return url


class PlaylistWatch:
    """Durable per-batch record of previously queued playlist tracks."""

    def __init__(self, path):
        self.path = Path(path)
        self.batches = {}

    def load(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_FILE + 1)
        except FileNotFoundError:
            self.batches = {}
            return self
        except OSError:
            raise ValueError("无法读取歌单追更记录。") from None
        if len(raw) > _MAX_FILE:
            raise ValueError("歌单追更记录过大。")
        try:
            payload = json.loads(raw)
            assert isinstance(payload, dict) and isinstance(payload.get("batches"), dict)
        except (ValueError, AssertionError, RecursionError):
            raise ValueError("歌单追更记录无效。") from None
        batches = {}
        for batch_key, entry in payload["batches"].items():
            if (not _BATCH.fullmatch(str(batch_key)) or not isinstance(entry, dict)
                    or not isinstance(entry.get("tracks"), (dict, list))):
                raise ValueError("歌单追更记录无效。")
            values = entry["tracks"].keys() if isinstance(entry["tracks"], dict) else entry["tracks"]
            tracks = set()
            for url in values:
                if not isinstance(url, str):
                    raise ValueError("歌单追更记录无效。")
                try:
                    tracks.add(_canonical_url(url))
                except ValueError:
                    raise ValueError("歌单追更记录无效。") from None
            if len(tracks) > _MAX_TRACKS:
                raise ValueError("歌单追更记录超过上限。")
            batches[batch_key] = {"updated_at": str(entry.get("updated_at", ""))[:32], "tracks": tracks}
        self.batches = batches
        return self

    def save(self):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix="playlist-watch-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "batches": {key: {"updated_at": entry["updated_at"],
                                                            "tracks": sorted(entry["tracks"])}
                                                     for key, entry in self.batches.items()}},
                          stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ValueError("无法保存歌单追更记录。") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self

    def diff(self, batch_key, rows):
        """Split rows into (new_rows, complete_urls) for one stable batch key."""
        if not isinstance(batch_key, str) or not _BATCH.fullmatch(batch_key):
            raise ValueError("歌单队列标识无效。")
        if not isinstance(rows, (list, tuple)):
            raise ValueError("歌单曲目无效。")
        known = self.batches.get(batch_key, {}).get("tracks", set())
        new_rows, complete = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("歌单曲目无效。")
            url = _canonical_url(row.get("url"))
            if url in complete:
                continue
            complete.add(url)
            if url not in known:
                new_rows.append(dict(row))
        if len(complete) > _MAX_TRACKS:
            raise ValueError("歌单超过追更记录上限，请分批处理。")
        return new_rows, complete

    def commit(self, batch_key, urls):
        """Persist the complete current URL set for a batch, newest first."""
        if not isinstance(batch_key, str) or not _BATCH.fullmatch(batch_key):
            raise ValueError("歌单队列标识无效。")
        tracks = set()
        for value in urls:
            tracks.add(_canonical_url(value))
        if not tracks or len(tracks) > _MAX_TRACKS:
            raise ValueError("歌单追更记录超出允许范围。")
        self.batches[batch_key] = {"updated_at": _now(), "tracks": tracks}
        if len(self.batches) > _MAX_BATCHES:
            oldest = sorted(self.batches, key=lambda key: self.batches[key]["updated_at"])
            for key in oldest[:len(self.batches) - _MAX_BATCHES]:
                del self.batches[key]
        self.save()
        return self

    def forget(self, batch_key):
        if batch_key in self.batches:
            del self.batches[batch_key]
            self.save()
        return self
