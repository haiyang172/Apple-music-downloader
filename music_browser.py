"""Search and library panes for the amdl desktop window."""

from collections import OrderedDict
import base64
from pathlib import Path
import struct
import subprocess
import threading
import tkinter as tk
from tkinter import font, ttk
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from music_catalog import CatalogClient
from music_library import LibraryClient


FILTERS = {"song": "歌曲", "album": "专辑", "playlist": "歌单"}
PLACEHOLDER = "搜索歌曲、艺人或专辑"


def _duration(value):
    if not isinstance(value, (int, float)) or not 0 < value < 86400:
        return "—"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}:{seconds:02}"


def _apple_music_link(value):
    try:
        url = urlsplit(value)
        return url.scheme == "https" and url.netloc.lower() == "music.apple.com"
    except (TypeError, ValueError):
        return False


def _apple_art_url(value):
    try:
        url = urlsplit(value)
        hostname = url.hostname or ""
        return (url.scheme == "https" and hostname.endswith(".mzstatic.com") and
                url.username is None and url.password is None and url.port in (None, 443))
    except (TypeError, ValueError):
        return False


class _DirectAppleRequest(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _apple_data(url, size, timeout=5):
    request = Request(url, headers={"User-Agent": "amdl"})
    with build_opener(_DirectAppleRequest()).open(request, timeout=timeout) as response:
        data = response.read(size + 1)
    if len(data) > size:
        raise ValueError("Apple Music 媒体文件超出预期大小。")
    return data


def _thumbnail(url):
    if not _apple_art_url(url):
        return b""
    try:
        data = _apple_data(url, 192 * 1024)
    except (OSError, ValueError):
        return b""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return b""
    if any(not 1 <= dimension <= 96 for dimension in struct.unpack(">II", data[16:24])):
        return b""
    return base64.b64encode(data)


def prepare_preview(url, ffmpeg, directory):
    try:
        target = urlsplit(url)
        host = target.hostname or ""
        if (target.scheme != "https" or not host.endswith((".mzstatic.com", ".itunes.apple.com"))
                or target.netloc.lower() != host):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("这首歌没有可用的试听。") from None
    directory = Path(directory)
    source = directory / "preview.audio"
    output = directory / "preview.wav"
    try:
        source.write_bytes(_apple_data(url, 5 * 1024 * 1024, timeout=10))
        subprocess.run(
            [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
             "-map", "0:a:0", "-t", "35", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(output)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=15,
        )
        if output.stat().st_size < 128:
            raise ValueError()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError("无法播放这首歌的试听。") from None
    return output


class MusicBrowser:
    def __init__(self, app, page, *, private=False):
        self.app = app
        self.private = private
        self.catalog = app.catalog
        self.page = page
        self.kind = "song"
        self.collection = None
        self.term = ""
        self.loaded = False
        self.busy = False
        self.serial = 0
        self.cursor = None
        self.rows = {}
        self.thumbnails = OrderedDict()
        self.art_serial = 0
        self.column_sizes = ()

        page.rowconfigure(3, weight=1)
        bar = ttk.Frame(page, style="Surface.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        self.query = tk.StringVar(page)
        if private:
            app.label(bar, "资料库", large=True, background=app.background).grid(row=0, column=0, sticky="w")
            self.connect_button = app.action(bar, "连接资料库", app.connect_library, primary=True)
            self.connect_button.grid(row=0, column=1, padx=(8, 0))
            app.action(bar, "刷新", self.refresh).grid(row=0, column=2, padx=(8, 0))
        else:
            self.entry = ttk.Entry(bar, textvariable=self.query, style="MusicSearch.TEntry",
                                   font=(app.font, 13))
            self.entry.grid(row=0, column=0, sticky="ew")
            self.entry.bind("<Return>", self.search)
            self.entry.bind("<FocusIn>", self._focus)
            self.entry.bind("<FocusOut>", self._blur)
            self._blur()
            app.action(bar, "搜索", self.search, primary=True).grid(row=0, column=1, padx=(8, 0))

        toolbar = ttk.Frame(page, style="Surface.TFrame")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(13, 9))
        toolbar.columnconfigure(4, weight=1)
        self.back_button = app.action(toolbar, "‹ 返回", self.back)
        self.back_button.grid(row=0, column=0, padx=(0, 7))
        self.back_button.grid_remove()
        self.filters = {}
        for column, (kind, label) in enumerate(FILTERS.items(), 1):
            button = app.action(toolbar, label, lambda choice=kind: self.select_kind(choice))
            button.grid(row=0, column=column, padx=(0, 6))
            self.filters[kind] = button
        self._update_filters()
        self.queue_button = app.action(toolbar, "加入队列", self.add_selection)
        self.queue_button.grid(row=0, column=5, padx=(4, 7))
        self.download_button = app.action(toolbar, "下载所选", lambda: self.add_selection(begin=True), primary=True)
        self.download_button.grid(row=0, column=6)

        self.caption = tk.StringVar(page, "资料库" if private else "热门歌曲")
        app.label(page, variable=self.caption, background=app.background, muted=True).grid(
            row=2, column=0, sticky="w", pady=(0, 5))
        result = ttk.Frame(page, style="Surface.TFrame")
        result.grid(row=3, column=0, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(result, columns=("artist", "album", "duration"), show="tree headings",
                                 selectmode="extended", style="Music.Treeview")
        self.row_font = font.Font(root=self.tree, family=app.font, size=11)
        self.tree.heading("#0", text="名称", anchor="w")
        self.tree.heading("artist", text="艺人", anchor="w")
        self.tree.heading("album", text="专辑", anchor="w")
        self.tree.heading("duration", text="时长")
        self.tree.column("#0", width=260, minwidth=155, stretch=True)
        self.tree.column("artist", width=140, minwidth=85)
        self.tree.column("album", width=152, minwidth=90)
        self.tree.column("duration", width=63, minwidth=50, stretch=False, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(result, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        self.tree.bind("<Double-1>", self.activate)
        self.tree.bind("<Return>", self.activate)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Button-3>", self.context_menu)
        self.tree.bind("<Configure>", self.reflow)
        self.tree.bind("<ButtonRelease-1>", self.reflow, add="+")

        foot = ttk.Frame(page, style="Surface.TFrame")
        foot.grid(row=4, column=0, sticky="ew", pady=(7, 0))
        self.status = tk.StringVar(page)
        app.label(foot, variable=self.status, muted=True, background=app.background).pack(side="left", fill="x")
        self.more = app.action(foot, "更多", lambda: self.refresh(append=True))
        self.stop_preview = app.action(foot, "停止试听", app.stop_preview)
        self._selection_changed()

    def _focus(self, _event=None):
        if self.query.get() == PLACEHOLDER:
            self.query.set("")

    def _blur(self, _event=None):
        if not self.query.get().strip():
            self.query.set(PLACEHOLDER)

    def ensure_loaded(self):
        if not self.loaded and not self.busy:
            self.refresh()

    def search(self, _event=None):
        term = self.query.get().strip()
        if term != PLACEHOLDER and term.startswith("https://"):
            self.app.add_link_text(term, begin=False)
            return
        self.term = term if term != PLACEHOLDER else ""
        self.collection = None
        self.refresh()

    def select_kind(self, kind):
        self.kind = kind
        self.collection = None
        self._update_filters()
        self.refresh()

    def _update_filters(self):
        for kind, button in self.filters.items():
            button.configure(style="Selected.TButton" if kind == self.kind else "TButton")

    def back(self):
        self.collection = None
        self.refresh()

    def invalidate(self):
        self.serial += 1
        self.art_serial += 1
        self.busy = self.loaded = False
        self.collection = None
        self.cursor = None
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.more.pack_forget()
        self.back_button.grid_remove()
        self.status.set("")
        self._selection_changed()

    def refresh(self, append=False):
        if append and (self.busy or not self.cursor):
            return
        self.loaded = True
        self.serial += 1
        self.art_serial += 1
        serial = self.serial
        collection = self.collection
        cursor = self.cursor if append else None
        self.more.pack_forget()
        if not append:
            self.rows.clear()
            self.tree.delete(*self.tree.get_children())
        self.cursor = None
        self.busy = True
        self._selection_changed()
        if collection:
            self.caption.set(collection["title"])
            self.back_button.grid()
        else:
            label = FILTERS[self.kind]
            self.caption.set(label if self.private else f"“{self.term}” · {label}" if self.term else f"热门{label}")
            self.back_button.grid_remove()
        show_song_details = bool(collection) or self.kind == "song"
        self.tree.configure(displaycolumns=("artist", "album", "duration") if show_song_details else ("artist",))
        self.tree.heading("artist", text="创建者" if not show_song_details and self.kind == "playlist" else "艺人")
        token = ""
        if self.private:
            try:
                token = self.app.library_token()
            except ValueError as error:
                self.receive(serial, None, None, str(error))
                return
            if not token:
                self.receive(serial, None, None, "连接 Apple Music 资料库后即可查看。")
                return
        storefront = self.app.catalog_region()
        term = self.term
        kind = self.kind
        self.status.set("正在加载…")

        def fetch():
            try:
                if self.private:
                    client = LibraryClient(token, storefront, catalog=self.catalog)
                    if collection:
                        rows, next_cursor = client.tracks(collection, cursor=cursor)
                    else:
                        rows, next_cursor = client.items(kind, cursor=cursor)
                elif collection:
                    rows, next_cursor = self.catalog.tracks(collection, storefront), None
                elif term:
                    rows, next_cursor = self.catalog.search(term, storefront, kind), None
                else:
                    rows, next_cursor = self.catalog.charts(storefront, kind), None
                self.app.post("browser_result", (self, serial, rows, next_cursor, ""))
            except (OSError, RuntimeError, ValueError) as error:
                self.app.post("browser_result", (self, serial, None, None, str(error)))

        threading.Thread(target=fetch, daemon=True).start()

    def receive(self, serial, rows, cursor, error):
        if serial != self.serial:
            return
        self.busy = False
        self.cursor = cursor
        if rows is None:
            self.status.set(error)
            self._selection_changed()
            return
        for item in rows:
            text, values = self.cells(item)
            item_id = self.tree.insert("", "end", text=text, values=values)
            self.rows[item_id] = item
        self.status.set(f"{len(self.rows)} 项" if self.rows else "没有结果")
        if cursor:
            self.more.pack(side="right")
        self._selection_changed()
        self._load_art()

    def clipped(self, text, width):
        if self.row_font.measure(text) <= width:
            return text
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.row_font.measure(text[:middle] + "…") <= width:
                low = middle
            else:
                high = middle - 1
        return text[:low] + "…"

    def cells(self, item):
        artist = self.clipped(item.get("artist") or "—", self.tree.column("artist", "width") - 14)
        album = self.clipped(item.get("album") or "—", self.tree.column("album", "width") - 14)
        padding = 61 if item.get("artwork_url") else 14
        title = self.clipped(item["title"], self.tree.column("#0", "width") - padding)
        return title, (artist, album, _duration(item.get("duration")))

    def reflow(self, _event=None):
        sizes = tuple(self.tree.column(name, "width") for name in ("#0", "artist", "album"))
        if sizes == self.column_sizes:
            return
        self.column_sizes = sizes
        for row, item in self.rows.items():
            text, values = self.cells(item)
            self.tree.item(row, text=text, values=values)

    def _load_art(self):
        queue = []
        for row, item in self.rows.items():
            url = item.get("artwork_url")
            if not url:
                continue
            if url in self.thumbnails:
                self.thumbnails.move_to_end(url)
                self.tree.item(row, image=self.thumbnails[url])
            else:
                queue.append((row, url))
        if not queue:
            return
        serial = self.art_serial

        def fetch_art():
            seen = {}
            for row, url in queue[:50]:
                if serial != self.art_serial or self.app.closing:
                    return
                if url not in seen:
                    seen[url] = _thumbnail(url)
                if seen[url]:
                    self.app.post("browser_art", (self, serial, row, url, seen[url]))

        threading.Thread(target=fetch_art, daemon=True).start()

    def receive_art(self, serial, row, url, data):
        if serial != self.art_serial or row not in self.rows:
            return
        try:
            image = self.thumbnails.get(url)
            if image is None:
                image = tk.PhotoImage(data=data)
                self.thumbnails[url] = image
                if len(self.thumbnails) > 256:
                    self.thumbnails.popitem(last=False)
            self.tree.item(row, image=image)
        except tk.TclError:
            return

    def selected(self):
        return [self.rows[row] for row in self.tree.selection() if row in self.rows]

    def select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _selection_changed(self):
        state = "normal" if self.selected() and not self.busy else "disabled"
        self.queue_button.configure(state=state)
        self.download_button.configure(state=state)

    def activate(self, event=None):
        if event is not None and event.type == tk.EventType.ButtonPress and not self.tree.identify_row(event.y):
            return
        selected = self.selected()
        if len(selected) == 1 and selected[0]["type"] != "song":
            self.collection = selected[0]
            self.refresh()
        elif len(selected) == 1:
            self.app.preview_music(self, selected[0])

    def add_selection(self, begin=False):
        selected = self.selected()
        links = [item for item in selected if _apple_music_link(item.get("url"))]
        if links:
            count = self.app.queue_music(links, begin=begin)
            if len(links) != len(selected):
                self.status.set("已跳过没有目录链接的项目。")
            elif not begin and count:
                self.status.set(f"已加入队列 {count} 项")
        elif len(selected) == 1 and selected[0]["type"] != "song":
            self.activate()
        elif selected:
            self.status.set("所选曲目没有可下载的 Apple Music 目录版本。")

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row not in self.rows:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        menu = tk.Menu(self.tree, tearoff=False)
        item = self.rows[row]
        if item["type"] != "song":
            menu.add_command(label="查看曲目", command=self.activate)
        elif item.get("preview_url"):
            menu.add_command(label="试听", command=lambda: self.app.preview_music(self, item))
        menu.add_command(label="加入队列", command=self.add_selection)
        menu.add_command(label="下载所选", command=lambda: self.add_selection(begin=True))
        if _apple_music_link(item.get("url")):
            menu.add_command(label="在 Apple Music 打开", command=lambda: self.app.open_music_link(item["url"]))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
