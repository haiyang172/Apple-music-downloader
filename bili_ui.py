"""Resolve Bilibili links and select video parts in the desktop app."""

import threading
import tkinter as tk
from tkinter import font, ttk

import bili_catalog
import bili_collections
from apple_download import bilibili_category
from music_browser import TreeSort
from qr_account_ui import BiliAccountPanel
from share_links import links_in_text


def _video_duration(value):
    if not isinstance(value, int) or value <= 0:
        return "—"
    minutes, seconds = divmod(value, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


class BiliBrowser:
    def __init__(self, app, page, formats):
        self.app = app
        self.serial = 0
        self.busy = False
        self.rows = {}
        self.result = self.parent_view = None
        self.stop = threading.Event()
        self.link = tk.StringVar(page)
        self.caption = tk.StringVar(page, "视频分 P")
        self.metadata = tk.StringVar(page)
        self.status = tk.StringVar(page)
        self.count = tk.StringVar(page)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        bar = ttk.Frame(page, style="Surface.TFrame")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        app.label(bar, "B 站", large=True, background=app.background).grid(row=0, column=0, sticky="w")
        self.account = BiliAccountPanel(app, bar, app.bili_auth)
        self.account.button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.video_format = formats["video"]
        self.format = ttk.Combobox(bar, textvariable=app.bili_format, values=tuple(formats.values()), width=15,
                                   state="readonly", style="Surface.TCombobox")
        self.format.grid(row=0, column=0, sticky="e")
        self.format.bind("<<ComboboxSelected>>", app.quality_changed)
        app.label(bar, "视频、合集、系列或收藏夹链接", muted=True, background=app.background).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 3)
        )
        self.entry = ttk.Entry(bar, textvariable=self.link, style="MusicSearch.TEntry", font=(app.font, 13))
        self.entry.grid(row=2, column=0, sticky="ew")
        self.entry.bind("<Return>", self.resolve)
        self.resolve_button = app.action(bar, "解析", self.resolve, primary=True)
        self.resolve_button.grid(row=2, column=1, padx=(8, 0))

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
        details = ttk.Frame(toolbar, style="MusicToolbar.TFrame")
        details.grid(row=1, column=0, columnspan=4, sticky="ew", padx=(3, 0), pady=(3, 0))
        self.back_button = app.action(details, "返回列表", self.back)
        self.parts_button = app.action(details, "查看分 P", self.open_parts)
        ttk.Label(details, textvariable=self.metadata, style="MusicStatus.TLabel", width=1).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        self.queue_all_button = app.action(toolbar, "全部入队", self.queue_all)
        self.queue_all_button.grid(row=0, column=1, padx=(0, 6))
        self.queue_button = app.action(toolbar, "所选入队", self.queue_selected)
        self.queue_button.grid(row=0, column=2)
        self.download_button = app.action(toolbar, "下载所选", self.download_selected, primary=True)
        self.download_button.grid(row=0, column=3, padx=(6, 0))

        headings = {"position": "分 P", "title": "标题", "artist": "歌手", "category": "分类", "duration": "时长"}
        self.tree = ttk.Treeview(card, columns=tuple(headings), show="headings",
                                 selectmode="extended", style="Queue.Treeview")
        self.tree.grid(row=1, column=0, sticky="nsew")
        heading_font = font.Font(root=self.tree, family=app.font, size=10, weight="bold")
        row_font = font.Font(root=self.tree, family=app.font, size=11)
        self.position_width = max(round(64 * app.scale), heading_font.measure("序号  ▼") + round(20 * app.scale))
        self.duration_width = max(round(70 * app.scale), row_font.measure("12:59:59") + round(14 * app.scale))
        for column in headings:
            aligned_left = column in ("title", "artist", "category")
            width = {"title": 220, "artist": 92, "category": 101}.get(column, 64)
            self.tree.column(column, width=round(width * app.scale),
                             minwidth=1, stretch=False, anchor="w" if aligned_left else "center")
            self.tree.heading(column, anchor="w" if aligned_left else "center")
        self.tree.bind("<Configure>", self._fit_columns)
        self.sort = TreeSort(self.tree, headings, self._sort_value)
        self._update_audio_view()
        self.app.bili_format.trace_add("write", self._update_audio_view)
        for key in ("bili_auto_category", "category"):
            self.app.fields[key].trace_add("write", self._update_categories)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Return>", self._queue_from_keyboard)
        self.tree.bind("<Double-1>", self._open_clicked)
        self.tree.bind("<Button-3>", self._row_menu)
        self.song_menu = tk.Menu(self.tree, tearoff=False)
        self.song_menu.add_command(label="编辑歌名和歌手", command=self.edit_song)
        scrollbar = ttk.Scrollbar(card, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(round(5 * app.scale), 0))
        self.tree.configure(yscrollcommand=scrollbar.set)

        foot = ttk.Frame(card, style="MusicToolbar.TFrame")
        foot.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        ttk.Label(foot, textvariable=self.status, style="MusicStatus.TLabel", width=1).pack(
            side="left", fill="x", expand=True, padx=(3, 8)
        )
        self.view_queue_button = app.action(foot, "查看队列", self.open_queue)
        self.view_queue_button.pack(side="right", padx=(8, 0))
        ttk.Label(foot, textvariable=self.count, style="MusicStatus.TLabel").pack(side="right", padx=(0, 3))
        self._selection_changed()

    def resolve(self, _event=None):
        if self.busy or self.app.closing:
            return "break"
        value = self.link.get().strip()
        if not value:
            self.status.set("请先输入视频、合集、系列或收藏夹链接。")
            self.entry.focus_set()
            return "break"
        links = links_in_text(value, lambda link: bili_catalog.is_video_link(link) or bili_collections.is_collection_link(link))
        if len(links) != 1:
            self.status.set("请一次粘贴一个 B 站视频、合集、系列或收藏夹链接。")
            return "break"
        self.parent_view = None
        self._read(links[0])
        return "break"

    def load_link(self, value):
        if self.busy or self.app.closing:
            return False
        self.link.set(value)
        self.resolve()
        return True

    def _read(self, value):
        self.serial += 1
        serial = self.serial
        self.busy = True
        self.stop = stop = threading.Event()
        self.resolve_button.configure(state="disabled")
        self.rows.clear()
        self.result = None
        self.tree.delete(*self.tree.get_children())
        self.caption.set("合集 / 收藏夹" if bili_collections.is_collection_link(value) else "视频分 P")
        self.metadata.set("")
        self.status.set("正在读取 B 站…")
        self._selection_changed()
        session = self.account.session

        def fetch():
            try:
                if bili_collections.is_collection_link(value):
                    result = bili_collections.resolve_collection(value, session=session, stop=stop)
                else:
                    result = bili_catalog.resolve_video(value, session=session)
            except (OSError, RuntimeError, ValueError) as error:
                self.app.post("bili_result", (serial, None, str(error)))
            except Exception:
                self.app.post("bili_result", (serial, None, "解析失败，请重试。"))
            else:
                self.app.post("bili_result", (serial, result, ""))

        threading.Thread(target=fetch, daemon=True).start()

    def receive(self, serial, result, error):
        if serial != self.serial or self.app.closing:
            return
        self.busy = False
        self.resolve_button.configure(state="normal")
        if error:
            self.status.set(error)
        else:
            self._render(result)
        self._selection_changed()

    def _render(self, result):
        self.result = result
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.caption.set(result["title"])
        collection = bool(result.get("collection_kind"))
        uploader, category = result.get("uploader"), result.get("source_category")
        summary = "列表入队默认取每条视频的 P1" if collection else category
        self.metadata.set("  ·  ".join(value for value in (uploader, summary) if value))
        self.sort.set_heading("position", "序号" if collection else "分 P")
        for item in result["rows"]:
            position = item["position"]
            row = self.tree.insert("", "end", values=(position if collection else f"P{position}", self._display_title(item),
                                                      self._artist(item), self._category(item),
                                                      _video_duration(item["duration"])))
            self.rows[row] = item
        self.sort.apply()
        self.status.set(result.get("warning") or ("" if self.rows else "没有找到可用的视频。"))
        if len(self.rows) == 1:
            self.tree.selection_set(next(iter(self.rows)))

    def _display_title(self, item):
        if self.app.bili_format.get() != self.video_format:
            return self._audio_title(item) or item["title"]
        return item["title"]

    @staticmethod
    def _audio_title(item):
        title = item.get("audio_title")
        return item.get("preview_audio_title") if title is None else title

    @staticmethod
    def _artist(item):
        key = "preview_artist" if item.get("audio_title") is None else "artist"
        return item.get(key) or ""

    def _category(self, item):
        direct = self.app.fields["category"].get().strip()
        if direct or not self.app.fields["bili_auto_category"].get():
            return direct
        source = item.get("source_category") or item.get("preview_source_category") or ""
        media = item.get("media_category") or item.get("preview_media_category") or ""
        parent = item.get("batch_title", "") if self.result and not self.result.get("collection_kind") else ""
        return bilibili_category(item["title"], source, media, parent_title=parent)

    def _sort_value(self, row, column):
        item = self.rows[row]
        return self.tree.set(row, column) if column in ("title", "artist", "category") else item[column]

    def _update_audio_view(self, *_):
        audio = self.app.bili_format.get() != self.video_format
        visible = ("position", "title", "artist", "category", "duration") if audio else ("position", "title", "duration")
        self.tree.configure(displaycolumns=visible)
        self._fit_columns()
        if self.sort.column not in (None, *visible):
            self.sort.reset()
            self.tree.set_children("", *sorted(self.rows, key=lambda row: self.rows[row]["position"]))
        self.sort.set_heading("title", "歌名 / 标题" if audio else "标题")
        for row, item in self.rows.items():
            self.tree.set(row, "title", self._display_title(item))
        self.sort.apply()
        self._selection_changed()

    def _fit_columns(self, _event=None):
        available = self.tree.winfo_width() - 2
        if available < 12:
            return
        position = min(self.position_width, available // 6)
        duration = min(self.duration_width, available // 5)
        title = available - position - duration
        widths = {"position": position, "duration": duration}
        if "artist" in self.tree.cget("displaycolumns"):
            widths["artist"] = max(1, round(title * 0.25))
            widths["category"] = max(1, round(title * 0.27))
            title -= widths["artist"] + widths["category"]
        widths["title"] = title
        for column, width in widths.items():
            if self.tree.column(column, "width") != width:
                self.tree.column(column, width=width)

    def _update_categories(self, *_):
        for row, item in self.rows.items():
            self.tree.set(row, "category", self._category(item))
        if self.sort.column == "category":
            self.sort.apply()

    def open_parts(self, row=None):
        if self.busy or not self.result or not self.result.get("collection_kind"):
            return
        if row is None:
            selection = self.tree.selection()
            if len(selection) != 1:
                return
            row, = selection
        if row not in self.rows:
            return
        self.parent_view = (self.result, self.link.get(), self.rows[row]["position"], self.tree.yview()[0])
        value = self.rows[row]["url"].partition("?")[0]
        self.link.set(value)
        self._read(value)

    def _open_clicked(self, event):
        if self.tree.identify_region(event.x, event.y) in ("tree", "cell"):
            self.open_parts(self.tree.identify_row(event.y))

    def _row_menu(self, event):
        if self.busy or self.app.closing or self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        row = self.tree.identify_row(event.y)
        if row not in self.rows:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        try:
            self.song_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.song_menu.grab_release()
        return "break"

    def edit_song(self):
        selected = self.tree.selection()
        if self.busy or self.app.closing or len(selected) != 1:
            return
        row, = selected
        item = self.rows.get(row)
        if item is None:
            return

        app = self.app
        dialog = tk.Toplevel(app.window, background=app.background)
        dialog.title("编辑歌名和歌手")
        dialog.transient(app.window)
        dialog.resizable(False, False)
        song = tk.StringVar(dialog, self._audio_title(item) or item["title"])
        artist = tk.StringVar(dialog, self._artist(item))
        status = tk.StringVar(dialog)
        card = app.panel(dialog, padding=(round(16 * app.scale), round(13 * app.scale)))
        card.pack(fill="both", expand=True, padx=10, pady=10)
        card.columnconfigure(0, weight=1, minsize=round(330 * app.scale))
        app.label(card, "歌名").grid(row=0, column=0, sticky="w", pady=(0, 4))
        title_entry = ttk.Entry(card, textvariable=song, style="MusicSearch.TEntry")
        title_entry.grid(row=1, column=0, sticky="ew")
        app.label(card, "歌手").grid(row=2, column=0, sticky="w", pady=(9, 4))
        ttk.Entry(card, textvariable=artist, style="MusicSearch.TEntry").grid(row=3, column=0, sticky="ew")
        app.label(card, variable=status, muted=True).grid(row=4, column=0, sticky="w", pady=(4, 0))

        def swap():
            previous_title = song.get()
            song.set(artist.get())
            artist.set(previous_title)

        def apply(_event=None):
            title = song.get().strip()
            if not title:
                status.set("请输入歌名。")
                title_entry.focus_set()
                return "break"
            if not app.closing and self.rows.get(row) is item:
                item.update(audio_title=title, artist=artist.get().strip(), media_category="music", song_edited=True)
                if not item.get("source_category") and item.get("preview_source_category"):
                    item["source_category"] = item["preview_source_category"]
                self.tree.set(row, "title", self._display_title(item))
                self.tree.set(row, "artist", self._artist(item))
                self.tree.set(row, "category", self._category(item))
                self.sort.apply()
                self._selection_changed()
            dialog.destroy()
            return "break"

        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        app.action(actions, "互换", swap).pack(side="left")
        app.action(actions, "应用", apply, primary=True).pack(side="right")
        app.action(actions, "取消", dialog.destroy).pack(side="right", padx=(0, 7))
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.bind("<Return>", apply)
        dialog.grab_set()
        title_entry.focus_set()
        title_entry.selection_range(0, "end")

    def back(self):
        if not self.parent_view or self.busy:
            return
        result, link, position, top = self.parent_view
        self.parent_view = None
        self.link.set(link)
        self._render(result)
        self.tree.selection_set(*(row for row, item in self.rows.items() if item["position"] == position))
        self.tree.yview_moveto(top)
        self._selection_changed()

    def _selection_changed(self, _event=None):
        selection = tuple(row for row in self.tree.selection() if row in self.rows)
        selected = len(selection)
        hint = f"已选 {selected} / {len(self.rows)}" if self.rows else ""
        if selected == 1 and self.app.bili_format.get() == self.video_format and (title := self._audio_title(self.rows[selection[0]])):
            artist = self._artist(self.rows[selection[0]])
            label = f"{artist} - {title}" if artist else title
            hint = f"音频命名 · {label[:28]}{'…' if len(label) > 28 else ''}"
        self.count.set(hint)
        self.queue_button.configure(state="normal" if selected else "disabled")
        self.download_button.configure(state="normal" if selected else "disabled")
        self.queue_all_button.configure(state="normal" if self.rows else "disabled")
        if self.result and self.result.get("collection_kind"):
            self.parts_button.pack(side="right")
            self.parts_button.configure(state="normal" if selected == 1 else "disabled")
        else:
            self.parts_button.pack_forget()
        if self.parent_view:
            self.back_button.pack(side="right")
            self.back_button.configure(state="disabled" if self.busy else "normal")
        else:
            self.back_button.pack_forget()

    def _select_all(self, _event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _queue_from_keyboard(self, _event):
        if self.result and self.result.get("collection_kind"):
            self.open_parts()
        else:
            self.queue_selected()
        return "break"

    def queue_selected(self):
        self._queue(self.tree.selection())

    def download_selected(self):
        self._queue(self.tree.selection(), begin=True)

    def queue_all(self):
        self._queue(self.rows)

    def open_queue(self):
        self.app.show_download_source("bilibili")

    def _queue(self, row_ids, begin=False):
        rows = sorted((self.rows[row] for row in row_ids if row in self.rows), key=lambda item: item["position"])
        if not rows or self.app.closing:
            return
        count = self.app.queue_bili(rows, begin=begin)
        if count:
            self.status.set(f"已入队 {count} 项")
