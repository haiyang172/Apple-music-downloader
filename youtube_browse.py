"""Read public YouTube lists lazily with an owned, isolated worker."""

import atexit
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
import subprocess
from tempfile import mkdtemp
from threading import Event, Lock, Thread
import time
import weakref

from engine import Cancelled
from youtube_download import _parse, _worker, _VIDEO_ID, _watch


_ROOT = Path(__file__).resolve().parent
_MAX_RESPONSE = 1024 * 1024
_PAGE_SIZE = 50
_TIMEOUT = 90
_LIVE = weakref.WeakSet()
_REGISTRY_LOCK = Lock()
_EXITING = Event()


def _query(value):
    value = " ".join(value.split()) if isinstance(value, str) else ""
    if not 0 < len(value) <= 120 or any(
            ord(char) < 32 or char == "\x7f" or "\ud800" <= char <= "\udfff" for char in value):
        raise ValueError("请输入 1–120 字的 YouTube 搜索关键词。")
    return value


def _valid_row(item):
    if not isinstance(item, dict):
        return False
    identifier = item.get("id")
    return (isinstance(identifier, str) and bool(_VIDEO_ID.fullmatch(identifier))
            and item.get("source") == "youtube" and item.get("url") == _watch(identifier)
            and type(item.get("position")) is int and 0 < item["position"] < 10 ** 10
            and all(isinstance(item.get(key), str) and len(item[key]) <= 300 for key in ("title", "artist")))


class _State:
    def __init__(self, process, temporary):
        self.process = process
        self.temporary = temporary
        self.replies = Queue(maxsize=2)
        self.retired = Event()
        self.retire_lock = Lock()
        self.reaper = None

    def put(self, value):
        while not self.retired.is_set():
            try:
                self.replies.put(value, timeout=0.1)
                return
            except Full:
                pass


def _read_replies(state):
    try:
        while not state.retired.is_set():
            line = state.process.stdout.readline(_MAX_RESPONSE + 1)
            if not line:
                state.put(("error", "YouTube 列表连接已结束，请重试。"))
                return
            if len(line) > _MAX_RESPONSE or not line.endswith(b"\n"):
                state.put(("error", "YouTube 列表信息过大或不完整，请重新查找。"))
                return
            state.put(("reply", line))
    except (OSError, ValueError):
        state.put(("error", "YouTube 列表连接中断，请重试。"))


def _retire(state, *, background=True):
    if state is None:
        return

    def finish():
        try:
            try:
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.process.kill()
                state.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            for pipe in (state.process.stdin, state.process.stdout):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                # No metadata command writes in this isolated working directory.
                # Remove only that exact empty directory, never recurse into files.
                state.temporary.rmdir()
            except OSError:
                pass
            with _REGISTRY_LOCK:
                _LIVE.discard(state)

    with state.retire_lock:
        if state.retired.is_set():
            return
        state.retired.set()
        # Flat metadata has JavaScript disabled and creates no child processes.
        # Kill only this owned helper now; pipe cleanup never waits on the UI.
        try:
            if state.process.poll() is None:
                state.process.terminate()
        except OSError:
            pass
        if background and not _EXITING.is_set():
            state.reaper = Thread(target=finish, daemon=True)
            try:
                state.reaper.start()
                return
            except RuntimeError:
                state.reaper = None
        finish()


def _close_holder(holder):
    state, holder[0] = holder[0], None
    _retire(state)


def _shutdown_workers():
    with _REGISTRY_LOCK:
        _EXITING.set()
        states = list(_LIVE)
    for state in states:
        _retire(state, background=False)
        if state.reaper is not None:
            state.reaper.join(timeout=5)


atexit.register(_shutdown_workers)


class YoutubeBrowser:
    """Keep one native continuation iterator; each page reads at most 50 slots.

    A failed request discards its helper. Retrying replays only after failure and
    verifies the acknowledged prefix before continuing, so changed lists cannot
    silently overwrite or skip already displayed positions.
    """

    def __init__(self, value, *, search=False):
        if type(search) is not bool:
            raise ValueError("无效的 YouTube 列表类型。")
        self.search = search
        if search:
            self.value = _query(value)
        else:
            kind, _, self.value = _parse(value)
            if kind != "playlist":
                raise ValueError("请提供 YouTube 播放列表链接。")
        self.cursor = 0
        self.done = False
        self.title = self.value
        self.total = None
        self._digest = ""
        self._sequence = 0
        self._closed = Event()
        self._page_lock = Lock()
        self._holder = [None]

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _check(self, stop):
        if self._closed.is_set() or stop.is_set():
            raise Cancelled("已取消 YouTube 列表读取。")

    def _start(self, stop):
        self._check(stop)
        command = _worker(search=self.search)
        helper = _ROOT / "youtube_browse_worker.py"
        if not helper.is_file():
            raise RuntimeError("YouTube 列表组件不完整，请运行新版安装程序。")
        # Reuse the pinned component lookup and the existing isolated Python.
        # The worker builds a fixed flat-metadata configuration, never user flags.
        arguments = [*command[:3], "-B", str(helper), str(command[3]),
                     "search" if self.search else "playlist", str(self.cursor), self._digest, self.value]
        temporary = Path(mkdtemp(prefix="amdl-youtube-list-")).resolve()
        with _REGISTRY_LOCK:
            if _EXITING.is_set():
                temporary.rmdir()
                raise Cancelled("已取消 YouTube 列表读取。")
            try:
                process = subprocess.Popen(arguments, cwd=temporary, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                temporary.rmdir()
                raise RuntimeError("无法启动 YouTube 列表组件。") from None
            state = _State(process, temporary)
            _LIVE.add(state)
        self._holder[0] = state
        Thread(target=_read_replies, args=(state,), daemon=True).start()
        self._check(stop)
        return state

    def close(self):
        """Cancel metadata reads and release the owned child without waiting on UI."""
        self._closed.set()
        _close_holder(self._holder)

    def page(self, *, stop=None, limit=_PAGE_SIZE):
        """Return a bounded raw-slot page, including empty filtered pages."""
        if type(limit) is not int or not 1 <= limit <= _PAGE_SIZE:
            raise ValueError("YouTube 每次读取数量应为 1–50。")
        stop = stop if stop is not None else Event()
        if not self._page_lock.acquire(blocking=False):
            raise RuntimeError("正在读取 YouTube 列表，请稍后再试。")
        state = None
        try:
            self._check(stop)
            if self.done:
                return {"source": "youtube", "title": self.title, "rows": [], "total": self.total,
                        "next_offset": None}
            state = self._holder[0] or self._start(stop)
            self._check(stop)
            self._sequence += 1
            request = {"op": "next", "id": self._sequence, "limit": limit}
            try:
                state.process.stdin.write((json.dumps(request) + "\n").encode("ascii"))
                state.process.stdin.flush()
            except (OSError, ValueError):
                raise RuntimeError("YouTube 列表连接中断，请重试。") from None
            deadline = time.monotonic() + _TIMEOUT
            while True:
                self._check(stop)
                if time.monotonic() >= deadline:
                    raise RuntimeError("YouTube 列表读取超时，请重试。")
                try:
                    kind, payload = state.replies.get(timeout=0.1)
                    break
                except Empty:
                    pass
            self._check(stop)
            if kind == "error":
                raise RuntimeError(payload)
            try:
                reply = json.loads(payload)
            except (ValueError, UnicodeError, RecursionError):
                raise RuntimeError("YouTube 列表信息无法读取，请重试。") from None
            if not isinstance(reply, dict) or reply.get("id") != self._sequence:
                raise RuntimeError("YouTube 列表响应顺序有误，请重新查找。")
            error = reply.get("error")
            if error:
                if not isinstance(error, str) or len(error) > 240 or not error.isprintable():
                    error = "YouTube 列表读取失败，请重试。"
                raise RuntimeError(error)
            result = reply.get("result")
            cursor, digest, more = reply.get("cursor"), reply.get("digest"), reply.get("more")
            if (not isinstance(result, dict) or not isinstance(result.get("rows"), list)
                    or len(result["rows"]) > limit or any(not _valid_row(item) for item in result["rows"])
                    or type(cursor) is not int
                    or not self.cursor <= cursor <= self.cursor + limit or type(more) is not bool
                    or (more and cursor == self.cursor) or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not isinstance(result.get("title"), str) or len(result["title"]) > 300
                    or result.get("source") != "youtube"
                    or (result.get("total") is not None and (type(result["total"]) is not int
                                                             or result["total"] < 0))):
                raise RuntimeError("YouTube 列表返回了无效的分页信息，请重新查找。")
            self.cursor, self._digest, self.done = cursor, digest, not more
            self.title, self.total = result["title"], result.get("total")
            result["next_offset"] = cursor if more else None
            if self.done:
                _close_holder(self._holder)
            return result
        except BaseException:
            if state is None or self._holder[0] is state:
                _close_holder(self._holder)
            else:
                _retire(state)
            raise
        finally:
            self._page_lock.release()
