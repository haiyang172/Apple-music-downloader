"""Reusable queue controller shared by the Qt pages.

This mirrors the download orchestration in Studio.py without the Tkinter UI, so
the migrating Qt shell can drive the same queue, engine and local conversion
pipeline the original interface uses.
"""

import hashlib
import os
import threading
from pathlib import Path
from queue import SimpleQueue

import settings
from apple_download import download_options, run_tasks
from download_queue import QueueStore
from engine import Engine
from login_service import LoginService


def backend_home(root):
    """Locate the bundled apmyx backend and FFmpeg, as Studio.find_backend does."""
    candidates = [root / "backend",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "apmyx",
                  root.parent / "GUI-downloader"]
    bridge = Path("_internal/backend/bin/bridge/bridge.exe")
    for folder in candidates:
        if (folder / bridge).is_file() and (folder / "tools/ffmpeg.exe").is_file():
            return folder
    return root / "backend"


class QtApp:
    """Owns the queue store, engine and login service for the Qt shell."""

    def __init__(self, root):
        self.root = Path(root)
        self.preferences = settings.load()
        self.queue_store = QueueStore(settings.DATA / "downloads.sqlite3")
        self.login_service = LoginService(self.root, settings.DATA)
        self._engine = None
        self.busy = False
        self.stop = threading.Event()
        self.messages = SimpleQueue()

    def engine(self):
        if self._engine is None:
            self._engine = Engine(self.root, backend_home(self.root))
        return self._engine

    def list_tasks(self):
        return self.queue_store.list_tasks()

    def queue_local(self, paths, output, folder):
        """Enqueue local files for conversion and return the new task ids."""
        rows = []
        for value in paths:
            path = Path(value)
            info = path.stat()
            destination = str(Path(folder) if folder else path.parent)
            options = download_options({**self.preferences, "folder": destination, "category": ""})
            options.update(local_format=output, local_size=info.st_size, local_mtime=info.st_mtime_ns)
            key = f"{os.path.normcase(destination)}\0{output}\0{info.st_size}\0{info.st_mtime_ns}"
            batch = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
            rows.append({"source": "local", "url": str(path), "title": path.name,
                         "batch_key": batch, "batch_title": output, "options": options})
        tasks = self.queue_store.add(rows)
        return [task.id for task in tasks]

    def enqueue(self, rows):
        """Add prepared platform rows and attach the current download options."""
        tasks = self.queue_store.add(rows)
        ids = [task.id for task in tasks if task.options is None]
        if ids:
            self.queue_store.prepare(ids, download_options(self.preferences))
        return [task.id for task in tasks]

    def start(self, task_ids=None):
        """Run pending tasks in a worker thread and stream status via messages."""
        if self.busy:
            return
        if task_ids is None:
            tasks = self.list_tasks()
        else:
            tasks = [self.queue_store.get(task_id) for task_id in task_ids]
        tasks = [task for task in tasks if task.state not in ("running", "complete")]
        if not tasks:
            self.messages.put(("status", "队列里没有待下载项目。"))
            return
        self.busy = True
        self.stop = threading.Event()
        engine = self.engine()

        def run():
            try:
                result = run_tasks(self.queue_store, tasks, self.preferences, engine,
                                   self.login_service, self.stop, self._post)
                self.messages.put(("done", result))
            except Exception as error:
                self.messages.put(("failed", str(error)))

        try:
            threading.Thread(target=run, daemon=True).start()
        except (OSError, RuntimeError) as error:
            self.busy = False
            self.messages.put(("failed", f"无法启动下载：{error}"))

    def cancel(self):
        self.stop.set()

    def remove(self, task_ids):
        self.queue_store.remove(task_ids)

    def _post(self, kind, value):
        self.messages.put((kind, value))

    def close(self):
        self.cancel()
        try:
            if self._engine is not None:
                self._engine.close_service()
        except (OSError, RuntimeError):
            pass
        try:
            self.login_service.close()
        except (OSError, RuntimeError):
            pass
