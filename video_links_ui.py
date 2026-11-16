"""Browse official YouTube and Douyin links from separate desktop pages."""

import re
import threading
import tkinter as tk
from tkinter import ttk
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
import webbrowser


SOURCES = {"youtube": "YouTube", "douyin": "抖音"}
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_LIST = re.compile(r"[A-Za-z0-9_-]{2,120}")
_DOUYIN_HOSTS = {"douyin.com", "www.douyin.com", "m.douyin.com"}
_DOUYIN_ITEM = re.compile(r"[0-9]{1,24}")


def _only(query, name):
    fields = query.get(name, ())
    return fields[0] if len(fields) == 1 else ""


def _youtube_link(parts, host, query):
    if host == "youtu.be":
        path = re.fullmatch(r"/([A-Za-z0-9_-]{11})/?", parts.path)
        video = path[1] if path else ""
    elif parts.path in ("/watch", "/watch/"):
        video = _only(query, "v")
    else:
        path = re.fullmatch(r"/(?:shorts|live|embed|v)/([A-Za-z0-9_-]{11})/?", parts.path)
        video = path[1] if path else ""

    base = "https://music.youtube.com" if host == "music.youtube.com" else "https://www.youtube.com"
    playlist = _only(query, "list")
    if _YOUTUBE_ID.fullmatch(video):
        params = {"v": video}
        if _YOUTUBE_LIST.fullmatch(playlist):
            params["list"] = playlist
        timestamp = _only(query, "t")
        if re.fullmatch(r"[0-9hms]{1,16}", timestamp):
            params["t"] = timestamp
        return "youtube", f"{base}/watch?{urlencode(params)}"
    if host != "youtu.be" and parts.path in ("/playlist", "/playlist/") and _YOUTUBE_LIST.fullmatch(playlist):
        return "youtube", f"{base}/playlist?{urlencode({'list': playlist})}"
    return None


def _douyin_link(parts, host, query):
    if host == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]{1,64}/?", parts.path):
        return "douyin", f"https://{host}{parts.path}"
    if host == "www.iesdouyin.com" and re.fullmatch(r"/share/(?:video|note)/[A-Za-z0-9_=+-]{1,256}/?", unquote(parts.path)):
        # The platform's sharing path may contain an opaque identifier and share parameters.
        params = {key: field for key in ("region", "mid", "u_code", "titleType")
                  if re.fullmatch(r"[A-Za-z0-9_-]{1,120}", field := _only(query, key))}
        return "douyin", parts._replace(netloc=host, query=urlencode(params), fragment="").geturl()
    if host in _DOUYIN_HOSTS:
        match = re.fullmatch(r"/(video|note)/([0-9]{1,24})/?", parts.path)
        if match:
            return "douyin", f"https://www.douyin.com/{match[1]}/{match[2]}"
        modal = _only(query, "modal_id")
        if parts.path in ("", "/") and _DOUYIN_ITEM.fullmatch(modal):
            return "douyin", f"https://www.douyin.com/?{urlencode({'modal_id': modal})}"
    return None


def _parse_video_link(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 4096 or any(character.isspace() or ord(character) < 33 or ord(character) == 127
                                or character == "\\" for character in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.netloc.lower() != parts.hostname:
            return None
        host = parts.hostname
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=40)
    except ValueError:
        return None
    if host in _YOUTUBE_HOSTS or host == "youtu.be":
        return _youtube_link(parts, host, query)
    return _douyin_link(parts, host, query)


def video_link_source(value):
    """Return the source for a recognized official video link, or None."""
    result = _parse_video_link(value)
    return result[0] if result else None


def is_video_source_link(value):
    return video_link_source(value) is not None


class VideoLinkPage:
    def __init__(self, app, page, source, formats):
        self.app = app
        self.source = source
        self.link = tk.StringVar(page)
        self.status = tk.StringVar(page)
        self.rows = {}
        self.serial = 0
        self.busy = False
        self.stop = threading.Event()
        name = SOURCES[source]

        page.columnconfigure(0, weight=1)
        app.label(page, name, large=True, background=app.background).grid(row=0, column=0, sticky="w")
        if source == "youtube":
            self.format = ttk.Combobox(page, textvariable=app.youtube_format, values=tuple(formats), width=15,
                                       state="readonly", style="Surface.TCombobox")
            self.format.grid(row=0, column=0, sticky="e")
            self.format.bind("<<ComboboxSelected>>", app.quality_changed)
        card = app.panel(page, padding=(round(16 * app.scale), round(14 * app.scale)))
        card.grid(row=1, column=0, sticky="ew", pady=(round(12 * app.scale), 0))
        card.columnconfigure(0, weight=1)
        description = "视频、Shorts 或播放列表链接" if source == "youtube" else "视频、图文或抖音短链"
        ttk.Label(card, text=description, style="MusicStatus.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.entry = ttk.Entry(card, textvariable=self.link, font=(app.font, 13))
        self.entry.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.entry.bind("<Return>", self.open_original)
        self.open_button = app.action(card, "在浏览器打开", self.open_original, primary=source == "douyin")
        self.open_button.grid(row=1, column=1, padx=(8, 0), pady=(8, 0))
        ttk.Label(card, textvariable=self.status, style="MusicStatus.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        if source == "youtube":
            self._build_downloads(page, card)
        else:
            app.action(card, "导入已保存文件", self.import_saved).grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        self.link.trace_add("write", self._update_link)
        self._update_link()

    def _update_link(self, *_):
        value = self.link.get().strip()
        recognized = video_link_source(value) == self.source
        self.open_button.configure(state="normal" if recognized else "disabled")
        self.status.set(f"请输入有效的 {SOURCES[self.source]} 链接。" if value and not recognized else "")
        if self.source == "youtube":
            self.stop.set()
            self.serial += 1
            self.busy = False
            self.rows.clear()
            self.tree.delete(*self.tree.get_children())
            self.caption.set("视频")
            self.read_button.configure(state="normal" if recognized else "disabled")
            self._update_download_buttons()

    def load_link(self, value):
        self.link.set(value)

    def open_original(self, _event=None):
        parsed = _parse_video_link(self.link.get())
        if not self.app.closing and parsed and parsed[0] == self.source:
            try:
                opened = webbrowser.open(parsed[1])
            except (OSError, webbrowser.Error):
                opened = False
            if opened:
                self.status.set("保存后可导入文件" if self.source == "douyin" else "已在浏览器打开")
            else:
                self.status.set("无法打开默认浏览器。")
        return "break"

    def import_saved(self):
        if self.app.closing:
            return
        self.app.show("local")
        self.app.local_converter.choose_files()

    def _build_downloads(self, page, card):
        app = self.app
        self.read_button = app.action(card, "读取视频", self.resolve, primary=True)
        self.read_button.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        self.entry.bind("<Return>", self.resolve)
        page.rowconfigure(2, weight=1)
        results = app.panel(page, padding=(12, 9))
        results.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(results, style="MusicToolbar.TFrame")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        toolbar.columnconfigure(0, weight=1)
        self.caption = tk.StringVar(page, "视频")
        ttk.Label(toolbar, textvariable=self.caption, style="MusicTitle.TLabel", width=1).grid(row=0, column=0, sticky="ew")
        self.queue_button = app.action(toolbar, "加入队列", lambda: self._enqueue(False))
        self.queue_button.grid(row=0, column=1, padx=6)
        self.download_button = app.action(toolbar, "下载", lambda: self._enqueue(True), primary=True)
        self.download_button.grid(row=0, column=2)
        self.tree = ttk.Treeview(results, columns=("title", "author"), show="headings", selectmode="extended",
                                 style="Queue.Treeview")
        self.tree.heading("title", text="标题", anchor="w")
        self.tree.heading("author", text="频道", anchor="w")
        self.tree.column("title", width=round(290 * app.scale), minwidth=60)
        self.tree.column("author", width=round(125 * app.scale), minwidth=45)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(results, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Control-a>", self._select_all)

    def _select_all(self, _event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _update_download_buttons(self):
        state = "normal" if self.rows else "disabled"
        self.queue_button.configure(state=state)
        self.download_button.configure(state=state)

    def resolve(self, _event=None):
        parsed = _parse_video_link(self.link.get())
        if self.busy or self.app.closing or not parsed or parsed[0] != self.source:
            return "break"
        self.serial += 1
        serial = self.serial
        self.stop = stop = threading.Event()
        self.busy = True
        self.read_button.configure(state="disabled")
        self.status.set("正在读取 YouTube…")
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self._update_download_buttons()

        def fetch():
            from youtube_download import resolve_youtube

            try:
                result, error = resolve_youtube(parsed[1], stop=stop), ""
            except (OSError, RuntimeError, ValueError) as problem:
                result, error = None, str(problem)
            self.app.post("youtube_result", (serial, result, error))

        threading.Thread(target=fetch, daemon=True).start()
        return "break"

    def receive(self, serial, result, error):
        if serial != self.serial or self.app.closing:
            return
        self.busy = False
        self.read_button.configure(state="normal")
        if error:
            self.status.set(error)
            return
        self.caption.set(result["title"])
        for item in result["rows"]:
            row = self.tree.insert("", "end", values=(item["title"], item.get("artist", "")))
            self.rows[row] = item
        self.status.set(result.get("warning") or f"{len(self.rows)} 项")
        self._update_download_buttons()

    def _enqueue(self, begin):
        selected = self.tree.selection() or self.tree.get_children()
        if selected:
            self.app.enqueue_tasks((self.rows[row] for row in selected), begin)
