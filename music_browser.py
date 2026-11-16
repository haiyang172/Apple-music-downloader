"""Search and library panes for the amdl desktop window."""

from collections import OrderedDict, defaultdict, deque
import base64
import hashlib
import json
from math import gcd
from pathlib import Path
import re
import sqlite3
import struct
import subprocess
import threading
import tkinter as tk
from tkinter import font, ttk
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from music_catalog import CatalogClient, CatalogError, artwork_url_at_size
from music_library import LibraryClient
from round_theme import music_placeholder
from share_links import links_in_text


FILTERS = {"song": "歌曲", "album": "专辑", "playlist": "歌单"}
PLACEHOLDER = "搜索歌曲、艺人或专辑"
PREFETCH_ROWS = 8
RENDER_BATCH = 100
_COVER_MAX_BYTES = 768 * 1024
_COVER_MAX_PIXELS = 1024 * 1024
_COVER_MAX_SIDE = 384


def _duration(value):
    if not isinstance(value, (int, float)) or not 0 < value < 86400:
        return "—"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}:{seconds:02}"


def _library_cache_scope(token, region, kind, collection):
    identity = ("collection", collection["type"], collection["library_id"]) if collection else ("library", kind)
    key = json.dumps((token, region, identity), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _apple_music_link(value):
    try:
        url = urlsplit(value)
        return url.scheme == "https" and url.netloc.lower() == "music.apple.com"
    except (TypeError, ValueError):
        return False


def _collection_items(collection, rows, total):
    kind = collection["type"]
    library_id = collection.get("library_id")
    if library_id:
        key = f"library:{kind}:{library_id}"
    else:
        key = f"catalog:{collection['storefront']}:{kind}:{collection['id']}"
    return [dict(row, batch_key=key, batch_title=collection["title"], position=position, total=total)
            for position, row in rows]


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


def _png_dimensions(data):
    if len(data) >= 24 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _resize_cover(data, pixels, dimensions):
    if not 0 < len(data) <= _COVER_MAX_BYTES:
        return b""
    if dimensions is not None:
        width, height = dimensions
        if not (width and height and width * height <= _COVER_MAX_PIXELS):
            return b""
        demuxer = "png_pipe"
    elif data.startswith(b"\xff\xd8\xff"):
        demuxer = "jpeg_pipe"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        demuxer = "webp_pipe"
    else:
        return b""

    root = Path(__file__).resolve().parent
    ffmpeg = root / "backend" / "tools" / "ffmpeg.exe"
    if not ffmpeg.is_file():
        ffmpeg = root.parent / "GUI-downloader" / "tools" / "ffmpeg.exe"
    if not ffmpeg.is_file():
        return b""
    filters = rf"crop=min(iw\,ih):min(iw\,ih):exact=1,scale={pixels}:{pixels}:flags=lanczos,setsar=1"
    try:
        result = subprocess.run(
            [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-filter_threads", "1",
             "-threads", "1", "-max_pixels", str(_COVER_MAX_PIXELS), "-protocol_whitelist", "pipe", "-f", demuxer,
             "-i", "pipe:0", "-map", "0:v:0", "-frames:v", "1", "-vf", filters, "-pix_fmt", "rgba",
             "-threads", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True, timeout=6,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return b""
    if len(result) <= _COVER_MAX_BYTES and _png_dimensions(result) == (pixels, pixels):
        return result
    return b""


def _thumbnail(url, pixels):
    if not _apple_art_url(url) or type(pixels) is not int or not 1 <= pixels <= _COVER_MAX_SIDE:
        return b""
    sized = artwork_url_at_size(url, pixels)
    try:
        data = _apple_data(sized or url, _COVER_MAX_BYTES)
    except (OSError, ValueError):
        if not sized or sized == url:
            return b""
        try:
            data = _apple_data(url, _COVER_MAX_BYTES)
        except (OSError, ValueError):
            return b""
    dimensions = _png_dimensions(data)
    if dimensions == (pixels, pixels):
        return base64.b64encode(data)
    converted = _resize_cover(data, pixels, dimensions)
    if converted:
        return base64.b64encode(converted)
    if dimensions and all(1 <= side <= _COVER_MAX_SIDE for side in dimensions):
        return base64.b64encode(data)
    return b""


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


def _sort_value(value):
    if isinstance(value, (int, float)):
        return value
    return tuple(int(part) if index % 2 else part for index, part in
                 enumerate(re.split(r"([0-9]+)", str(value).strip().casefold())))


class TreeSort:
    """Click a list heading to sort loaded rows and click again to reverse."""

    def __init__(self, tree, headings, value, on_choose=None):
        self.tree = tree
        self.headings = dict(headings)
        self.value = value
        self.on_choose = on_choose or self.apply
        self.column = None
        self.descending = False
        self.show_headings()

    def show_headings(self):
        for column, label in self.headings.items():
            suffix = ("  ▼" if self.descending else "  ▲") if column == self.column else ""
            self.tree.heading(column, text=label + suffix, command=lambda key=column: self.choose(key))

    def set_heading(self, column, label):
        self.headings[column] = label
        self.show_headings()

    def choose(self, column):
        self.descending = self.column == column and not self.descending
        self.column = column
        self.show_headings()
        self.on_choose()

    def reset(self):
        self.column = None
        self.descending = False
        self.show_headings()

    def apply(self):
        if self.column is None:
            return
        present, empty = [], []
        for row in self.tree.get_children():
            value = self.value(row, self.column)
            if value is None or value == 0 or (isinstance(value, str) and value.strip() in ("", "—")):
                empty.append(row)
            else:
                present.append((row, _sort_value(value)))
        present.sort(key=lambda item: item[1], reverse=self.descending)
        self.tree.set_children("", *(row for row, _ in present), *empty)


class RepeatedLibraryCursor(RuntimeError):
    """A library refresh needs to restart from its first page."""


class MusicBrowser:
    def __init__(self, app, page, *, private=False):
        self.app = app
        self.private = private
        self.catalog = app.catalog
        self.page = page
        self.kind = "song"
        self.collection = None
        self.parent_view = None
        self.term = ""
        self.loaded = False
        self.busy = False
        self.appending = False
        self.batch_busy = False
        self.serial = 0
        self.batch_serial = 0
        self.cursor = None
        self.request_cursor = None
        self.seen_cursors = set()
        self.page_error = False
        self.library_scope = None
        self.library_snapshot = None
        self.library_checked = set()
        self.library_events = deque()
        self.library_seen = set()
        self.library_cursor = None
        self.view_timer = None
        self.render_timer = None
        self.collection_complete = False
        self.rows = {}
        self.thumbnails = OrderedDict()
        self.art_serial = 0
        self.art_busy = False
        self.art_visible = ()
        self.art_wanted = set()
        self.column_sizes = ()
        self.sized_rows = set()

        page.rowconfigure(1, weight=1)
        bar = ttk.Frame(page, style="Surface.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        app.label(bar, "Apple Music", large=True, background=app.background).grid(row=0, column=0, sticky="w")
        tabs = ttk.Frame(bar, style="Surface.TFrame")
        tabs.grid(row=0, column=1, sticky="e", padx=(round(8 * app.scale), 0))
        self.browse_tab = ttk.Button(tabs, text="浏览", command=lambda: app.show("music"),
                                     style="MusicTab.TButton" if private else "MusicTabActive.TButton")
        self.browse_tab.grid(row=0, column=0)
        self.library_tab = ttk.Button(tabs, text="资料库", command=lambda: app.show("library"),
                                      style="MusicTabActive.TButton" if private else "MusicTab.TButton")
        self.library_tab.grid(row=0, column=1, padx=(round(3 * app.scale), 0))
        self.account_tab = ttk.Button(tabs, text="账户", command=lambda: app.show("account"), style="MusicTab.TButton")
        self.account_tab.grid(row=0, column=2, padx=(round(3 * app.scale), 0))
        self.query = tk.StringVar(page)
        if private:
            actions = ttk.Frame(bar, style="Surface.TFrame")
            actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(7, 0))
            self.connect_button = app.action(actions, "连接资料库", app.connect_library, primary=True)
            self.connect_button.grid(row=0, column=0)
            app.action(actions, "刷新", lambda: self.refresh(force=True)).grid(row=0, column=1, padx=(8, 0))
        else:
            self.entry = ttk.Entry(bar, textvariable=self.query, style="MusicSearch.TEntry",
                                   font=(app.font, 13))
            self.entry.grid(row=1, column=0, sticky="ew", pady=(7, 0))
            self.entry.bind("<Return>", self.search)
            self.entry.bind("<FocusIn>", self._focus)
            self.entry.bind("<FocusOut>", self._blur)
            self._blur()
            app.action(bar, "搜索", self.search, primary=True).grid(row=1, column=1, padx=(8, 0), pady=(7, 0))

        result = app.panel(page, padding=(round(12 * app.scale), round(9 * app.scale)))
        result.grid(row=1, column=0, sticky="nsew", pady=(round(14 * app.scale), 0))
        result.columnconfigure(0, weight=1)
        result.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(result, style="MusicToolbar.TFrame")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=2)
        toolbar.columnconfigure(1, weight=1)
        self.back_button = ttk.Button(toolbar, text="‹ 返回", command=self.back, style="MusicFilter.TButton")
        self.back_button.grid(row=0, column=0, padx=(0, 3))
        self.back_button.grid_remove()
        self.caption = tk.StringVar(page, "资料库" if private else "热门歌曲")
        self.caption_label = ttk.Label(toolbar, textvariable=self.caption, style="MusicTitle.TLabel", width=1)
        self.caption_label.grid(row=0, column=1, sticky="ew", padx=(3, 8))
        self.open_button = app.action(toolbar, "查看曲目", self.open_collection)
        self.open_button.grid(row=0, column=2, padx=(0, 6))
        self.queue_button = app.action(toolbar, "所选入队", self.add_selection)
        self.queue_button.grid(row=0, column=3, padx=(0, 6))
        self.download_button = app.action(toolbar, "下载所选", lambda: self.add_selection(begin=True), primary=True)
        self.download_button.grid(row=0, column=4)

        self.filter_bar = ttk.Frame(result, style="MusicToolbar.TFrame")
        self.filter_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(round(6 * app.scale), round(8 * app.scale)))
        self.filters = {}
        for column, (kind, label) in enumerate(FILTERS.items()):
            button = ttk.Button(self.filter_bar, text=label, command=lambda choice=kind: self.select_kind(choice))
            button.grid(row=0, column=column, padx=(0, round(4 * app.scale)))
            self.filters[kind] = button
        self._update_filters()

        self.tree = ttk.Treeview(result, columns=("artist", "album", "duration"), show="tree headings",
                                 selectmode="extended", style="Music.Treeview")
        self.row_font = font.Font(root=self.tree, family=app.font, size=11)
        self.heading_font = font.Font(root=self.tree, family=app.font, size=10, weight="bold")
        self._set_cover_metrics(app.scale)
        self.tree.heading("#0", text="名称", anchor="w")
        self.tree.heading("artist", text="艺人", anchor="w")
        self.tree.heading("album", text="专辑", anchor="w")
        self.tree.heading("duration", text="时长")
        self.sort = TreeSort(self.tree, {"#0": "名称", "artist": "艺人", "album": "专辑", "duration": "时长"},
                             lambda row, column: self.rows[row].get("title" if column == "#0" else column),
                             self._sort_library if private else None)
        self.tree.column("#0", width=260, minwidth=1, stretch=False)
        self.tree.column("artist", width=140, minwidth=1, stretch=False)
        self.tree.column("album", width=152, minwidth=1, stretch=False)
        self.tree.column("duration", width=self.duration_width, minwidth=1, stretch=False, anchor="center")
        self.tree.grid(row=2, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(result, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        scrollbar.grid(row=2, column=1, sticky="ns", padx=(round(5 * app.scale), 0))
        self.tree.configure(yscrollcommand=lambda first, last: self._scrolled(scrollbar, first, last))
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        self.tree.bind("<Double-1>", self.activate)
        self.tree.bind("<Return>", self.activate)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Button-3>", self.context_menu)
        self.tree.bind("<Configure>", self.reflow)
        self.tree.bind("<ButtonPress-1>", self._guard_column_separator, add="+")
        self.tree.bind("<ButtonRelease-1>", lambda _: self._schedule_view(), add="+")

        foot = ttk.Frame(result, style="MusicToolbar.TFrame")
        foot.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(round(6 * app.scale), 0))
        self.status = tk.StringVar(page)
        ttk.Label(foot, textvariable=self.status, style="MusicStatus.TLabel", width=1).pack(
            side="left", fill="x", expand=True, padx=(3, 8))
        self.retry_page = app.action(foot, "重试加载", self._retry_loading)
        self.whole_button = app.action(foot, "全部加入队列", self.add_collection)
        self.whole_download_button = app.action(foot, "下载全部", lambda: self.add_collection(begin=True), primary=True)
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
        else:
            self._schedule_view()

    def _scrolled(self, scrollbar, first, last):
        scrollbar.set(first, last)
        self._schedule_view()

    def _set_cover_metrics(self, scale):
        self.cover_scale = scale
        self.cover_size = round(52 * scale)
        self.row_images = {kind: music_placeholder(self.tree, self.cover_size, kind) for kind in FILTERS}
        self.cell_padding = max(14, round(12 * scale))
        self.tree_inset = max(4, round(4 * scale))
        self.duration_width = max(round(65 * scale), self.row_font.measure("1439:59") + self.cell_padding,
                                  self.heading_font.measure("时长  ▼") + self.cell_padding)

    def set_cover_scale(self, scale):
        if scale == self.cover_scale or self.app.closing:
            return
        first = self.tree.yview()[0]
        self.art_serial += 1
        self.art_visible = ()
        self.art_wanted.clear()
        self.thumbnails.clear()
        self.sized_rows.clear()
        self._set_cover_metrics(scale)
        style = ttk.Style(self.tree)
        name = self.tree.cget("style")
        height = round(64 * scale)
        if str(style.lookup(name, "rowheight")) != str(height):
            style.configure(name, rowheight=height)
        self.tree.yview_moveto(first)
        self.reflow()

    def _schedule_view(self):
        if self.view_timer is None and not self.app.closing:
            self.view_timer = self.tree.after(40, self._check_view)

    def stop_view_updates(self):
        for name in ("view_timer", "render_timer"):
            timer = getattr(self, name)
            if timer is not None:
                self.tree.after_cancel(timer)
                setattr(self, name, None)

    def _check_view(self):
        self.view_timer = None
        if self.app.closing:
            return
        active = "library" if self.private else "music"
        visible_page = self.app.active_page == active and self.tree.winfo_viewable()
        if visible_page and not self.private:
            _, bottom = self.tree.yview()
            if (self.cursor and not self.busy and not self.page_error
                    and len(self.rows) * (1 - bottom) <= PREFETCH_ROWS):
                self.refresh(append=True)
        if not visible_page:
            self.art_visible = ()
            self.art_wanted = set()
            return
        visible = self._visible_rows()
        self._size_visible_rows(visible)
        self._load_art(visible)

    def _sort_library(self):
        self._apply_library_sort()

    def _retry_loading(self):
        self.refresh(append=bool(self.cursor), force=self.private)

    def _apply_library_sort(self):
        self.sort.apply()
        self.tree.yview_moveto(0)
        self._schedule_view()

    def _cancel_render(self):
        if self.render_timer is not None:
            self.tree.after_cancel(self.render_timer)
            self.render_timer = None

    def _clear_rows(self):
        self._cancel_render()
        self.art_serial += 1
        self.rows.clear()
        self.sized_rows.clear()
        self.tree.delete(*self.tree.get_children())

    def _configure_list(self):
        if self.collection:
            self.caption.set(self.collection["title"])
            self.back_button.configure(text=f"‹ {FILTERS[self.kind]}" if self.private else "‹ 返回")
            self.back_button.grid()
        else:
            label = FILTERS[self.kind]
            self.caption.set(label if self.private else f"“{self.term}” · {label}" if self.term else f"热门{label}")
            self.back_button.grid_remove()
        if self.private and self.collection:
            self.filter_bar.grid_remove()
        else:
            self.filter_bar.grid()
        tracks = bool(self.collection) or self.kind == "song"
        self.tree.configure(displaycolumns=("artist", "album", "duration") if tracks else ("artist",))
        self._layout_columns()
        self.sort.set_heading("artist", "创建者" if not tracks and self.kind == "playlist" else "艺人")

    def search(self, _event=None):
        term = self.query.get().strip()
        if "://" in term and (term.startswith("https://") or links_in_text(term, self.app.link_source)):
            self.app.add_link_text(term, begin=False)
            return
        self.term = term if term != PLACEHOLDER else ""
        self.collection = None
        self.refresh()

    def select_kind(self, kind):
        self.kind = kind
        self.collection = None
        self.parent_view = None
        self._update_filters()
        self.refresh()

    def _update_filters(self):
        for kind, button in self.filters.items():
            button.configure(style="MusicFilterActive.TButton" if kind == self.kind else "MusicFilter.TButton")

    def back(self):
        self.collection = None
        self.refresh()

    def invalidate(self):
        self.serial += 1
        self._clear_rows()
        self.batch_serial += 1
        self.batch_busy = False
        self.busy = self.loaded = self.appending = False
        self.collection = None
        self.parent_view = None
        self.collection_complete = False
        self.cursor = None
        self.request_cursor = None
        self.seen_cursors.clear()
        self.page_error = False
        self.library_scope = self.library_snapshot = None
        self.library_events.clear()
        self.library_checked.clear()
        self.library_seen.clear()
        self.library_cursor = None
        if self.private:
            self.sort.reset()
        self.retry_page.pack_forget()
        self.back_button.grid_remove()
        self.status.set("")
        self._selection_changed()

    def refresh(self, append=False, *, force=False):
        if self.private:
            self._refresh_library(force)
            return
        if append and (self.busy or not self.cursor):
            return
        self.loaded = True
        self.busy = True
        self.appending = append
        self.serial += 1
        serial = self.serial
        collection = self.collection
        cursor = self.cursor if append else None
        self.request_cursor = cursor
        self.page_error = False
        self.retry_page.pack_forget()
        if not append:
            self._clear_rows()
            self.seen_cursors.clear()
            self.collection_complete = False
            self._configure_list()
        self.cursor = None
        self._selection_changed()
        storefront = self.app.catalog_region()
        term = self.term
        kind = self.kind
        starting_count = len(self.rows)
        self.status.set(f"{starting_count} 项 · 正在预取下一页" if append else "正在连接 Apple Music…")

        def fetch():
            try:
                if collection:
                    rows, next_cursor = self.catalog.tracks_page(collection, collection.get("storefront") or storefront, cursor)
                elif term:
                    rows, next_cursor = self.catalog.search_page(term, storefront, kind, cursor)
                else:
                    rows, next_cursor = self.catalog.charts_page(storefront, kind, cursor)
                self.app.post("browser_result", (self, serial, rows, next_cursor, ""))
            except (OSError, RuntimeError, ValueError) as error:
                self.app.post("browser_result", (self, serial, None, cursor, str(error)))
            except Exception:
                self.app.post("browser_result", (self, serial, None, cursor, "列表读取失败，请重试。"))

        threading.Thread(target=fetch, daemon=True).start()

    def _refresh_library(self, force):
        collection = self.collection
        region = self.app.catalog_region()
        kind = self.kind
        token = ""
        error = ""
        try:
            token = self.app.library_token()
        except ValueError as problem:
            error = str(problem)
        scope = _library_cache_scope(token, region, kind, collection) if token else None
        same = bool(scope and scope == self.library_scope)
        if same and self.busy:
            return

        self.loaded = True
        self.serial += 1
        serial = self.serial
        self.library_events.clear()
        self.library_scope = scope
        self.page_error = False
        self.retry_page.pack_forget()
        if not same:
            self._clear_rows()
            self.library_snapshot = None
            self.collection_complete = False
            self.library_seen.clear()
            self.library_cursor = None
            self.sort.reset()
        if same and self.library_snapshot is None and self.rows and not self.library_cursor:
            self._clear_rows()
            self.library_seen.clear()
        self._configure_list()
        if not scope:
            self._finish_page(None, error or "个人资料库需要 Apple Music 网页授权。")
            return

        self.busy = True
        self.appending = bool(self.rows) or self.library_snapshot is not None
        self._selection_changed()
        self.status.set(f"{len(self.rows)} 项 · 正在同步" if self.appending else "正在读取资料库…")
        snapshot = self.library_snapshot
        partial = list(self.rows.values()) if snapshot is None and self.library_cursor else []
        checked = not force and scope in self.library_checked
        threading.Thread(target=self._load_library, args=(serial, scope, token, region, kind, collection,
                                                           snapshot, partial, self.library_cursor,
                                                           set(self.library_seen), checked), daemon=True).start()

    def _load_library(self, serial, scope, token, region, kind, collection, snapshot, partial, cursor, seen, checked):
        def current():
            return serial == self.serial and not self.app.closing

        def post(event, value=None):
            if current():
                self.app.post("browser_library", (self, serial, event, value))

        if snapshot is None and not partial:
            try:
                snapshot = self.app.library_cache.load(scope)
            except (OSError, sqlite3.Error, ValueError):
                pass
            if snapshot is not None:
                post("cache", snapshot)
        if checked and snapshot is not None:
            post("done", snapshot)
            return

        rows = [dict(item) for item in partial]
        if collection:
            for item in rows:
                item.pop("position", None)
        try:
            client = LibraryClient(token, region, catalog=self.catalog)
            for page, cursor in self._library_pages(client, kind, collection, lambda: not current(), cursor, seen):
                rows.extend(page)
                if snapshot is None:
                    post("page", (page, cursor, set(seen)))
        except RepeatedLibraryCursor as error:
            post("restart", str(error))
            return
        except (OSError, RuntimeError, ValueError) as error:
            post("error", str(error))
            return
        except Exception:
            post("error", "列表读取失败，请重试。")
            return
        if not current():
            return
        try:
            self.app.library_cache.save(scope, rows)
        except (OSError, sqlite3.Error, ValueError):
            pass
        post("done", rows)

    def receive_library(self, serial, event, value):
        if serial != self.serial or self.app.closing:
            return
        self.library_events.append((event, value))
        self._drain_library_events(serial)

    def _drain_library_events(self, serial):
        while self.library_events and self.render_timer is None:
            event, value = self.library_events.popleft()
            if event in ("cache", "page"):
                if event == "cache":
                    self.library_snapshot = value
                    self.collection_complete = bool(self.collection)
                else:
                    value, self.library_cursor, self.library_seen = value
                self._render_rows(serial, value, 0, done=lambda: self._library_rows_ready(serial))
                return
            if event in ("error", "restart"):
                if event == "restart":
                    self.library_cursor = None
                    self.library_seen.clear()
                count = f"{len(self.rows)} 项 · " if self.rows or self.library_snapshot is not None else ""
                self._finish_page(None, f"{count}同步失败：{value}")
                return
            self._update_library_rows(value)
            self.library_snapshot = value
            self.library_checked.add(self.library_scope)
            self.library_cursor = None
            self.library_seen.clear()
            self._finish_page(None, "")

    def _library_rows_ready(self, serial):
        self.appending = True
        if self.library_snapshot is not None and self.sort.column is not None:
            self._sort_keep_view()
        self._restore_parent_view()
        self._selection_changed()
        self._schedule_view()
        self.status.set(f"{len(self.rows)} 项 · 正在同步")
        if self.library_events:
            self.render_timer = self.tree.after(1, self._continue_library_events, serial)

    def _continue_library_events(self, serial):
        self.render_timer = None
        if serial == self.serial and not self.app.closing:
            self._drain_library_events(serial)

    def _update_library_rows(self, snapshot):
        if self.library_snapshot == snapshot:
            return
        displayed = list(self.rows.values())
        desired = [dict(item, position=index) if self.collection else item for index, item in enumerate(snapshot, 1)]
        if displayed == desired:
            return

        def identity(row):
            return row["type"], row.get("library_id") or row.get("id") or row.get("url") or row["title"]

        previous = defaultdict(deque)
        for row_id, item in self.rows.items():
            previous[identity(item)].append(row_id)
        updated = {}
        order = self.tree.get_children()
        first, _ = self.tree.yview()
        anchor = order[min(len(order) - 1, round(first * len(order)))] if order else None
        for item in desired:
            matches = previous[identity(item)]
            if matches:
                row_id = matches.popleft()
                if self.rows[row_id] != item:
                    self.tree.item(row_id, **self._row_options(item))
                    self.sized_rows.discard(row_id)
            else:
                row_id = self.tree.insert("", "end", **self._row_options(item))
            updated[row_id] = item
        removed = set(self.rows) - updated.keys()
        self.tree.delete(*removed)
        self.sized_rows.difference_update(removed)
        self.rows = updated
        self.tree.set_children("", *updated)
        if anchor in updated:
            self.tree.yview(self.tree.index(anchor))

    def _sort_keep_view(self):
        order = self.tree.get_children()
        first, _ = self.tree.yview()
        anchor = order[min(len(order) - 1, round(first * len(order)))] if order else None
        self.sort.apply()
        if anchor is not None:
            self.tree.yview(self.tree.index(anchor))

    def receive(self, serial, rows, cursor, error):
        if serial != self.serial or self.app.closing:
            return
        if rows is None:
            self._finish_page(cursor, error)
            return
        if self.request_cursor:
            self.seen_cursors.add(self.request_cursor)
        if cursor in self.seen_cursors:
            cursor, error = None, "平台返回了重复分页。"
        self.collection_complete = False
        self._render_rows(serial, rows, 0, cursor, error)

    def _row_options(self, item):
        return {
            "text": item["title"],
            "values": (item.get("artist") or "—", item.get("album") or "—", _duration(item.get("duration"))),
            "image": self.row_images.get(item.get("type"), self.row_images["song"]),
        }

    def _render_rows(self, serial, rows, offset, cursor=None, error="", *, done=None):
        self.render_timer = None
        if serial != self.serial or self.app.closing:
            return
        end = offset + RENDER_BATCH
        for item in rows[offset:end]:
            index = len(self.rows) + 1
            if self.collection:
                item = dict(item, position=index)
            item_id = self.tree.insert("", "end", **self._row_options(item))
            self.rows[item_id] = item
        if end < len(rows):
            self.render_timer = self.tree.after(1, lambda: self._render_rows(serial, rows, end, cursor, error, done=done))
        elif done is not None:
            done()
        else:
            self._finish_page(cursor, error)

    def _finish_page(self, cursor, error):
        self.busy = self.appending = False
        self.cursor = cursor
        complete = self.library_snapshot is not None if self.private else not cursor and not error
        self.collection_complete = bool(self.collection and complete)
        if error:
            self.status.set(error)
            self.page_error = True
            self.retry_page.pack(side="right")
        elif self.sort.column is not None:
            self._sort_keep_view()
        self._restore_parent_view()
        if not error:
            self.status.set(f"{len(self.rows)} 项" if self.rows else "没有结果")
        self._selection_changed()
        self._schedule_view()

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
        artist = self.clipped(item.get("artist") or "—", self.tree.column("artist", "width") - self.cell_padding)
        album = self.clipped(item.get("album") or "—", self.tree.column("album", "width") - self.cell_padding)
        padding = self.cell_padding + self.cover_size + round(8 * self.cover_scale)
        title = self.clipped(item["title"], self.tree.column("#0", "width") - padding)
        return title, (artist, album, _duration(item.get("duration")))

    def reflow(self, _event=None):
        self._layout_columns()
        self._schedule_view()

    def _guard_column_separator(self, event):
        if self.tree.identify_region(event.x, event.y) == "separator":
            return "break"

    def _layout_columns(self):
        available = self.tree.winfo_width() - self.tree_inset
        if available < 8:
            return
        if "duration" in self.tree.cget("displaycolumns"):
            duration = min(self.duration_width, available - 3)
            text_width = available - duration
            artist = max(1, round(text_width * 0.23))
            album = max(1, round(text_width * 0.29))
            widths = {"#0": text_width - artist - album, "artist": artist, "album": album, "duration": duration}
        else:
            artist = round(available * 0.32)
            widths = {"#0": available - artist, "artist": artist}
        for column, width in widths.items():
            if self.tree.column(column, "width") != width:
                self.tree.column(column, width=width)

    def _update_column_sizes(self):
        sizes = tuple(self.tree.column(name, "width") for name in ("#0", "artist", "album"))
        if sizes != self.column_sizes:
            self.column_sizes = sizes
            self.sized_rows.clear()

    def _size_visible_rows(self, visible):
        self._update_column_sizes()
        for row in visible:
            if row in self.sized_rows:
                continue
            text, values = self.cells(self.rows[row])
            self.tree.item(row, text=text, values=values)
            self.sized_rows.add(row)

    def _visible_rows(self):
        # The row style is at least 24 px tall. Sampling every 12 px finds
        # even partially visible rows without walking the entire tree.
        height = self.tree.winfo_height()
        x = min(self.tree.winfo_width() // 2, self.tree.column("#0", "width") // 2)
        visible = {}
        for y in (*range(0, height, 12), height - 2):
            if self.tree.identify_region(x, y) not in ("tree", "cell"):
                continue
            row = self.tree.identify_row(y)
            if row in self.rows:
                visible[row] = None
        return tuple(visible)

    def _load_art(self, visible):
        self.art_visible = visible
        pending = {}
        for row in visible:
            item = self.rows[row]
            image = self.row_images.get(item.get("type"), self.row_images["song"])
            url = item.get("artwork_url")
            if url:
                if url in self.thumbnails:
                    self.thumbnails.move_to_end(url)
                    image = self.thumbnails[url] or image
                else:
                    pending[url] = None
            if str(image) not in self.tree.item(row, "image"):
                self.tree.item(row, image=image)
        self.art_wanted = set(pending)
        if self.art_busy or not pending:
            return
        serial = self.art_serial
        pixels = self.cover_size
        self.art_busy = True

        def fetch_art():
            try:
                for url in pending:
                    if serial != self.art_serial or self.app.closing:
                        break
                    if url in self.art_wanted:
                        self.app.post("browser_art", (self, serial, url, _thumbnail(url, pixels)))
            finally:
                self.app.post("browser_art", (self, serial, None, None))

        threading.Thread(target=fetch_art, daemon=True).start()

    def receive_art(self, serial, url, data):
        if url is None:
            self.art_busy = False
            self._schedule_view()
            return
        if serial != self.art_serial:
            return
        try:
            image = self._cover_image(data) if data else None
        except tk.TclError:
            image = None
        self.thumbnails[url] = image
        if len(self.thumbnails) > 256:
            self.thumbnails.popitem(last=False)
        if image is not None:
            for row in self.art_visible:
                if self.rows.get(row, {}).get("artwork_url") == url:
                    self.tree.item(row, image=image)

    def _cover_image(self, data):
        source = tk.PhotoImage(master=self.tree, data=data)
        size = self.cover_size
        side = min(source.width(), source.height())
        x, y = (source.width() - side) // 2, (source.height() - side) // 2
        if size != side:
            # Match the placeholder width. Tk can resample and crop directly into the tile.
            divisor = gcd(size, side)
            numerator, denominator = size // divisor, side // divisor
        else:
            numerator = denominator = 1
        image = tk.PhotoImage(master=self.tree, width=size, height=size)
        image.tk.call(str(image), "copy", str(source), "-from", x, y, x + side, y + side,
                      "-zoom", numerator, numerator, "-subsample", denominator, denominator)
        radius = max(2, round(6 * self.cover_scale))
        for x in range(radius):
            for y in range(radius):
                if (radius - x - 0.5) ** 2 + (radius - y - 0.5) ** 2 > radius ** 2:
                    for corner_x in (x, size - x - 1):
                        for corner_y in (y, size - y - 1):
                            image.transparency_set(corner_x, corner_y, True)
        return image

    def selected(self):
        return [self.rows[row] for row in self.tree.selection() if row in self.rows]

    def select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _selection_changed(self):
        available = (not self.busy or self.appending) and not self.batch_busy
        selected = self.selected()
        state = "normal" if selected and available else "disabled"
        self.queue_button.configure(state=state)
        self.download_button.configure(state=state)
        if not self.collection and self.kind != "song":
            can_open = len(selected) == 1 and selected[0]["type"] in ("album", "playlist")
            self.open_button.configure(state="normal" if available and can_open else "disabled")
            self.open_button.grid()
        else:
            self.open_button.grid_remove()
        if self.collection:
            whole_available = available and (not self.private or self.collection_complete or not self.busy)
            whole_state = "normal" if whole_available else "disabled"
            self.whole_button.configure(state=whole_state)
            self.whole_download_button.configure(state=whole_state)
            if not self.whole_button.winfo_manager():
                self.whole_download_button.pack(side="right", padx=(7, 0))
                self.whole_button.pack(side="right", padx=(7, 0))
        else:
            self.whole_download_button.pack_forget()
            self.whole_button.pack_forget()

    def open_collection(self, item=None):
        if item is None:
            selected = self.selected()
            if len(selected) != 1:
                return
            item = selected[0]
        if self.collection or item["type"] not in ("album", "playlist"):
            return
        if self.private:
            self.parent_view = item["library_id"], self.sort.column, self.sort.descending
        self.collection = item
        self.refresh()

    def _restore_parent_view(self):
        if self.collection or not self.parent_view:
            return
        identity, column, descending = self.parent_view
        row = next((row for row, item in self.rows.items() if item.get("library_id") == identity), None)
        if row:
            self.parent_view = None
            self.sort.column, self.sort.descending = column, descending
            self.sort.show_headings()
            self.sort.apply()
            self.tree.selection_set(row)
            self.tree.focus(row)
            self.tree.see(row)

    def activate(self, event=None):
        if event is not None and event.type == tk.EventType.ButtonPress and not self.tree.identify_row(event.y):
            return
        selected = self.selected()
        if len(selected) == 1 and selected[0]["type"] != "song":
            self.open_collection(selected[0])
        elif len(selected) == 1:
            self.app.preview_music(self, selected[0])

    def add_selection(self, begin=False):
        selected = self.selected()
        if not selected or (self.busy and not self.appending) or self.batch_busy or self.app.closing:
            return
        if self.collection:
            if self.private and self.library_snapshot is not None:
                total = len(self.library_snapshot)
            else:
                total = len(self.rows) if self.collection_complete else None
            rows = sorted((item["position"], item) for item in selected)
            self._enqueue(_collection_items(self.collection, rows, total), begin)
        elif any(item["type"] in ("album", "playlist") for item in selected):
            self._resolve_batch(selected, begin)
        else:
            self._enqueue(selected, begin)

    def add_collection(self, begin=False):
        if self.collection and (not self.busy or self.appending) and not (self.batch_busy or self.app.closing):
            if self.collection_complete:
                rows = self.library_snapshot if self.private else list(self.rows.values())
                if not rows:
                    self.status.set("这个列表没有曲目。")
                    return
                self._enqueue(_collection_items(self.collection, enumerate(rows, 1), len(rows)), begin)
                return
            if not self.private or not self.busy:
                self._resolve_batch([self.collection], begin)

    def _resolve_batch(self, selected, begin):
        try:
            token = self.app.library_token() if self.private else None
            storefront = self.app.catalog_region()
            library = LibraryClient(token, storefront, catalog=self.catalog) if self.private else None
        except (RuntimeError, ValueError) as error:
            self.status.set(str(error))
            return
        self.batch_serial += 1
        serial = self.batch_serial
        self.batch_busy = True
        self._selection_changed()
        self.status.set("正在读取完整曲目列表…")
        checked = frozenset(self.library_checked)

        def cancelled():
            return self.app.closing or serial != self.batch_serial

        def resolve():
            items, failures = [], []
            cached_count = 0
            for item in selected:
                if cancelled():
                    return
                if item["type"] not in ("album", "playlist"):
                    items.append(item)
                    continue
                try:
                    if library is None:
                        rows = self.catalog.tracks(item, item.get("storefront") or storefront)
                    else:
                        scope = _library_cache_scope(token, storefront, item["type"], item)
                        rows, offline = self._library_collection(library, item, scope, scope in checked, cancelled)
                        cached_count += int(offline)
                    if not rows:
                        failures.append(f"{item['title']}没有曲目。")
                    items.extend(_collection_items(item, enumerate(rows, 1), len(rows)))
                except (OSError, RuntimeError, ValueError) as error:
                    failures.append(f"{item['title']} · {error}")
            if not cancelled():
                error = failures[0] if failures else ""
                if len(failures) > 1:
                    error += f"（共 {len(failures)} 个列表无法读取）"
                if cached_count:
                    error = " · ".join(filter(None, (error, f"{cached_count} 个列表使用上次同步的曲目")))
                skipped = sum(not _apple_music_link(item.get("url")) for item in items)
                self.app.post("browser_batch", (self, serial, items, skipped, error, begin))

        threading.Thread(target=resolve, daemon=True).start()

    def _library_collection(self, library, collection, scope, checked, cancelled):
        try:
            snapshot = self.app.library_cache.load(scope)
        except (OSError, sqlite3.Error, ValueError):
            snapshot = None
        if checked and snapshot is not None:
            return snapshot, False
        try:
            rows = [row for page, _ in self._library_pages(library, collection["type"], collection,
                                                           cancelled, None, set()) for row in page]
        except (OSError, CatalogError) as error:
            status = error.status if isinstance(error, CatalogError) else None
            if snapshot is None or (status is not None and status != 429 and status < 500):
                raise
            return snapshot, True
        if not cancelled():
            try:
                self.app.library_cache.save(scope, rows)
            except (OSError, sqlite3.Error, ValueError):
                pass
        return rows, False

    @staticmethod
    def _library_pages(library, kind, collection, cancelled, cursor, seen):
        while not cancelled():
            if cursor:
                seen.add(cursor)
            if collection:
                page, next_cursor = library.tracks(collection, cursor=cursor)
            else:
                page, next_cursor = library.items(kind, cursor=cursor)
            if next_cursor in seen:
                raise RepeatedLibraryCursor("资料库返回了重复的分页地址。")
            yield page, next_cursor
            if not next_cursor:
                return
            cursor = next_cursor

    def receive_batch(self, serial, items, skipped, error, begin):
        if serial != self.batch_serial or self.app.closing:
            return
        self.batch_busy = False
        self._selection_changed()
        self._enqueue(items, begin, skipped=skipped, error=error)

    def _enqueue(self, items, begin, *, skipped=None, error=""):
        playable = [item for item in items if _apple_music_link(item.get("url"))]
        if skipped is None:
            skipped = len(items) - len(playable)
        count = self.app.queue_music(playable, begin=begin) if playable else 0
        messages = []
        if count:
            messages.append(f"已确认 {count} 项在队列中")
        if skipped:
            messages.append(f"已跳过 {skipped} 首没有可下载目录版本的曲目")
        if error:
            messages.append(error)
        if messages:
            notice = " · ".join(messages)
            self.status.set(notice)
            if begin and count and (skipped or error):
                self.app.post("log", notice)

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row not in self.rows:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        menu = tk.Menu(self.tree, tearoff=False)
        item = self.rows[row]
        if item["type"] != "song":
            menu.add_command(label="查看曲目", command=lambda: self.open_collection(item))
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
