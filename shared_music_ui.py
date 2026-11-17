"""Search songs, browse shared links, and queue tracks."""

from collections import defaultdict, deque
import csv
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, font, ttk
from urllib.parse import urlsplit
import webbrowser

import netease_catalog
import qq_catalog
import shared_music
from music_browser import TreeSort, _duration
from qr_account_ui import NeteaseAccountPanel, QqAccountPanel
from share_links import links_in_text
from short_music_links import expand_short_music_link, short_music_source


SOURCES = {"qq": "QQ 音乐", "netease": "网易云音乐"}
OFFICIAL_HOSTS = {"qq": {"y.qq.com"}, "netease": {"music.163.com"}}
CATALOGS = {"qq": qq_catalog, "netease": netease_catalog}
ACCOUNT_PANELS = {"qq": QqAccountPanel, "netease": NeteaseAccountPanel}


def public_music_source(value):
    return shared_music.shared_music_source(value) or short_music_source(value)


def resolve_public_music_link(value, *, session=None, stop=None):
    link = expand_short_music_link(value, stop=stop)
    if shared_music.shared_music_source(link) == "qq":
        return qq_catalog.resolve_public_qq(link, session=session, stop=stop)
    return netease_catalog.resolve_public_netease(link, session=session, stop=stop)


def _official_url(value, source):
    if not isinstance(value, str) or any(ord(character) <= 32 or character == "\\" for character in value):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme == "https" and parsed.netloc in OFFICIAL_HOSTS.get(source, ()):
        return value
    return ""


def _csv_cell(value):
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


class SharedMusicBrowser:
    def __init__(self, app, page, *, source=None):
        if source is not None and source not in SOURCES:
            raise ValueError(f"Unknown shared music source: {source}")
        self.app = app
        self.page_source = source
        self.catalog = CATALOGS.get(source)
        self.serial = 0
        self.busy = False
        self.appending = False
        self.stop = threading.Event()
        self.rows = {}
        self.source = ""
        self.collection_url = ""
        self.total = None
        self.view = "tracks"
        self.private_view = False
        self.private_serial = 0
        self.private_stop = threading.Event()
        self.private_cache_access = threading.Lock()
        self.private_refreshing = False
        self.private_playlist = None
        self.private_complete = False
        self.private_position = None
        self.batch_serial = 0
        self.batch_stop = threading.Event()
        self.batch_busy = False
        self.batch_session = None
        self.batch_owner = None
        self.library = None
        self.library_position = None
        self.search_query = ""
        self.next_search_offset = None
        self.search_warning = ""
        self.search_urls = set()
        self.link_hint = "搜索歌曲，或粘贴歌曲、歌单链接" if source else ""
        self.link = tk.StringVar(page, self.link_hint)
        self.caption = tk.StringVar(page, "曲目")
        self.status = tk.StringVar(page)
        self.count = tk.StringVar(page)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        bar = ttk.Frame(page, style="Surface.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        app.label(bar, SOURCES.get(source, "公开音乐链接"), large=True, background=app.background).grid(
            row=0, column=0, sticky="w", pady=(0, 7))
        if self.catalog is not None:
            self.account = ACCOUNT_PANELS[source](app, bar, app.music_auth[source], changed=self._account_changed)
            self.account.button.grid(row=0, column=1, sticky="e", pady=(0, 7))
        if source is None:
            app.label(bar, "QQ 音乐或网易云音乐的歌曲、歌单链接", muted=True, background=app.background).grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(4, 3))
        self.entry = ttk.Entry(bar, textvariable=self.link, style="MusicSearch.TEntry", font=(app.font, 13))
        self.entry.grid(row=2, column=0, sticky="ew")
        self.entry.bind("<Return>", self.resolve)
        if source is not None:
            self.entry.bind("<FocusIn>", self._clear_link_hint)
            self.entry.bind("<FocusOut>", self._restore_link_hint)
        self.resolve_button = app.action(bar, "查找" if source else "读取曲目", self.resolve, primary=True)
        self.resolve_button.grid(row=2, column=1, padx=(8, 0))
        if self.catalog is not None:
            self.library_button = app.action(bar, "我的歌单", self.read_library)
            self.library_button.grid(row=2, column=2, padx=(6, 0))

        card = app.panel(page, padding=(round(12 * app.scale), round(9 * app.scale)))
        card.grid(row=1, column=0, sticky="nsew", pady=(round(14 * app.scale), 0))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(card, style="MusicToolbar.TFrame")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        toolbar.columnconfigure(0, weight=1)
        ttk.Label(toolbar, textvariable=self.caption, style="MusicTitle.TLabel", width=1).grid(
            row=0, column=0, sticky="ew", padx=(3, 8)
        )
        self.export_button = app.action(toolbar, "导出 CSV", self.export_csv)
        self.export_button.grid(row=0, column=1, padx=(0, 6))
        self.open_button = app.action(toolbar, "打开原页面", self.open_original)
        self.open_button.grid(row=0, column=2)
        if self.catalog is not None:
            self.open_playlist_button = app.action(toolbar, "打开歌单", self._open_selected, primary=True)
            self.open_playlist_button.grid(row=0, column=3, padx=(6, 0))
            self.open_playlist_button.grid_remove()
        if source in ("qq", "netease"):
            downloads = ttk.Frame(card, style="MusicToolbar.TFrame")
            downloads.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(7, 0))
            self.download_button = app.action(downloads, "下载所选", lambda: self._enqueue(True), primary=True)
            self.download_button.pack(side="right")
            self.queue_all_button = app.action(downloads, "全部入队", lambda: self._enqueue(False, all_rows=True))
            self.queue_all_button.pack(side="right", padx=(0, 6))
            self.queue_button = app.action(downloads, "所选入队", lambda: self._enqueue(False))
            self.queue_button.pack(side="right", padx=(0, 6))
            if source == "netease":
                ttk.Label(downloads, text="原始 MP3", style="MusicStatus.TLabel", width=1).pack(
                    side="left", fill="x", expand=True, padx=(3, 6))

        headings = {"position": "序号", "title": "歌曲", "artist": "艺人", "duration": "时长"}
        self.tree = ttk.Treeview(card, columns=tuple(headings), show="headings",
                                 selectmode="extended",
                                 style="Queue.Treeview")
        self.tree.grid(row=1, column=0, sticky="nsew")
        heading_font = font.Font(root=self.tree, family=app.font, size=10, weight="bold")
        for column, preferred_width in (("position", 66), ("title", 240), ("artist", 135), ("duration", 72)):
            fixed = column in ("position", "duration")
            width = max(round(preferred_width * app.scale), heading_font.measure(headings[column] + "  ▼")
                        + round(16 * app.scale))
            self.tree.column(column, width=width, minwidth=width if fixed else 1,
                             stretch=False, anchor="center" if fixed else "w")
            self.tree.heading(column, anchor="center" if fixed else "w")
        self.sort = TreeSort(self.tree, headings, lambda row, column: self.rows[row].get(column))
        self.tree.bind("<<TreeviewSelect>>", self._update_actions)
        self.tree.bind("<Configure>", self._fit_columns, add="+")
        self.tree.bind("<Return>", self._open_selected)
        self.tree.bind("<Double-1>", self._open_clicked)
        self.scrollbar = ttk.Scrollbar(card, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        self.scrollbar.grid(row=1, column=1, sticky="ns", padx=(round(5 * app.scale), 0))
        self.tree.configure(yscrollcommand=self._scroll_changed)

        foot = ttk.Frame(card, style="MusicToolbar.TFrame")
        foot.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        ttk.Label(foot, textvariable=self.status, style="MusicStatus.TLabel", width=1).pack(
            side="left", fill="x", expand=True, padx=(3, 8)
        )
        ttk.Label(foot, textvariable=self.count, style="MusicStatus.TLabel").pack(side="right", padx=(0, 3))
        self.retry_button = app.action(foot, "重试", self._load_next_search_page)
        self._update_actions()

    def _fit_columns(self, event):
        fixed = sum(self.tree.column(key, "width") for key in ("position", "duration"))
        available = max(2, event.width - fixed - round(4 * self.app.scale))
        artist = max(1, min(round(170 * self.app.scale), available // 3))
        self.tree.column("artist", width=artist)
        self.tree.column("title", width=available - artist)

    def _clear_link_hint(self, _event):
        if self.link.get() == self.link_hint:
            self.link.set("")

    def _restore_link_hint(self, _event):
        if not self.link.get().strip():
            self.link.set(self.link_hint)

    def resolve(self, _event=None):
        if self._opening or self.app.closing:
            return "break"
        value = self.link.get().strip()
        if not value or value == self.link_hint:
            self.status.set("请输入歌名或链接。" if self.page_source else "请先粘贴音乐链接。")
            self.entry.focus_set()
            return "break"
        links = links_in_text(value, lambda link: public_music_source(link) in (SOURCES if self.page_source is None
                                                                                else (self.page_source,)))
        link_source = public_music_source(links[0]) if len(links) == 1 else None
        if self.catalog is not None and not link_source and "://" not in value and not value.startswith("www."):
            self._search(value)
            return "break"
        if not link_source:
            name = SOURCES.get(self.page_source, "音乐平台")
            self.status.set(f"请一次粘贴一个{name}歌曲或歌单链接。")
            return "break"
        session = self.account.session if self.catalog is not None else None
        self._read(lambda stop: resolve_public_music_link(links[0], session=session, stop=stop), "正在读取公开曲目…")
        return "break"

    @property
    def _opening(self):
        return self.busy and not self.appending

    def _search(self, query, offset=0):
        session = self.account.session

        def fetch(stop):
            result = self.catalog.search_songs(query, session=session, stop=stop, offset=offset)
            return dict(result, view="search", query=query, offset=offset)

        self._read(fetch, "正在加载更多歌曲…" if offset else "正在搜索歌曲…", append=bool(offset))

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if (float(last) >= 0.95 and not self.search_warning and self.tree.winfo_viewable()
                and self.app.active_page == self.page_source):
            self._load_next_search_page()

    def _load_next_search_page(self):
        if self.busy or self.app.closing or self.view != "search" or self.next_search_offset is None:
            return
        self.search_warning = ""
        self.retry_button.pack_forget()
        self._search(self.search_query, self.next_search_offset)

    def _account_changed(self, session):
        self._cancel_batch()
        self._cancel_private()
        self.library = None
        self.library_position = None
        if session is not None:
            self.busy = False
            self.read_library()
        elif self.private_view:
            self._hide_private()

    def read_library(self):
        if self._opening or self.app.closing:
            return
        session = self.account.session
        if session is None:
            self.account.open()
            return
        if self.private_refreshing and self.private_playlist is None:
            return
        self._read_private(session, memory=self.library,
                           position=self.library_position if self.private_view and self.view == "tracks" else None)

    def _open_playlist(self, row=None):
        if row is None:
            selected = self.tree.selection()
            row = selected[0] if len(selected) == 1 else ""
        if self.busy or row not in self.rows or self.account.session is None:
            return
        session, playlist = self.account.session, self.rows[row]
        self.library_position = self._view_position()
        self._read_private(session, playlist=playlist)

    def _private_owner(self, session):
        profile = self.account.account
        if self.page_source == "qq":
            owner = getattr(session, "musicid", None)
            verified = getattr(profile, "musicid", None)
        else:
            owner = getattr(session, "user_id", None)
            verified = getattr(profile, "uid", None)
        return owner if owner and type(owner) is type(verified) and owner == verified else None

    def _queue_owner(self, session):
        if self.account.account is not None:
            return self._private_owner(session)
        field = "musicid" if self.page_source == "qq" else "user_id"
        return getattr(session, field, None)

    def _cancel_private(self):
        self.private_stop.set()
        self.private_serial += 1
        self.private_refreshing = False

    def _hide_private(self):
        self._cancel_private()
        self.busy = self.appending = self.private_view = self.private_complete = False
        self.private_playlist = self.private_position = None
        self._reset()
        self._private_ready()

    def _private_ready(self):
        self.busy = False
        self.resolve_button.configure(state="normal")
        self.library_button.configure(state="normal")
        self._update_actions()

    def _private_result(self, data, playlist):
        rows = data["rows"]
        warning = data.get("warning") or " · ".join(data.get("warnings") or ())
        if not warning and data.get("total") not in (None, len(rows)):
            warning = "平台只返回了部分内容。"
        data = dict(data, source=self.page_source, warning=warning)
        if playlist is None:
            data.update(view="library", title="我的歌单")
        return data

    def _read_private(self, session, *, playlist=None, memory=None, position=None):
        preserve = self.private_view and self.view == "library" and playlist is None and memory is not None
        self._cancel_private()
        self.stop.set()
        self.serial += 1
        self.appending = False
        self.private_view = self.private_refreshing = True
        self.private_playlist, self.private_position = playlist, position
        self.private_complete = False
        self.private_stop = stop = threading.Event()
        serial = self.private_serial
        source = self.page_source
        owner = self._private_owner(session)
        if memory is not None:
            self._paint_private(memory, preserve=preserve)
        else:
            self.busy = True
            self._reset()
            self.resolve_button.configure(state="disabled")
            self.library_button.configure(state="disabled")
            self._update_actions()
            self.status.set("正在读取歌单曲目…" if playlist else f"正在读取{SOURCES[source]}歌单…")

        def post(event, value):
            if not stop.is_set():
                self.app.post("shared_music_private", (source, serial, event, value))

        def load():
            cache = self.app.shared_music_cache
            if owner is not None and (memory is None or memory.get("warning")):
                try:
                    with self.private_cache_access:
                        cached = None if stop.is_set() else cache.read(source, owner, playlist=playlist)
                    if cached is not None:
                        post("cache", self._private_result(cached, playlist))
                except (OSError, sqlite3.Error, ValueError):
                    pass
            if stop.is_set():
                return
            try:
                result = (self.catalog.list_playlists(session, stop=stop) if playlist is None
                          else self.catalog.resolve_playlist(session, playlist, stop=stop))
                result = self._private_result(result, playlist)
            except (OSError, RuntimeError, ValueError) as error:
                if (owner is not None and getattr(error, "authorization_denied", False)
                        and not getattr(error, "session_expired", False)):
                    try:
                        with self.private_cache_access:
                            if not stop.is_set():
                                cache.discard(source, owner, playlist=playlist)
                    except (OSError, sqlite3.Error, ValueError):
                        pass
                post("error", error)
            except Exception:
                post("error", RuntimeError("读取失败，请重试。"))
            else:
                if owner is not None and not result["warning"]:
                    try:
                        with self.private_cache_access:
                            if not stop.is_set():
                                cache.replace(source, owner, result, playlist=playlist)
                    except (OSError, sqlite3.Error, ValueError):
                        pass
                post("done", result)

        threading.Thread(target=load, daemon=True).start()

    def _paint_private(self, result, *, preserve=False):
        self._show(result, preserve=preserve)
        self.private_complete = not bool(result["warning"])
        if self.private_position is not None:
            self._restore_view_position(self.private_position)
            self.private_position = None
        self._private_ready()
        if self.private_refreshing:
            self.status.set("正在同步…" if not result["warning"] else result["warning"])

    def receive_private(self, serial, event, value):
        if serial != self.private_serial or self.app.closing or not self.private_refreshing:
            return
        if event == "cache":
            self._paint_private(value, preserve=not self.busy)
            return
        self.private_refreshing = False
        if event == "done":
            if self.private_complete and value["warning"]:
                self.status.set(f"保留上次完整列表 · {value['warning']}")
            else:
                self._paint_private(value, preserve=not self.busy)
            return

        message = str(value) or "读取失败，请重试。"
        if getattr(value, "authorization_denied", False):
            if self.private_playlist is None:
                self.library = self.library_position = None
            self._hide_private()
            if getattr(value, "session_expired", False):
                self.account.expire()
        elif self.private_complete:
            message = f"保留上次完整列表 · {message}"
        self._private_ready()
        self.status.set(message)

    def _reset(self):
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.source = self.collection_url = ""
        self.total = None
        self.search_query = ""
        self.next_search_offset = None
        self.search_warning = ""
        self.search_urls.clear()
        self.retry_button.pack_forget()
        self.view = "tracks"
        self.caption.set("曲目")
        self.count.set("")
        self.status.set("")

    def _read(self, fetch, status, *, append=False):
        self._cancel_batch()
        self._cancel_private()
        self.serial += 1
        serial = self.serial
        self.stop.set()
        self.stop = stop = threading.Event()
        self.busy = True
        self.appending = append
        self.private_view = self.private_complete = False
        self.private_playlist = self.private_position = None
        if not append:
            self.resolve_button.configure(state="disabled")
            if self.catalog is not None:
                self.library_button.configure(state="disabled")
            self._reset()
        self.status.set(status)
        self._update_actions()

        def post(result, error):
            payload = (serial, result, error)
            if self.page_source:
                payload = (self.page_source, *payload)
            self.app.post("shared_music_result", payload)

        def load():
            try:
                result = fetch(stop)
            except (OSError, RuntimeError, ValueError) as error:
                post(None, str(error) or "读取失败，请重试。")
            except Exception:
                post(None, "读取失败，请重试。")
            else:
                post(result, "")

        threading.Thread(target=load, daemon=True).start()

    def receive(self, serial, result, error):
        if serial != self.serial or self.app.closing:
            return
        append = self.appending
        self.busy = self.appending = False
        self.resolve_button.configure(state="normal")
        if self.catalog is not None:
            self.library_button.configure(state="normal")
        if error:
            self.status.set(error)
            if append:
                self.search_warning = error
                self.retry_button.pack(side="right", padx=(0, 8))
        else:
            self._show(result, append=append)
        self._update_actions()

    @staticmethod
    def _item_identity(item):
        return item.get("id") or item.get("url") or item["title"]

    def _view_position(self):
        order = self.tree.get_children()
        first = self.tree.yview()[0]
        top = order[min(len(order) - 1, round(first * len(order)))] if order else ""
        counts = defaultdict(int)
        keys = {}
        for row, item in self.rows.items():
            identity = self._item_identity(item)
            keys[row] = (identity, counts[identity])
            counts[identity] += 1
        return tuple(keys[row] for row in self.tree.selection() if row in keys), keys.get(self.tree.focus()), keys.get(top), first

    def _restore_view_position(self, position):
        selected, focus, top, first = position
        counts = defaultdict(int)
        lookup = {}
        for row, item in self.rows.items():
            identity = self._item_identity(item)
            lookup[identity, counts[identity]] = row
            counts[identity] += 1
        self.tree.selection_set(*(lookup[key] for key in selected if key in lookup))
        if focus in lookup:
            self.tree.focus(lookup[focus])
        if top in lookup:
            self.tree.yview(self.tree.index(lookup[top]))
        else:
            self.tree.yview_moveto(first)

    def _show(self, result, *, append=False, preserve=False):
        previous = defaultdict(deque)
        if preserve:
            saved_view = self._view_position()
            for row, item in self.rows.items():
                previous[self._item_identity(item)].append(row)
            rendered = {}
        if not append:
            if not preserve:
                self._reset()
            self.source = result["source"]
            self.view = result.get("view", "tracks")
            self.collection_url = _official_url(result.get("url"), self.source)
            self.caption.set(result["title"] if self.page_source else f"{SOURCES[self.source]} · {result['title']}")
        if result.get("total") is not None or not append:
            self.total = result.get("total")
        library = self.view == "library"
        search = self.view == "search"
        if search:
            self.search_query = result["query"]
        if library:
            self.library = result
        for column, label in (("title", "歌单" if library else "歌曲"), ("artist", "类型" if library else "艺人"),
                              ("duration", "曲目数" if library else "时长")):
            if label != self.sort.headings[column]:
                self.sort.set_heading(column, label)
        kinds = {"liked": "我喜欢", "created": "创建", "collected": "收藏"}
        order = self.tree.get_children() if append and self.sort.column else ()
        anchor = order[min(len(order) - 1, round(self.tree.yview()[0] * len(order)))] if order else ""
        previous_count = len(self.rows)
        for position, item in enumerate(result["rows"], 1):
            if search:
                url = _official_url(item.get("url"), self.source)
                if not url or url in self.search_urls:
                    continue
                self.search_urls.add(url)
            if library:
                item = dict(item, position=position, artist=kinds.get(item.get("kind"), ""), duration=item.get("total"))
                duration = item["duration"] if item["duration"] is not None else "—"
            else:
                duration = _duration(item.get("duration"))
            values = (item.get("position") or "—", item["title"], item.get("artist") or "—", duration)
            if preserve and (matches := previous[self._item_identity(item)]):
                row = matches.popleft()
                if self.rows[row] != item:
                    self.tree.item(row, values=values)
            else:
                row = self.tree.insert("", "end", values=values)
            if preserve:
                rendered[row] = item
            else:
                self.rows[row] = item
        if preserve:
            self.tree.delete(*self.rows.keys() - rendered.keys())
            self.rows = rendered
            self.tree.set_children("", *rendered)
        elif len(self.rows) == 1 and not append:
            self.tree.selection_set(next(iter(self.rows)))
        self.sort.apply()
        if preserve:
            self._restore_view_position(saved_view)
        elif anchor:
            self.tree.see(anchor)
        count, total = len(self.rows), self.total
        if search:
            next_offset = result.get("next_offset")
            progressed = type(next_offset) is int and next_offset > result["offset"] and count > previous_count
            self.next_search_offset = next_offset if progressed else None
            self.count.set(f"已显示 {count} / {total} 首" if self.next_search_offset is not None and total and total > count
                           else f"{count} 首")
            self.status.set(result.get("warning") or ("" if count else "没有找到歌曲。"))
        elif library:
            self.count.set(f"{count} 张歌单")
            self.status.set(result.get("warning") or ("" if count else "当前账号没有可读取的歌单。"))
        elif total is not None and total > count:
            self.count.set(f"已读取 {count} / {total} 首")
            self.status.set(result.get("warning") or "页面只返回部分曲目。")
        else:
            self.count.set(f"{count} 首" if count else "")
            self.status.set(result.get("warning") or ("" if count else "这个页面没有返回曲目。"))

    def _selected_url(self, row=None):
        if row is None:
            row = next(iter(self.tree.selection()), "")
        return _official_url(self.rows.get(row, {}).get("url"), self.source)

    def _update_actions(self, _event=None):
        library = self.view == "library"
        self.export_button.configure(state="normal" if self.rows and not self._opening and not library else "disabled")
        can_open = not self._opening and (self.collection_url or self._selected_url())
        self.open_button.configure(state="normal" if can_open else "disabled")
        if self.catalog is not None:
            can_go_back = self.private_view and self.view == "tracks" and self.library is not None
            self.library_button.configure(text="返回歌单" if can_go_back else "我的歌单")
            if library:
                self.open_playlist_button.grid()
                single = len(self.tree.selection()) == 1 and not self._opening
                self.open_playlist_button.configure(state="normal" if single else "disabled")
            else:
                self.open_playlist_button.grid_remove()
        if self.page_source in ("qq", "netease"):
            can_enqueue = self.source == self.page_source and not self._opening and not self.batch_busy
            if library:
                can_enqueue = can_enqueue and self.account.session is not None
            selected = can_enqueue and bool(self.tree.selection())
            self.download_button.configure(state="normal" if selected else "disabled")
            self.queue_button.configure(state="normal" if selected else "disabled")
            self.queue_all_button.configure(state="normal" if self.rows and can_enqueue and self.view != "search" else "disabled")

    def _enqueue(self, begin, *, all_rows=False):
        if self.source != self.page_source or not self.source or self._opening or self.batch_busy or self.app.closing:
            return
        if all_rows and self.view == "search":
            return
        selected = set(self.tree.selection())
        items = [dict(item) for row, item in self.rows.items() if all_rows or row in selected]
        if self.view == "library":
            self._resolve_playlists(items, begin)
            return
        collection_key = ""
        if self.private_view and self.private_playlist is not None:
            owner = self._queue_owner(self.account.session)
            try:
                collection_key = shared_music.private_collection_key(self.source, owner, self.private_playlist)
            except ValueError as error:
                self.status.set(str(error))
                return
        rows = list(shared_music.queue_rows(self.source, items, collection_url=self.collection_url,
                                            batch_title=self.caption.get(), total=self.total, private=self.private_view,
                                            collection_key=collection_key))
        if rows:
            count = self.app.enqueue_tasks(rows, begin)
            if count and not begin:
                self.status.set(f"已加入队列 {count} 首")

    def _cancel_batch(self):
        self.batch_stop.set()
        self.batch_serial += 1
        self.batch_busy = False
        self.batch_session = self.batch_owner = None

    def _resolve_playlists(self, playlists, begin):
        if not playlists:
            return
        session = self.account.session
        owner = self._queue_owner(session)
        cache_owner = self._private_owner(session)
        try:
            selections = [(playlist, shared_music.private_collection_key(self.page_source, owner, playlist))
                          for playlist in sorted(playlists, key=lambda item: item["position"])]
        except (KeyError, ValueError):
            self.status.set("请先确认当前账号并刷新歌单。")
            return
        self._cancel_batch()
        self.batch_stop = stop = threading.Event()
        serial = self.batch_serial
        self.batch_busy = True
        self.batch_session, self.batch_owner = session, owner
        self._update_actions()
        self.status.set("正在读取所选歌单的曲目…")
        source = self.page_source

        def post(event, value):
            if not stop.is_set():
                self.app.post("shared_music_batch", (source, serial, event, value))

        def resolve():
            queued, warnings = [], []
            for index, (playlist, key) in enumerate(selections, 1):
                if stop.is_set():
                    return
                post("progress", f"正在读取 {index} / {len(selections)} · {playlist['title']}")
                try:
                    result, warning = self._batch_playlist(source, cache_owner, session, playlist, stop)
                    if stop.is_set():
                        return
                    if warning:
                        warnings.append(f"{playlist['title']} · {warning}")
                    rows = list(shared_music.queue_rows(source, result["rows"], collection_url=result.get("url", ""),
                                                        batch_title=result["title"], total=result.get("total"),
                                                        private=True, collection_key=key))
                    if len(rows) < len(result["rows"]):
                        warnings.append(f"{playlist['title']} · 已跳过 {len(result['rows']) - len(rows)} 首无有效链接的曲目")
                    if not rows:
                        warnings.append(f"{playlist['title']} · 没有可加入队列的曲目")
                    queued.extend(rows)
                except (OSError, RuntimeError, ValueError) as error:
                    if stop.is_set():
                        return
                    if getattr(error, "session_expired", False):
                        post("expired", str(error))
                        return
                    warnings.append(f"{playlist['title']} · {error}")
                except Exception:
                    warnings.append(f"{playlist['title']} · 读取失败，请重试")
            post("done", (queued, warnings, begin))

        threading.Thread(target=resolve, daemon=True).start()

    def _batch_playlist(self, source, owner, session, playlist, stop):
        cache = self.app.shared_music_cache
        with self.private_cache_access:
            cached = None if stop.is_set() or owner is None else cache.read(source, owner, playlist=playlist)
        try:
            result = self.catalog.resolve_playlist(session, playlist, stop=stop)
            result = self._private_result(result, playlist)
        except (OSError, RuntimeError) as error:
            if getattr(error, "authorization_denied", False):
                with self.private_cache_access:
                    if owner is not None and not stop.is_set() and not getattr(error, "session_expired", False):
                        cache.discard(source, owner, playlist=playlist)
                raise
            code = getattr(error, "code", None)
            protocol_error = isinstance(error, (qq_catalog.QqCatalogError, netease_catalog.NeteaseCatalogError))
            transient = not protocol_error or code == 429 or (type(code) is int and 500 <= code < 600)
            if cached is None or not transient:
                raise
            return cached, "连接失败，使用上次完整曲目"
        if result["warning"]:
            if cached is not None:
                return cached, f"使用上次完整曲目 · {result['warning']}"
            return result, result["warning"]
        with self.private_cache_access:
            if owner is not None and not stop.is_set():
                cache.replace(source, owner, result, playlist=playlist)
        return result, ""

    def receive_batch(self, serial, event, value):
        if serial != self.batch_serial or not self.batch_busy or self.app.closing:
            return
        if self.account.session is not self.batch_session or self._queue_owner(self.batch_session) != self.batch_owner:
            self._cancel_batch()
            self._update_actions()
            return
        if event == "progress":
            self.status.set(value)
            return
        self.batch_busy = False
        self.batch_session = self.batch_owner = None
        if event == "expired":
            self.account.expire()
            self.status.set(value)
            return
        self._update_actions()
        rows, warnings, begin = value
        count = self.app.enqueue_tasks(rows, begin) if rows else 0
        notices = [f"已确认 {count} 首在队列中"] if count else []
        if warnings:
            notices.append(warnings[0])
            if len(warnings) > 1:
                notices.append(f"另有 {len(warnings) - 1} 条提示")
            for warning in warnings:
                self.app.log_message(warning)
        self.status.set(" · ".join(notices) or "没有可加入队列的曲目。")

    def _open_selected(self, _event=None):
        if self.view == "library":
            self._open_playlist()
        else:
            self.open_original()
        return "break"

    def _open_clicked(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if self.view == "library":
                self._open_playlist(row)
            else:
                self.open_original(row)

    def close(self):
        self._cancel_batch()
        self.stop.set()
        self._cancel_private()
        if self.catalog is not None:
            self.account.close()

    def open_original(self, row=None):
        if self._opening or self.app.closing:
            return
        url = self._selected_url(row) or self.collection_url
        if url:
            try:
                if webbrowser.open(url):
                    return
            except (OSError, webbrowser.Error):
                pass
            self.status.set("无法打开默认浏览器。")

    def export_csv(self):
        if not self.rows or self.view == "library" or self._opening or self.app.closing:
            return
        path = filedialog.asksaveasfilename(parent=self.tree, title="导出曲目 CSV", initialfile="曲目.csv",
                                            defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        order = self.tree.get_children()
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(("序号", "歌曲", "艺人", "专辑", "时长（秒）", "链接"))
                for row in order:
                    item = self.rows[row]
                    writer.writerow([_csv_cell(item.get(key)) for key in ("position", "title", "artist", "album", "duration")]
                                    + [self._selected_url(row)])
        except OSError as error:
            self.app.error("无法导出 CSV", error)
            return
        self.status.set(f"已导出 {len(order)} 首")
