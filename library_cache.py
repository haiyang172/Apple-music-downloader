"""Store complete list snapshots for fast local browsing."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3


def _validate_rows(rows):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid library snapshot")


class LibraryCache:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS library_snapshots (
                    scope TEXT PRIMARY KEY NOT NULL,
                    rows_json TEXT NOT NULL
                ) WITHOUT ROWID
            """)
            yield db

    def load(self, scope):
        """Return a snapshot in source order, or None if none has been saved."""
        with self._database() as db:
            record = db.execute("SELECT rows_json FROM library_snapshots WHERE scope = ?", (scope,)).fetchone()
        if record is None:
            return None
        rows = json.loads(record[0])
        _validate_rows(rows)
        return rows

    def save(self, scope, rows):
        """Atomically replace a complete snapshot. Scope must not contain a credential."""
        _validate_rows(rows)
        try:
            encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        except TypeError:
            raise ValueError("Invalid library snapshot") from None
        with self._database() as db, db:
            changed = db.execute("""
                INSERT INTO library_snapshots (scope, rows_json) VALUES (?, ?)
                ON CONFLICT (scope) DO UPDATE SET rows_json = excluded.rows_json
                WHERE library_snapshots.rows_json != excluded.rows_json
            """, (scope, encoded)).rowcount
        return bool(changed)

    def discard(self, scope):
        """Remove just the selected snapshot."""
        with self._database() as db, db:
            changed = db.execute("DELETE FROM library_snapshots WHERE scope = ?", (scope,)).rowcount
        return bool(changed)
