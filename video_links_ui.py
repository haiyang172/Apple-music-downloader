"""Search YouTube and open YouTube or Douyin links."""

import re
from collections import deque
import threading
import tkinter as tk
from tkinter import ttk
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
import webbrowser

from share_links import links_in_text
from youtube_browse import YoutubeBrowser


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
        self.browser = None
        self.playlist = False
        self.resolved_value = ""
        self.total = None
        self.pending_pages = deque()
        self.pending_count = 0
        self.queue_pending = False
        self.view_timer = None
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
        ttk.Label(card, textvariable=self.status, style="MusicStatus.TLabel", width=1,
                  wraplength=round(425 * app.scale)).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
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
            self.read_button.configure(text="查找", state="normal" if recognized or query else "disabled")
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
        self.close()
        self.search_query = ""
        self.playlist = False
        self.resolved_value = ""
        self.total = None
        self.pending_pages.clear()
        self.pending_count = 0
        self.queue_pending = False
        self.next_offset = None
        self.page_error = False
        self.seen.clear()
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.retry_button.configure(text="重试", command=self._load_next)
        self.retry_button.grid_remove()

    def close(self):
        self.stop.set()
        if self.view_timer is not None:
            try:
                self.tree.after_cancel(self.view_timer)
            except tk.TclError:
                pass
            self.view_timer = None
        if self.browser is not None:
            self.browser.close()
            self.browser = None

    def _select_all(self, _event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _update_download_buttons(self, _event=None):
        selection = self.tree.selection()
        whole_playlist = self.playlist and not selection
        permitted = bool(self.rows or (whole_playlist and self.next_offset is not None))
        state = "normal" if permitted and not self.busy and (selection or not self.search_query) else "disabled"
        self.queue_button.configure(state=state, text="全部入队" if whole_playlist else "加入队列")
        download_text = "下载全部" if whole_playlist else "下载所选" if self.browser is not None else "下载"
        self.download_button.configure(state=state, text=download_text)
        if self.search_query:
            self.open_button.configure(state="normal" if len(selection) == 1 else "disabled")

    def resolve(self, _event=None):
        if self.app.closing:
            return "break"
        if self.busy:
            self.stop.set()
            self.status.set("正在停止…")
            return "break"
        value = self.link.get().strip()
        link = self._page_link(value)
        if link or self._is_search(value):
            self._clear_results()
            if not link:
                self.search_query = value
            else:
                from youtube_download import _parse
                self.playlist = _parse(link)[0] == "playlist"
            self.resolved_value = link or value
            if self.search_query or self.playlist:
                try:
                    self.browser = YoutubeBrowser(self.resolved_value, search=bool(self.search_query))
                except (OSError, RuntimeError, ValueError) as error:
                    self.status.set(str(error))
                    return "break"
                self.next_offset = 0
            self._read(link or value)
        return "break"

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if (float(last) >= 0.95 and not self.page_error and self.tree.winfo_viewable()
                and self.app.active_page == self.source):
            self._load_next()

    def _load_next(self):
        if self.busy or self.app.closing or self.browser is None or self.next_offset is None:
            return
        self.page_error = False
        self.retry_button.grid_remove()
        self._read(self.resolved_value, offset=self.next_offset)

    def _read(self, value, *, offset=0):
        self.retry_button.configure(text="重试", command=self._load_next)
        self.serial += 1
        serial = self.serial
        self.stop.set()
        self.stop = stop = threading.Event()
        self.busy = True
        self.read_button.configure(text="停止", state="normal")
        self.status.set("正在加载更多视频…" if offset else "正在查找 YouTube…")
        self._update_download_buttons()
        if self.pending_pages:
            result = self.pending_pages.popleft()
            self.pending_count -= len(result["rows"])
            self.receive(serial, result, "", offset)
            return
        browser = self.browser

        def fetch():
            from youtube_download import resolve_youtube

            try:
                if browser is not None:
                    result = browser.page(stop=stop)
                else:
                    result = resolve_youtube(value, stop=stop)
                error = ""
            except (OSError, RuntimeError, ValueError) as problem:
                result, error = None, str(problem)
            self.app.post("youtube_result", (serial, result, error, offset))

        threading.Thread(target=fetch, daemon=True).start()

    def receive(self, serial, result, error, offset=0):
        if serial != self.serial or self.app.closing:
            return
        self.busy = False
        self.read_button.configure(text="查找", state="normal")
        if error:
            self.status.set(error)
            if self.browser is not None and self.next_offset is not None:
                self.page_error = True
                self.retry_button.grid()
            self._update_download_buttons()
            return
        self.caption.set(result["title"])
        for item in result["rows"]:
            if self.search_query:
                url = item["url"]
                if url in self.seen:
                    continue
                self.seen.add(url)
            row = self.tree.insert("", "end", values=(item["title"], item.get("artist", "")))
            self.rows[row] = item
        cursor = result.get("next_offset")
        self.next_offset = cursor if self.browser is not None and type(cursor) is int and cursor > offset else None
        count = result.get("total")
        if type(count) is int and count >= 0:
            self.total = count
        if len(self.rows) == 1 and not offset and self.browser is None:
            self.tree.selection_set(next(iter(self.rows)))
        suffix = f" / {self.total}" if self.total is not None else ""
        status = f"已显示 {len(self.rows)}{suffix} 项" if self.rows or self.next_offset is not None else "没有找到视频。"
        warning = result.get("warning")
        self.status.set(status + (" · " + warning if warning else ""))
        self.page_error = bool(self.stop.is_set() and self.next_offset is not None)
        if self.page_error:
            self.status.set("已停止读取 · " + status)
            self.retry_button.configure(text="继续读取", command=self._load_next)
            self.retry_button.grid()
        self._update_download_buttons()
        if not self.page_error:
            if self.view_timer is not None:
                self.tree.after_cancel(self.view_timer)
            self.view_timer = self.tree.after_idle(self._fill_view)

    def _fill_view(self):
        self.view_timer = None
        if (not self.busy and not self.page_error and not self.app.closing and self.tree.winfo_viewable()
                and self.app.active_page == self.source and float(self.tree.yview()[1]) >= 0.95):
            self._load_next()

    def _enqueue(self, begin):
        if self.busy or self.app.closing:
            return
        selected = self.tree.selection()
        if self.playlist and not selected:
            self._enqueue_all(begin)
            return
        if not selected and not self.search_query:
            selected = self.tree.get_children()
        if selected:
            self.app.enqueue_tasks((self.rows[row] for row in selected), begin)

    def _enqueue_all(self, begin, *, captured_options=None):
        if self.browser is None or getattr(self.app, "queue_adding", False):
            self.status.set("请等当前列表入队完成。")
            return
        options = captured_options
        if begin and options is None:
            try:
                choices = self.app.snapshot()
                if not choices["folder"]:
                    self.app.browse_folder()
                    choices = self.app.snapshot()
                    if not choices["folder"]:
                        return
                from apple_download import download_options
                options = download_options(choices)
                self.app.store(choices)
            except (OSError, RuntimeError, ValueError) as error:
                self.app.error("请检查下载设置", error)
                return
        self.serial += 1
        serial = self.serial
        self.stop.set()
        self.stop = stop = threading.Event()
        browser = self.browser
        rows = list(self.rows.values())
        for page in self.pending_pages:
            rows.extend(page["rows"])
        cursor = self.pending_pages[-1].get("next_offset") if self.pending_pages else self.next_offset
        self.busy = True
        self.page_error = False
        self.retry_button.grid_remove()
        self.read_button.configure(text="停止", state="normal")
        self.status.set(f"正在读取整个播放列表 · 已读取 {len(rows)} 项")
        self._update_download_buttons()

        def fetch():
            error = ""
            try:
                nonlocal cursor
                while cursor is not None:
                    if stop.is_set():
                        raise RuntimeError("已停止读取整个播放列表。")
                    result = browser.page(stop=stop)
                    rows.extend(result["rows"])
                    cursor = result.get("next_offset")
                    self.app.post("youtube_batch", (serial, "page", result))
                if stop.is_set():
                    raise RuntimeError("已停止读取整个播放列表。")
            except (OSError, RuntimeError, ValueError) as problem:
                error = str(problem)
            self.app.post("youtube_batch", (serial, "done", (rows, error, begin, options)))

        threading.Thread(target=fetch, daemon=True).start()

    def receive_batch(self, serial, phase, payload):
        if serial != self.serial or self.app.closing:
            return
        if phase == "page":
            self.pending_pages.append(payload)
            self.pending_count += len(payload["rows"])
            if type(payload.get("total")) is int:
                self.total = payload["total"]
            self.status.set(f"正在读取整个播放列表 · 已读取 {len(self.rows) + self.pending_count} 项")
            return
        rows, error, begin, options = payload
        if error:
            self.busy = False
            self.read_button.configure(text="查找", state="normal")
            self.status.set(error + " · 尚未加入队列")
            self.page_error = self.next_offset is not None
            if self.page_error:
                self.retry_button.configure(text="重试整个列表",
                                            command=lambda: self._enqueue_all(begin, captured_options=options))
                self.retry_button.grid()
            self._update_download_buttons()
            return
        total = self.total
        if type(total) is int:
            rows = [dict(row, total=total) for row in rows]
        if options is not None:
            rows = [dict(row, options=options) for row in rows]
        self.status.set(f"正在加入 {len(rows)} 个任务…")
        self.queue_pending = True

        def done(count, failure, cancelled):
            if serial != self.serial or self.app.closing:
                return
            self.queue_pending = False
            self.busy = False
            self.read_button.configure(text="查找", state="normal")
            if failure:
                self.status.set(failure)
            elif cancelled and not count:
                self.status.set("已停止加入队列。")
            elif cancelled:
                self.status.set(f"已加入 {count} 项 · 已停止自动下载")
            else:
                self.status.set(f"已加入整个播放列表 · {count} 项")
            self._update_download_buttons()

        if not self.app.enqueue_tasks_async(rows, begin, stop=self.stop, on_done=done):
            done(0, "请等当前列表入队完成。", False)
