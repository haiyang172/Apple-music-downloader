"""Persist per-track download tasks independently of the desktop interface."""

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


STATES = frozenset({"pending", "running", "complete", "failed", "interrupted", "cancelled"})


@dataclass(frozen=True)
class QueueTask:
    id: int
    source: str
    url: str
    title: str
    artist: str
    state: str
    batch_title: str
    position: int | None
    total: int | None
    options: dict | None
    error: str
    files: tuple[str, ...]
    audio_title: str | None = None
    uploader: str = ""
    source_category: str = ""
    media_category: str = ""
    duration: float | None = None
    batch_key: str = ""
    song_edited: bool = False
    audio_details: tuple[dict, ...] = ()


def _audio_detail(item):
    return (isinstance(item, dict) and isinstance(item.get("filename"), str) and bool(item["filename"])
            and all(item.get(key) is None or isinstance(item.get(key), dict) for key in ("source", "output")))


def _read_audio_details(encoded):
    try:
        records = json.loads(encoded)
    except (TypeError, ValueError, RecursionError):
        return ()
    if not isinstance(records, list):
        return ()
    return tuple(item for item in records if _audio_detail(item))


def _task(row):
    if row is None:
        return None
    return QueueTask(
        id=row["id"], source=row["source"], url=row["url"], title=row["title"],
        artist=row["artist"], state=row["state"], batch_title=row["batch_title"],
        position=row["position"] or None, total=row["total"],
        options=json.loads(row["options"]) if row["options"] is not None else None,
        error=row["error"], files=tuple(json.loads(row["files"])),
        audio_title=row["audio_title"],
        uploader=row["uploader"], source_category=row["source_category"], media_category=row["media_category"],
        duration=row["duration"], batch_key=row["batch_key"], song_edited=bool(row["song_edited"]),
        audio_details=_read_audio_details(row["audio_details"]),
    )


def _options_json(options):
    if not isinstance(options, dict):
        raise TypeError("Download options must be a dictionary")
    return json.dumps(options, ensure_ascii=False, allow_nan=False)


class QueueStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    batch_key TEXT NOT NULL,
                    batch_title TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    total INTEGER,
                    options TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    files TEXT NOT NULL DEFAULT '[]',
                    audio_details TEXT NOT NULL DEFAULT '[]',
                    audio_title TEXT,
                    uploader TEXT NOT NULL DEFAULT '',
                    source_category TEXT NOT NULL DEFAULT '',
                    media_category TEXT NOT NULL DEFAULT '',
                    duration REAL,
                    song_edited INTEGER NOT NULL DEFAULT 0 CHECK (song_edited IN (0, 1)),
                    UNIQUE (source, url, batch_key, position)
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "audio_title" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN audio_title TEXT")
            if "audio_details" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN audio_details TEXT NOT NULL DEFAULT '[]'")
            if "duration" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN duration REAL")
            if "song_edited" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN song_edited INTEGER NOT NULL DEFAULT 0 CHECK (song_edited IN (0, 1))")
            for column in ("uploader", "source_category", "media_category"):
                if column not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _get(db, task_id):
        return _task(db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def add(self, items):
        """Add rows in input order, reusing existing rows with the same identity."""
        result = []
        with closing(self._connect()) as db, db:
            for item in items:
                source = item["source"].strip()
                url = item["url"].strip()
                if not source or not url:
                    raise ValueError("A download task needs a source and URL")
                batch_title = item.get("batch_title") or ""
                batch_key = item.get("batch_key", batch_title) or ""
                position = item.get("position") or 0
                total = item.get("total")
                options = item.get("options")
                edited = source == "bilibili" and item.get("song_edited") is True
                audio_title = item.get("audio_title")
                if edited and audio_title is None:
                    audio_title = ""
                db.execute("""
                    INSERT INTO tasks (source, url, title, artist, batch_key, batch_title,
                                       position, total, options, audio_title, uploader, source_category, media_category,
                                       duration, song_edited)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source, url, batch_key, position) DO UPDATE SET
                        total = COALESCE(excluded.total, tasks.total),
                        audio_title = CASE
                            WHEN excluded.song_edited THEN excluded.audio_title
                            WHEN tasks.song_edited THEN tasks.audio_title
                            ELSE COALESCE(NULLIF(excluded.audio_title, ''), tasks.audio_title, excluded.audio_title)
                        END,
                        artist = CASE
                            WHEN excluded.song_edited THEN excluded.artist
                            WHEN tasks.song_edited THEN tasks.artist
                            ELSE COALESCE(NULLIF(excluded.artist, ''), tasks.artist)
                        END,
                        song_edited = MAX(tasks.song_edited, excluded.song_edited),
                        uploader = COALESCE(NULLIF(excluded.uploader, ''), tasks.uploader),
                        source_category = COALESCE(NULLIF(excluded.source_category, ''), tasks.source_category),
                        media_category = COALESCE(NULLIF(excluded.media_category, ''), tasks.media_category),
                        duration = COALESCE(NULLIF(excluded.duration, 0), tasks.duration, excluded.duration)
                    WHERE NOT excluded.song_edited OR (tasks.state NOT IN ('running', 'complete') AND tasks.files = '[]')
                """, (source, url, item.get("title") or url, item.get("artist") or "",
                      batch_key, batch_title, position, total,
                      _options_json(options) if options is not None else None, audio_title,
                      item.get("uploader") or "", item.get("source_category") or "", item.get("media_category") or "",
                      item.get("duration"), int(edited)))
                row = db.execute("""
                    SELECT * FROM tasks WHERE source = ? AND url = ? AND batch_key = ? AND position = ?
                """, (source, url, batch_key, position)).fetchone()
                result.append(_task(row))
        return result

    def list_tasks(self):
        with closing(self._connect()) as db:
            return [_task(row) for row in db.execute("SELECT * FROM tasks ORDER BY id")]

    def get(self, task_id):
        with closing(self._connect()) as db:
            return self._get(db, task_id)

    def prepare(self, task_ids, options):
        """Capture options at first start and return existing tasks in the requested order."""
        task_ids = list(task_ids)
        encoded = _options_json(options)
        with closing(self._connect()) as db, db:
            db.executemany("""
                UPDATE tasks SET options = ? WHERE id = ? AND options IS NULL
                AND state IN ('pending', 'failed', 'interrupted', 'cancelled')
            """, ((encoded, task_id) for task_id in task_ids))
            return [task for task_id in task_ids if (task := self._get(db, task_id)) is not None]

    def set_state(self, task_id, state, error="", files=None, audio_details=None):
        """Update one task, optionally replacing its output paths and audio details."""
        if state not in STATES:
            raise ValueError(f"Unknown download task state: {state}")
        encoded_files = json.dumps([str(path) for path in files], ensure_ascii=False) if files is not None else None
        encoded_details = None
        if audio_details is not None:
            records = list(audio_details)
            if not all(_audio_detail(item) for item in records):
                raise TypeError("Invalid audio details")
            encoded_details = json.dumps(records, ensure_ascii=False, allow_nan=False)
        with closing(self._connect()) as db, db:
            db.execute("""
                UPDATE tasks SET state = ?, error = ?, files = COALESCE(?, files),
                                 audio_details = COALESCE(?, audio_details) WHERE id = ?
            """, (state, error, encoded_files, encoded_details, task_id))
            return self._get(db, task_id)

    def set_bilibili_metadata(self, task_id, item):
        with closing(self._connect()) as db, db:
            db.execute("""
                UPDATE tasks SET audio_title = ?, artist = ?,
                    uploader = COALESCE(NULLIF(?, ''), uploader), source_category = ?, media_category = ?
                WHERE id = ? AND source = 'bilibili' AND audio_title IS NULL
            """, (item["audio_title"], item["artist"], item["uploader"], item["source_category"],
                  item["media_category"], task_id))
            return self._get(db, task_id)

    def requeue(self, task_ids, options):
        """Apply new output options, keeping previously completed tasks as history."""
        encoded = _options_json(options)
        repeat = uuid4().hex
        updated = []
        with closing(self._connect()) as db, db:
            for task_id in task_ids:
                copy = db.execute("""
                    INSERT INTO tasks (source, url, title, artist, batch_key, batch_title,
                                       position, total, options, audio_title, uploader, source_category, media_category,
                                       duration, song_edited)
                    SELECT source, url, title, artist, batch_key || ?, batch_title,
                           position, total, ?, audio_title, uploader, source_category, media_category, duration, song_edited
                    FROM tasks WHERE id = ? AND state = 'complete'
                """, (f":repeat:{repeat}", encoded, task_id))
                if copy.rowcount:
                    updated.append(self._get(db, copy.lastrowid))
                    continue
                edit = db.execute("""
                    UPDATE tasks SET options = ?, state = 'pending', error = '', files = '[]', audio_details = '[]'
                    WHERE id = ? AND state != 'running'
                """, (encoded, task_id))
                if edit.rowcount:
                    updated.append(self._get(db, task_id))
        return updated

    def remove(self, task_ids):
        with closing(self._connect()) as db, db:
            return db.executemany("DELETE FROM tasks WHERE id = ?", ((task_id,) for task_id in task_ids)).rowcount

    def recover(self):
        with closing(self._connect()) as db, db:
            return db.execute("UPDATE tasks SET state = 'interrupted' WHERE state = 'running'").rowcount
