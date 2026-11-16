"""Search YouTube and open YouTube or Douyin links."""

import re
import threading
import tkinter as tk
from tkinter import ttk
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
import webbrowser

from share_links import links_in_text


SOURCES = {"youtube": "YouTube", "douyin": "抖音"}
_WEB_ACCOUNTS = {
    "youtube": (
        "https://www.youtube.com/",
        "打开 YouTube 网页",
        "在 YouTube 网页右上角选择「登录」，用 Google 账号继续。",
        "订阅、观看记录和私密播放列表在网页中访问。下载台可查找公开作品。",
    ),
    "douyin": (
        "https://www.douyin.com/",
        "打开抖音网页",
        "在抖音网页右上角选择「登录」，按网页提示扫码或验证。",
        "收藏和关注在网页中访问。通过抖音保存作品后，可在下载台导入影音文件。",
    ),
}
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
        self.search_query = ""
        self.next_offset = None
        self.page_error = False
        self.seen = set()
        self.stop = threading.Event()
        name = SOURCES[source]

        page.columnconfigure(0, weight=1)
        header = ttk.Frame(page, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        app.label(header, name, large=True, background=app.background).grid(row=0, column=0, sticky="w")
        if source == "youtube":
            self.format = ttk.Combobox(header, textvariable=app.youtube_format, values=tuple(formats), width=15,
                                       state="readonly", style="Surface.TCombobox")
            self.format.grid(row=0, column=1, padx=(8, 0))
            self.format.bind("<<ComboboxSelected>>", app.quality_changed)
        self.account_button = app.action(header, "账户", lambda: app.show(f"{source}_account"))
        self.account_button.grid(row=0, column=2, padx=(8, 0))
        card = app.panel(page, padding=(round(16 * app.scale), round(14 * app.scale)))
        card.grid(row=1, column=0, sticky="ew", pady=(round(12 * app.scale), 0))
        card.columnconfigure(0, weight=1)
        description = "输入关键词，或粘贴视频、Shorts 或播放列表链接" if source == "youtube" else "视频、图文或抖音短链"
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

    def build_account_page(self, page):
        app, source = self.app, self.source
        _, button_text, steps, features = _WEB_ACCOUNTS[source]
        name = SOURCES[source]
        page.columnconfigure(0, weight=1)
        header = ttk.Frame(page, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        app.label(header, f"{name} 账户", large=True, background=app.background).grid(row=0, column=0, sticky="w")
        self.account_back_button = app.action(header, f"返回{name}", lambda: app.show(source))
        self.account_back_button.grid(row=0, column=1, padx=(8, 0))

        card = app.panel(page, padding=(round(16 * app.scale), round(14 * app.scale)))
        card.grid(row=1, column=0, sticky="ew", pady=(round(12 * app.scale), 0))
        card.columnconfigure(0, weight=1)
        wrap = round(425 * app.scale)
        app.label(card, steps, wrap=wrap).grid(row=0, column=0, sticky="w")
        app.label(card, features, muted=True, wrap=wrap).grid(row=1, column=0, sticky="w", pady=(5, 12))
        self.browser_account_button = app.action(card, button_text, self.open_account, primary=True)
        self.browser_account_button.grid(row=2, column=0, sticky="w")
        self.account_status = tk.StringVar(page)
        ttk.Label(card, textvariable=self.account_status, style="MusicStatus.TLabel").grid(
            row=3, column=0, sticky="w", pady=(7, 0)
        )

    def open_account(self):
        self._open_in_browser(_WEB_ACCOUNTS[self.source][0], self.account_status, "请在浏览器完成登录。")

    def _update_link(self, *_):
        value = self.link.get().strip()
        recognized = self._page_link(value) is not None
        self.open_button.configure(state="normal" if recognized else "disabled")
        query = self.source == "youtube" and not recognized and self._is_search(value)
        invalid = value and not (recognized or query)
        self.status.set(f"请输入有效的 {SOURCES[self.source]} 链接。" if invalid else "")
        if self.source == "youtube":
            self.stop.set()
            self.serial += 1
            self.busy = False
            self._clear_results()
            self.caption.set("视频")
            self.read_button.configure(state="normal" if recognized or query else "disabled")
            self._update_download_buttons()

    @staticmethod
    def _is_search(value):
        return bool(value and "://" not in value and not value.startswith("www."))

    def _page_link(self, value):
        links = links_in_text(value, lambda link: video_link_source(link) == self.source)
        return _parse_video_link(links[0])[1] if len(links) == 1 else None

    def load_link(self, value):
        self.link.set(value)

    def open_original(self, _event=None):
        url = self._page_link(self.link.get())
        if self.source == "youtube" and self.search_query:
            selection = self.tree.selection()
            item = self.rows.get(selection[0]) if len(selection) == 1 else None
            if item:
                result = _parse_video_link(item["url"])
                url = result[1] if result and result[0] == "youtube" else ""
        success = "保存后可导入文件" if self.source == "douyin" else "已在浏览器打开"
        self._open_in_browser(url, self.status, success)
        return "break"

    def _open_in_browser(self, url, status, success):
        if not self.app.closing and url:
            try:
                opened = webbrowser.open(url)
            except (OSError, webbrowser.Error):
                opened = False
            status.set(success if opened else "无法打开默认浏览器。")

    def import_saved(self):
        if self.app.closing:
            return
        self.app.show("local")
        self.app.local_converter.choose_files()

    def _build_downloads(self, page, card):
        app = self.app
        self.read_button = app.action(card, "查找", self.resolve, primary=True)
        self.read_button.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        self.retry_button = app.action(card, "重试", self._load_next)
        self.retry_button.grid(row=2, column=2, padx=(6, 0), pady=(8, 0))
        self.retry_button.grid_remove()
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
        self.scrollbar = ttk.Scrollbar(results, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        self.scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self._scroll_changed)
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<<TreeviewSelect>>", self._update_download_buttons)

    def _clear_results(self):
        self.search_query = ""
        self.next_offset = None
        self.page_error = False
        self.seen.clear()
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.retry_button.grid_remove()

    def _select_all(self, _event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _update_download_buttons(self, _event=None):
        selection = self.tree.selection()
        state = "normal" if self.rows and (selection or not self.search_query) else "disabled"
        self.queue_button.configure(state=state)
        self.download_button.configure(state=state)
        if self.search_query:
            self.open_button.configure(state="normal" if len(selection) == 1 else "disabled")

    def resolve(self, _event=None):
        if self.busy or self.app.closing:
            return "break"
        value = self.link.get().strip()
        link = self._page_link(value)
        if link or self._is_search(value):
            self._clear_results()
            if not link:
                self.search_query = value
            self._read(link or value)
        return "break"

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if (float(last) >= 0.95 and not self.page_error and self.tree.winfo_viewable()
                and self.app.active_page == self.source):
            self._load_next()

    def _load_next(self):
        if self.busy or self.app.closing or not self.search_query or self.next_offset is None:
            return
        self.page_error = False
        self.retry_button.grid_remove()
        self._read(self.search_query, offset=self.next_offset)

    def _read(self, value, *, offset=0):
        search = bool(self.search_query)
        self.serial += 1
        serial = self.serial
        self.stop.set()
        self.stop = stop = threading.Event()
        self.busy = True
        self.read_button.configure(state="disabled")
        self.status.set("正在加载更多视频…" if offset else "正在查找 YouTube…")
        self._update_download_buttons()

        def fetch():
            from youtube_download import resolve_youtube, search_youtube

            try:
                if search:
                    result = search_youtube(value, stop=stop, offset=offset)
                else:
                    result = resolve_youtube(value, stop=stop)
                error = ""
            except (OSError, RuntimeError, ValueError) as problem:
                result, error = None, str(problem)
            if not stop.is_set():
                self.app.post("youtube_result", (serial, result, error, offset))

        threading.Thread(target=fetch, daemon=True).start()

    def receive(self, serial, result, error, offset=0):
        if serial != self.serial or self.app.closing:
            return
        self.busy = False
        self.read_button.configure(state="normal")
        if error:
            self.status.set(error)
            if offset:
                self.page_error = True
                self.retry_button.grid()
            return
        self.caption.set(result["title"])
        previous = len(self.rows)
        for item in result["rows"]:
            if self.search_query:
                url = item["url"]
                if url in self.seen:
                    continue
                self.seen.add(url)
            row = self.tree.insert("", "end", values=(item["title"], item.get("artist", "")))
            self.rows[row] = item
        cursor = result.get("next_offset")
        progressed = type(cursor) is int and cursor > offset and len(self.rows) > previous
        self.next_offset = cursor if self.search_query and progressed else None
        if len(self.rows) == 1 and not offset:
            self.tree.selection_set(next(iter(self.rows)))
        status = f"已显示 {len(self.rows)} 项" if self.rows else "没有找到视频。"
        self.status.set(result.get("warning") or status)
        self._update_download_buttons()

    def _enqueue(self, begin):
        selected = self.tree.selection()
        if not selected and not self.search_query:
            selected = self.tree.get_children()
        if selected:
            self.app.enqueue_tasks((self.rows[row] for row in selected), begin)
