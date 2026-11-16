"""Search songs, browse shared links, and queue tracks."""

import csv
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


SOURCES = {"qq": "QQ 音乐", "netease": "网易云音乐"}
OFFICIAL_HOSTS = {"qq": {"y.qq.com"}, "netease": {"music.163.com"}}
CATALOGS = {"qq": qq_catalog, "netease": netease_catalog}
ACCOUNT_PANELS = {"qq": QqAccountPanel, "netease": NeteaseAccountPanel}


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
        link_source = shared_music.shared_music_source(value)
        if self.catalog is not None and not link_source and "://" not in value and not value.startswith("www."):
            self._search(value)
            return "break"
        if self.page_source and link_source != self.page_source:
            self.status.set(f"请粘贴{SOURCES[self.page_source]}的歌曲或歌单链接。")
            return "break"

        if self.page_source == "qq":
            session = self.account.session
            self._read(lambda stop: qq_catalog.resolve_public_qq(value, session=session, stop=stop), "正在读取公开曲目…")
        else:
            self._read(lambda _stop: shared_music.resolve_music(value), "正在读取公开曲目…")
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
        self.library = None
        self.library_position = None
        if session is not None:
            self.busy = False
            self.read_library()
        elif session is None and self.private_view:
            self.stop.set()
            self.serial += 1
            self.busy = self.appending = self.private_view = False
            self.resolve_button.configure(state="normal")
            self.library_button.configure(state="normal", text="我的歌单")
            self._reset()
            self._update_actions()

    def read_library(self):
        if self._opening or self.app.closing:
            return
        session = self.account.session
        if session is None:
            self.account.open()
            return
        if self.library is not None and self.private_view and self.view == "tracks":
            self._show(self.library)
            if self.library_position is not None:
                identifier, top = self.library_position
                self.tree.selection_set(*(row for row, item in self.rows.items() if item["id"] == identifier))
                self.tree.yview_moveto(top)
            self._update_actions()
            return

        def fetch(stop):
            data = self.catalog.list_playlists(session, stop=stop)
            warnings = data.get("warnings") or ()
            return dict(data, source=self.page_source, view="library", title="我的歌单",
                        warning=data.get("warning") or " · ".join(warnings))

        self._read(fetch, f"正在读取{SOURCES[self.page_source]}歌单…", private=True)

    def _open_playlist(self, row=None):
        if row is None:
            row = next(iter(self.tree.selection()), "")
        if self.busy or row not in self.rows or self.account.session is None:
            return
        session, playlist = self.account.session, self.rows[row]
        self.library_position = (playlist["id"], self.tree.yview()[0])
        self._read(lambda stop: self.catalog.resolve_playlist(session, playlist, stop=stop),
                   "正在读取歌单曲目…", private=True)

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

    def _read(self, fetch, status, *, private=False, append=False):
        self.serial += 1
        serial = self.serial
        self.stop.set()
        self.stop = stop = threading.Event()
        self.busy = True
        self.appending = append
        self.private_view = private
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

    def _show(self, result, *, append=False):
        if not append:
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
            row = self.tree.insert("", "end", values=(item.get("position") or "—", item["title"],
                                                      item.get("artist") or "—", duration))
            self.rows[row] = item
        if len(self.rows) == 1 and not append:
            self.tree.selection_set(next(iter(self.rows)))
        self.sort.apply()
        if anchor:
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
                self.open_playlist_button.configure(state="normal" if self.tree.selection() else "disabled")
            else:
                self.open_playlist_button.grid_remove()
        if self.page_source in ("qq", "netease"):
            can_enqueue = self.source == self.page_source and not self._opening and not library
            selected = can_enqueue and bool(self.tree.selection())
            self.download_button.configure(state="normal" if selected else "disabled")
            self.queue_button.configure(state="normal" if selected else "disabled")
            self.queue_all_button.configure(state="normal" if self.rows and can_enqueue and self.view != "search" else "disabled")

    def _enqueue(self, begin, *, all_rows=False):
        if self.source != self.page_source or not self.source or self.view == "library" or self._opening or self.app.closing:
            return
        if all_rows and self.view == "search":
            return
        selected = set(self.tree.selection())
        in_playlist = self.private_view or "/playlist" in urlsplit(self.collection_url).path
        rows = []
        for row, item in self.rows.items():
            if not all_rows and row not in selected:
                continue
            url = self._selected_url(row)
            path = urlsplit(url).path
            if not (path == "/song" if self.source == "netease" else "/songDetail/" in path):
                continue
            task = dict(item, source=self.source, url=url)
            if in_playlist:
                group = self.collection_url or self.caption.get()
                task.update(batch_key=f"{self.source}:{group}", batch_title=self.caption.get(), total=self.total)
            else:
                task.pop("position", None)
            rows.append(task)
        if rows:
            count = self.app.enqueue_tasks(rows, begin)
            if count and not begin:
                self.status.set(f"已加入队列 {count} 首")

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
        self.stop.set()
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
