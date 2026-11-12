"""amdl desktop application."""

import ctypes
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
from urllib.parse import parse_qs, urlsplit
import webbrowser
import winsound


ROOT = Path(__file__).resolve().parent
APP_TITLE = "amdl"
for library in (ROOT.parent / "playlist-download" / "python-deps", ROOT / "lib"):
    if library.is_dir():
        sys.path.insert(0, str(library))

import audio
from engine import Cancelled, Engine
from login_service import LoginService
from music_browser import MusicBrowser, prepare_preview
from music_catalog import CatalogClient
import settings
from storage import destination_for, publish, volume_identity


QUALITY = {"AAC": "AAC-LC · 兼容", "ALAC": "ALAC · 无损", "ATMOS": "Dolby Atmos"}
PAGES = {"music": "音乐", "library": "资料库", "download": "下载队列",
         "account": "账户", "settings": "设置", "components": "组件"}
BG = "#edf3f2"
PANEL = "#ffffff"
INK = "#152d32"
MUTED = "#486367"
NAV = "#102b31"
NAV_ACTIVE = "#24524f"
ACCENT = "#11715e"
BORDER = "#c9d9d5"


def backend_locations():
    installed = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "apmyx"
    return ROOT / "backend", installed, ROOT.parent / "GUI-downloader"


def find_backend():
    bridge = Path("_internal/backend/bin/bridge/bridge.exe")
    for folder in backend_locations():
        if (folder / bridge).is_file() and (folder / "tools/ffmpeg.exe").is_file():
            return folder
    raise RuntimeError("找不到下载引擎或 FFmpeg。请运行 amdl 安装包。")


def configure_windows():
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = (ctypes.c_wchar_p,)
        set_app_id("amdl.desktop")
    except (AttributeError, OSError):
        pass


def read_links(text):
    result = []
    for row, raw in enumerate(text.splitlines(), 1):
        link = raw.strip()
        if not link:
            continue
        try:
            parsed = urlsplit(link)
            parts = parsed.path.strip("/").split("/")
            if parts and re.fullmatch(r"[a-z]{2}", parts[0], re.I):
                parts = parts[1:]
            valid = (
                parsed.scheme == "https" and parsed.netloc.lower() == "music.apple.com"
                and len(parts) > 1 and parts[0] in ("song", "album", "playlist")
                and not any(char.isspace() or ord(char) < 32 for char in link)
            )
            if not valid:
                raise ValueError()
            track = parse_qs(parsed.query, keep_blank_values=True).get("i")
            if parts[0] == "album" and track:
                if len(track) != 1 or not re.fullmatch(r"[0-9]+", track[0]):
                    raise ValueError()
                parsed = parsed._replace(query=f"i={track[0]}")
            result.append(parsed._replace(fragment="").geturl())
        except ValueError:
            raise ValueError(f"第 {row} 行不是有效的 Apple Music 歌曲、专辑或歌单链接。") from None
    if not result:
        raise ValueError("请至少粘贴一个 Apple Music 链接。")
    return list(dict.fromkeys(result))


def run_download(job, stop, post, engine=None, login_service=None):
    paths = destination_for(job["folder"], job["category"])
    disk = volume_identity(Path(job["folder"]))
    engine = engine or Engine(ROOT, find_backend())
    quality = job["quality"]
    if job.get("native_account"):
        login_service = login_service or LoginService(ROOT, settings.DATA)
        login_service.start(stop, lambda status: post("status", status))
        if not login_service.status()["logged_in"]:
            raise ValueError("Apple Music 登录已过期。请在「账户」重新登录。")
        config = login_service.engine_config()
        region = config.get("applemusicdecrypt-storefront") or job["region"]
        config["storefront"] = region
        config["applemusicdecrypt-storefront"] = region
    else:
        config = {"media-user-token": settings.session(job), "storefront": job["region"]}
        if not config["media-user-token"]:
            raise ValueError("请先在「账户」登录 Apple Music。")
        if quality in ("ALAC", "ATMOS"):
            engine.prepare_service(job["wrapper"], stop, lambda status: post("status", status))

    saved = existing = errors = 0
    for index, url in enumerate(job["links"], 1):
        if stop.is_set():
            break
        before = saved + existing
        errors_before = errors
        prefix = f"链接 {index}/{len(job['links'])}"
        post("status", prefix + " · 正在读取曲目")
        post("queue_status", (url, "下载中"))

        def on_event(event):
            if event.get("event") == "progress":
                data = event.get("data", {})
                label = data.get("name") or data.get("phase")
                if label:
                    post("status", prefix + " · " + str(label)[:75])
            elif event.get("event") == "warning":
                post("log", str(event.get("message", "")))

        with tempfile.TemporaryDirectory(prefix="amdl-") as working:
            stage = Path(working)
            try:
                result = engine.download(config, url, quality, stage, stop, on_event)
            except Cancelled:
                post("queue_status", (url, "已取消"))
                break
            except (OSError, RuntimeError, ValueError) as error:
                errors += 1
                post("log", f"{prefix} · {error}")
                post("queue_status", (url, "失败"))
                continue
            errors += result["errors"]
            for artifact in result["artifacts"]:
                if stop.is_set():
                    break
                try:
                    post("status", prefix + " · 正在检查并保存音频")
                    source, stem = audio.prepare(engine.ffmpeg, stage, artifact, quality,
                                                 job["output"], job["mp3_bitrate"], stop)
                    target, written = publish(source, paths, stem, artifact.get("track_id"),
                                              expected_volume=disk)
                    saved += int(written)
                    existing += int(not written)
                    post("log", ("已保存  " if written else "已存在  ") + target.name)
                except (Cancelled, audio.Cancelled):
                    break
                except (OSError, RuntimeError, ValueError) as error:
                    errors += 1
                    post("log", f"曲目 {artifact.get('track_id') or '?'} · {error}")
        if stop.is_set():
            post("queue_status", (url, "已取消"))
        elif saved + existing == before:
            if errors == errors_before:
                errors += 1
            post("queue_status", (url, "失败"))
        else:
            post("queue_status", (url, "部分完成" if errors > errors_before else "已完成"))
    return saved, existing, errors


class Studio:
    def __init__(self, window):
        self.window = window
        self.stop = threading.Event()
        self.service_stop = threading.Event()
        self.auth_stop = threading.Event()
        self.messages = queue.SimpleQueue()
        self.engine = None
        self.login_service = LoginService(ROOT, settings.DATA)
        self.catalog = CatalogClient()
        self.background = BG
        self.queue_rows = {}
        self.queue_items = {}
        self.queue_states = {}
        self.active_page = "music"
        self.preview_serial = 0
        self.preview_owner = None
        self.preview_temp = None
        self.preview_timer = None
        self.busy = False
        self.closing = False
        self.service_busy = False
        self.auth_busy = False
        self.pending_region = None
        try:
            self.preferences = settings.load()
            load_error = ""
        except ValueError as error:
            self.preferences = dict(settings.DEFAULTS)
            load_error = str(error)
        self.fields = {
            key: (tk.BooleanVar(window, value) if isinstance(value, bool)
                  else tk.StringVar(window, value=str(value)))
            for key, value in self.preferences.items() if key in settings.DEFAULTS
        }
        if self.fields["quality"].get() not in QUALITY:
            self.fields["quality"].set("AAC")
        if self.fields["output"].get() not in audio.OUTPUTS:
            self.fields["output"].set("original")
        self.quality_label = tk.StringVar(window, QUALITY[self.fields["quality"].get()])
        self.output_label = tk.StringVar(window, audio.OUTPUTS[self.fields["output"].get()])
        self.account_status = tk.StringVar(window)
        self.path_summary = tk.StringVar(window)
        self.status = tk.StringVar(window, load_error or "就绪")
        self.engine_status = tk.StringVar(window)
        self.service_status = tk.StringVar(window, "按下载音质启动")
        self.token = tk.StringVar(window)
        self.username = tk.StringVar(window)
        self.password = tk.StringVar(window)
        self.auth_code = tk.StringVar(window)
        self.auth_status = tk.StringVar(window)
        self.native_status = None
        self.pages = {}
        self.navigation = {}

        window.title(APP_TITLE)
        self.scale = max(1, window.winfo_fpixels("1i") / 96)
        width = min(round(960 * self.scale), window.winfo_screenwidth() - 42)
        height = min(round(670 * self.scale), window.winfo_screenheight() - 100)
        window.geometry(f"{width}x{height}")
        window.minsize(min(round(790 * self.scale), width), min(round(590 * self.scale), height))
        icon = ROOT / "assets" / "AMDLStudio.ico"
        if icon.is_file():
            window.iconbitmap(str(icon))
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.rowconfigure(0, weight=1)
        window.columnconfigure(1, weight=1)

        installed_fonts = set(font.families(window))
        self.font = "Inter" if "Inter" in installed_fonts else "Segoe UI"
        self.configure_style()

        rail = tk.Frame(window, background=NAV, width=round(148 * self.scale))
        rail.grid(row=0, column=0, sticky="ns")
        rail.grid_propagate(False)
        rail.columnconfigure(0, weight=1)
        rail.rowconfigure(7, weight=1)
        mark = ROOT / "assets" / "amdl-32.png"
        self.brand_icon = tk.PhotoImage(file=mark) if mark.is_file() else None
        tk.Label(rail, text=APP_TITLE, image=self.brand_icon, compound="left", padx=4,
                 font=(self.font, 19, "bold"), background=NAV, foreground="white").grid(
            row=0, column=0, padx=13, pady=(24, 23), sticky="w")
        for row, (key, label) in enumerate(PAGES.items(), 1):
            button = tk.Button(rail, text=label, command=lambda page=key: self.show(page),
                               background=NAV, activebackground=NAV_ACTIVE, foreground="white",
                               activeforeground="white", relief="flat", anchor="w",
                               font=(self.font, 11), padx=14, pady=8, cursor="hand2")
            button.grid(row=row, column=0, sticky="ew", padx=9, pady=2)
            self.navigation[key] = button

        stage = tk.Frame(window, bg=BG)
        stage.grid(row=0, column=1, sticky="nsew")
        stage.columnconfigure(0, weight=1)
        stage.rowconfigure(0, weight=1)
        for key in PAGES:
            page = tk.Frame(stage, background=BG, padx=24, pady=19)
            page.grid(row=0, column=0, sticky="nsew")
            page.columnconfigure(0, weight=1)
            self.pages[key] = page

        self.music_browser = MusicBrowser(self, self.pages["music"])
        self.library_browser = MusicBrowser(self, self.pages["library"], private=True)
        self.make_download()
        self.make_account()
        self.make_settings()
        self.make_components()
        self.refresh_account()
        self.refresh_engine()
        for key in ("folder", "category"):
            self.fields[key].trace_add("write", self.update_path)
        self.update_path()
        self.show("music")
        self.pump_timer = window.after(80, self.pump)
        self.start_timer = None
        if self.fields["auto_service"].get() and (
            self.fields["native_account"].get() or self.fields["quality"].get() in ("ALAC", "ATMOS")
        ):
            self.start_timer = window.after(250, self.start_service)

    def configure_style(self):
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure(".", font=(self.font, 10), foreground=INK)
        style.configure("TButton", background=PANEL, foreground=INK, bordercolor=BORDER,
                        focuscolor=ACCENT, padding=(10, 6))
        style.map("TButton", background=[("active", "#e3efeb")], foreground=[("disabled", "#899c98")])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", bordercolor=ACCENT)
        style.map("Primary.TButton", background=[("disabled", "#c9d9d5"), ("active", "#0d594c")],
                  foreground=[("disabled", "#617b73")])
        style.configure("TCheckbutton", background=PANEL, foreground=INK)
        style.configure("Surface.TFrame", background=BG)
        style.configure("Selected.TButton", background="#e0f0e9", foreground=ACCENT, bordercolor="#95bfb1")
        style.configure("TEntry", padding=4, fieldbackground=PANEL)
        style.configure("MusicSearch.TEntry", padding=(11, 8), fieldbackground=PANEL, bordercolor=BORDER)
        style.configure("TCombobox", padding=4, fieldbackground=PANEL, background=PANEL)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], selectforeground=[("readonly", INK)],
                  selectbackground=[("readonly", PANEL)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#dce8e3",
                        lightcolor=ACCENT, darkcolor=ACCENT, bordercolor="#dce8e3")
        style.configure("Music.Treeview", rowheight=max(52, round(55 * self.scale)), font=(self.font, 11),
                        fieldbackground=PANEL, background=PANEL, bordercolor=BORDER, foreground=INK)
        style.configure("Music.Treeview.Heading", padding=(6, 7), font=(self.font, 10, "bold"),
                        background="#e3ede8", foreground=INK)
        style.map("Music.Treeview", background=[("selected", "#cce7db")], foreground=[("selected", INK)])
        style.configure("Queue.Treeview", rowheight=round(38 * self.scale), font=(self.font, 11),
                        fieldbackground=PANEL, background=PANEL, bordercolor=BORDER, foreground=INK)
        style.configure("Queue.Treeview.Heading", padding=(6, 7), font=(self.font, 10, "bold"),
                        background="#e3ede8", foreground=INK)
        style.map("Queue.Treeview", background=[("selected", "#cce7db")], foreground=[("selected", INK)])

    def label(self, parent, text="", *, variable=None, large=False, muted=False, background=PANEL, wrap=615):
        options = {"textvariable": variable} if variable is not None else {"text": text}
        return tk.Label(parent, **options, background=background, foreground=MUTED if muted else INK,
                        justify="left", anchor="w", wraplength=wrap,
                        font=(self.font, 22, "bold") if large else (self.font, 10))

    def header(self, page, title, detail):
        self.label(page, title, large=True, background=BG).grid(row=0, column=0, sticky="w")
        self.label(page, detail, muted=True, background=BG).grid(row=1, column=0, sticky="w", pady=(4, 13))

    def card(self, page, row):
        card = tk.Frame(page, bg=PANEL, padx=14, pady=12, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=row, column=0, sticky="nsew", pady=(0, 9))
        return card

    def action(self, parent, text, command, primary=False):
        return ttk.Button(parent, text=text, command=command, style="Primary.TButton" if primary else "TButton")

    def textbox(self, parent, height, **options):
        return tk.Text(parent, height=height, wrap="word", padx=8, pady=5, borderwidth=0,
                       highlightbackground=BORDER, highlightcolor=ACCENT, highlightthickness=1,
                       bg="#f6faf8", fg=INK, insertbackground=INK, font=(self.font, 10), **options)

    def make_download(self):
        page = self.pages["download"]
        page.rowconfigure(2, weight=1)
        title = ttk.Frame(page, style="Surface.TFrame")
        title.grid(row=0, column=0, sticky="ew")
        title.columnconfigure(0, weight=1)
        self.label(title, "下载队列", large=True, background=BG).grid(row=0, column=0, sticky="w")
        self.action(title, "添加链接", self.link_dialog).grid(row=0, column=1, padx=(8, 0))
        self.remove_button = self.action(title, "移除", self.remove_queued)
        self.remove_button.grid(row=0, column=2, padx=(8, 0))

        options = ttk.Frame(page, style="Surface.TFrame")
        options.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        options.columnconfigure(1, weight=1)
        self.quality_box = ttk.Combobox(options, textvariable=self.quality_label,
                                        values=list(QUALITY.values()), state="readonly", width=17)
        self.quality_box.grid(row=0, column=0, sticky="w")
        self.quality_box.bind("<<ComboboxSelected>>", self.quality_changed)
        self.label(options, variable=self.path_summary, background=BG, muted=True, wrap=390).grid(
            row=0, column=1, sticky="w", padx=10)
        self.action(options, "保存目录", self.browse_folder).grid(row=0, column=2)
        self.action(options, "打开", self.open_folder).grid(row=0, column=3, padx=(6, 0))

        results = ttk.Frame(page, style="Surface.TFrame")
        results.grid(row=2, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self.queue_tree = ttk.Treeview(results, columns=("title", "artist", "state"),
                                       show="headings", selectmode="extended", style="Queue.Treeview")
        for name, label, width, minimum in (
            ("title", "名称", 360, 140), ("artist", "艺人", 160, 85), ("state", "状态", 90, 75),
        ):
            self.queue_tree.heading(name, text=label)
            self.queue_tree.column(name, width=width, minwidth=minimum, stretch=name != "state")
        self.queue_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(results, command=self.queue_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.queue_tree.configure(yscrollcommand=scroll.set)
        self.queue_tree.bind("<Delete>", lambda _: self.remove_queued())

        buttons = ttk.Frame(page, style="Surface.TFrame")
        buttons.grid(row=3, column=0, sticky="ew", pady=(9, 7))
        self.start_button = self.action(buttons, "开始下载", self.start_download, primary=True)
        self.start_button.pack(side="left")
        self.cancel_button = self.action(buttons, "取消", self.cancel)
        self.cancel_button.pack(side="left", padx=7)
        self.cancel_button.configure(state="disabled")
        self.action(buttons, "下载记录", self.history_dialog).pack(side="right")
        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.grid(row=4, column=0, sticky="ew")
        self.label(page, variable=self.status, background=BG, muted=True).grid(
            row=5, column=0, sticky="w", pady=(4, 0))
        self.history = []
        self.history_text = None
        self.history_window = None

    def queue_music(self, items, begin=False):
        accepted = {}
        for item in items:
            try:
                url = read_links(item.get("url", ""))[0]
            except ValueError:
                continue
            accepted[url] = None
            if url in self.queue_rows:
                if self.queue_states[url] != "下载中":
                    self.set_queue_state(url, "待下载")
                continue
            name = item.get("title") or url
            artist = item.get("artist") or ""
            row = self.queue_tree.insert("", "end", values=(name, artist, "待下载"))
            self.queue_items[row] = url
            self.queue_rows[url] = row
            self.queue_states[url] = "待下载"
        self.update_queue_count()
        if accepted and begin:
            self.show("download")
            if not self.busy:
                self.start_download(list(accepted))
        return len(accepted)

    def set_queue_state(self, url, state):
        if url in self.queue_rows:
            self.queue_states[url] = state
            self.queue_tree.set(self.queue_rows[url], "state", state)
            self.update_queue_count()

    def update_queue_count(self):
        pending = sum(state in ("待下载", "下载中") for state in self.queue_states.values())
        self.navigation["download"].configure(text=f"下载队列  {pending}" if pending else "下载队列")

    def remove_queued(self):
        if self.busy:
            return
        for row in self.queue_tree.selection():
            url = self.queue_items.pop(row)
            self.queue_rows.pop(url)
            self.queue_states.pop(url)
            self.queue_tree.delete(row)
        self.update_queue_count()

    def add_link_text(self, text, begin=False):
        try:
            links = read_links(text)
        except ValueError as error:
            self.error("添加链接", error)
            return False
        rows = []
        for url in links:
            parts = urlsplit(url).path.strip("/").split("/")
            title = parts[-2].replace("-", " ") if len(parts) > 3 else url
            rows.append({"url": url, "title": title})
        self.queue_music(rows, begin=begin)
        if not begin:
            self.show("download")
        return True

    def link_dialog(self):
        popup = tk.Toplevel(self.window)
        popup.title("添加链接")
        popup.transient(self.window)
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(0, weight=1)
        entry = self.textbox(popup, 5, undo=True, width=58)
        entry.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)

        def accept():
            if self.add_link_text(entry.get("1.0", "end")):
                popup.destroy()

        self.action(popup, "加入队列", accept, primary=True).grid(row=1, column=0, sticky="e", padx=14, pady=(0, 12))
        popup.bind("<Escape>", lambda _: popup.destroy())
        popup.grab_set()
        entry.focus_set()

    def history_dialog(self):
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window.lift()
            return
        popup = tk.Toplevel(self.window)
        popup.title("下载记录")
        popup.transient(self.window)
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(0, weight=1)
        history = self.textbox(popup, 12, width=65)
        history.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        history.insert("1.0", "\n".join(self.history))
        history.configure(state="disabled")
        self.history_text = history
        self.history_window = popup

    def make_account(self):
        page = self.pages["account"]
        self.header(page, "Apple Music 账户", "登录成功后会记住会话，下次打开 amdl 会自动恢复。")
        card = self.card(page, 2)
        card.columnconfigure(1, weight=1)
        self.label(card, variable=self.account_status).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self.label(card, "Apple 账号").grid(row=1, column=0, sticky="w")
        self.user_entry = ttk.Entry(card, textvariable=self.username)
        self.user_entry.grid(row=1, column=1, sticky="ew", padx=(12, 0))
        self.label(card, "密码").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.password_entry = ttk.Entry(card, textvariable=self.password, show="•")
        self.password_entry.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=(8, 0))
        self.label(card, "账号地区").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(card, textvariable=self.fields["region"], width=7).grid(
            row=3, column=1, sticky="w", padx=12, pady=(8, 0))
        self.code_label = self.label(card, "验证码")
        self.code_label.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.code_entry = ttk.Entry(card, textvariable=self.auth_code, width=12)
        self.code_entry.grid(row=4, column=1, sticky="w", padx=12, pady=(8, 0))
        self.code_label.grid_remove()
        self.code_entry.grid_remove()
        actions = tk.Frame(card, bg=PANEL)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.login_button = self.action(actions, "登录", self.login_account, primary=True)
        self.login_button.pack(side="left")
        self.cancel_auth_button = self.action(actions, "取消验证", self.reset_login)
        self.logout_button = self.action(actions, "退出登录", self.logout_account)
        self.logout_button.pack(side="right")
        self.label(card, variable=self.auth_status, muted=True).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(7, 0))

        self.legacy_card = self.card(page, 3)
        self.legacy_card.columnconfigure(1, weight=1)
        self.label(self.legacy_card, "会话令牌").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.legacy_card, textvariable=self.token, show="•").grid(
            row=0, column=1, sticky="ew", padx=(12, 0))
        legacy_actions = tk.Frame(self.legacy_card, bg=PANEL)
        legacy_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.action(legacy_actions, "保存会话", self.save_account).pack(side="left")
        self.action(legacy_actions, "导入 apmyx", self.import_account).pack(side="left", padx=7)
        self.legacy_card.grid_remove()
        self.legacy_toggle = self.action(page, "兼容会话设置", self.toggle_legacy)
        self.legacy_toggle.grid(row=4, column=0, sticky="w")

    def make_settings(self):
        page = self.pages["settings"]
        self.header(page, "保存与转码", "选择下载目录、可选分类，以及 FFmpeg 输出格式。")
        card = self.card(page, 2)
        card.columnconfigure(1, weight=1)
        self.label(card, "保存目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(card, textvariable=self.fields["folder"]).grid(row=0, column=1, sticky="ew", padx=10)
        self.action(card, "浏览", self.browse_folder).grid(row=0, column=2)
        self.label(card, "分类文件夹").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(card, textvariable=self.fields["category"]).grid(row=1, column=1, sticky="ew", padx=10, pady=(8, 0))
        self.label(card, "可留空", muted=True).grid(row=1, column=2, pady=(8, 0))
        self.label(card, "输出格式").grid(row=2, column=0, sticky="w", pady=(8, 0))
        output = ttk.Combobox(card, values=list(audio.OUTPUTS.values()), textvariable=self.output_label,
                              state="readonly", width=22)
        output.grid(row=2, column=1, sticky="w", padx=10, pady=(8, 0))
        self.label(card, "MP3 码率").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(card, values=audio.MP3_BITRATES, textvariable=self.fields["mp3_bitrate"],
                     width=9, state="readonly").grid(row=3, column=1, sticky="w", padx=10, pady=(8, 0))
        self.label(card, "有损源转换为 FLAC 后，源音质保持原有水平。", muted=True).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 8))
        self.action(card, "保存设置", self.save_preferences, primary=True).grid(row=5, column=0, sticky="w")

    def make_components(self):
        page = self.pages["components"]
        self.header(page, "下载组件", "查看登录服务、下载引擎和 FFmpeg 的状态。")
        card = self.card(page, 2)
        card.columnconfigure(1, weight=1)
        self.label(card, variable=self.engine_status, wrap=545).grid(row=0, column=0, columnspan=2, sticky="w")
        self.action(card, "重新检查", self.refresh_engine).grid(row=0, column=2, sticky="e")
        self.label(card, "兼容服务").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(card, textvariable=self.fields["wrapper"]).grid(row=1, column=1, sticky="ew", padx=10, pady=(10, 0))
        self.action(card, "浏览", self.browse_wrapper).grid(row=1, column=2, pady=(10, 0))
        self.label(card, "用于旧会话模式。留空时查找本机已有的 Ubuntu-AMDL。", muted=True).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=10, pady=(4, 5))
        ttk.Checkbutton(card, text="打开 amdl 时恢复登录服务",
                        variable=self.fields["auto_service"]).grid(row=3, column=0, columnspan=3, sticky="w")
        self.label(card, variable=self.service_status, muted=True).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(7, 6))
        buttons = tk.Frame(card, bg=PANEL)
        buttons.grid(row=5, column=0, columnspan=3, sticky="w")
        self.action(buttons, "保存设置", self.save_preferences, primary=True).pack(side="left")
        self.action(buttons, "启动服务", self.start_service).pack(side="left", padx=7)

    def show(self, page):
        if page != self.active_page and self.preview_owner is not None:
            self.stop_preview()
        self.active_page = page
        self.pages[page].tkraise()
        for key, button in self.navigation.items():
            button.configure(background=NAV_ACTIVE if key == page else NAV)
        if page == "music":
            self.music_browser.ensure_loaded()
        elif page == "library":
            self.library_browser.ensure_loaded()

    def catalog_region(self):
        region = self.fields["region"].get().strip().lower()
        return region if re.fullmatch(r"[a-z]{2}", region) else "au"

    def open_music_link(self, url):
        try:
            webbrowser.open(read_links(url)[0])
        except ValueError:
            return

    def preview_music(self, browser, item):
        self.stop_preview()
        url = item.get("preview_url")
        if not url:
            browser.status.set("暂无试听。")
            return
        try:
            ffmpeg = find_backend() / "tools" / "ffmpeg.exe"
        except RuntimeError as error:
            browser.status.set(str(error))
            return
        self.preview_owner = browser
        browser.status.set("正在加载试听…")
        browser.stop_preview.pack(side="right", padx=(0, 6))
        serial = self.preview_serial

        def fetch():
            scratch = None
            try:
                scratch = tempfile.TemporaryDirectory(prefix="amdl-preview-")
                audio_path = prepare_preview(url, ffmpeg, scratch.name)
                if serial == self.preview_serial and not self.closing:
                    self.post("preview", (serial, browser, item["title"], scratch, audio_path, ""))
                    scratch = None
            except (OSError, RuntimeError, ValueError) as error:
                self.post("preview", (serial, browser, "", None, None, str(error)))
            finally:
                if scratch is not None:
                    scratch.cleanup()

        threading.Thread(target=fetch, daemon=True).start()

    def play_preview(self, serial, browser, title, scratch, path, error):
        if serial != self.preview_serial or self.closing:
            if scratch is not None:
                scratch.cleanup()
            return
        if error:
            self.stop_preview()
            browser.status.set(error)
            return
        self.preview_temp = scratch
        try:
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            browser.status.set(f"试听 · {title}")
            self.preview_timer = self.window.after(36000, self.stop_preview)
        except RuntimeError:
            self.stop_preview()
            browser.status.set("无法播放试听。")

    def stop_preview(self):
        self.preview_serial += 1
        if self.preview_timer is not None:
            self.window.after_cancel(self.preview_timer)
            self.preview_timer = None
        if self.preview_temp is not None:
            try:
                winsound.PlaySound(None, 0)
                self.preview_temp.cleanup()
            except (OSError, RuntimeError):
                pass
            self.preview_temp = None
        if self.preview_owner is not None:
            browser = self.preview_owner
            browser.stop_preview.pack_forget()
            if browser.status.get().startswith(("试听", "正在加载试听")):
                browser.status.set(f"{len(browser.rows)} 项" if browser.rows else "")
            self.preview_owner = None

    def library_token(self):
        return settings.session(self.preferences)

    def saved_apmyx_session(self):
        for folder in backend_locations()[1:]:
            if (folder / "config.yaml").is_file():
                value = Engine(ROOT, folder).read_config()
                if value.get("media-user-token"):
                    return value
        raise ValueError("本机 apmyx 配置中没有可导入的会话。")

    def connect_library(self):
        dialog = tk.Toplevel(self.window)
        dialog.title("连接资料库")
        dialog.transient(self.window)
        dialog.columnconfigure(0, weight=1)
        token = tk.StringVar(dialog)
        self.label(dialog, "Apple Music 网页会话令牌", background=dialog.cget("background")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 5))
        entry = ttk.Entry(dialog, textvariable=token, show="•", width=55)
        entry.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        actions = ttk.Frame(dialog)
        actions.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 12))

        def accept(imported=False):
            try:
                session = self.saved_apmyx_session() if imported else {"media-user-token": token.get()}
                values = dict(self.preferences)
                values["session"] = settings.protect_session(session["media-user-token"])
                if not values["native_account"] and re.fullmatch(r"[a-z]{2}", session.get("storefront", "")):
                    values["region"] = session["storefront"]
                self.store(values)
                token.set("")
                dialog.destroy()
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("无法连接资料库", str(error), parent=dialog)

        self.action(actions, "导入 apmyx", lambda: accept(True)).pack(side="left")
        self.action(actions, "连接", accept, primary=True).pack(side="right")
        entry.bind("<Return>", lambda _: accept())
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()
        entry.focus_set()

    def update_path(self, *_):
        base = self.fields["folder"].get().strip()
        category = self.fields["category"].get().strip()
        self.path_summary.set(str(Path(base) / category) if base else "请选择保存目录")

    def refresh_account(self):
        if self.preferences.get("native_account") and self.native_status and self.native_status.get("logged_in"):
            self.account_status.set("Apple Music 已登录")
        elif self.preferences.get("native_account"):
            self.account_status.set("有已记住的登录")
        elif self.preferences.get("session"):
            self.account_status.set("兼容会话已配置")
        else:
            self.account_status.set("请登录 Apple Music")
        self.logout_button.configure(state="normal" if not self.auth_busy and (
            self.preferences.get("native_account") or self.preferences.get("session")
        ) else "disabled")

    def toggle_legacy(self):
        if self.legacy_card.winfo_manager():
            self.legacy_card.grid_remove()
        else:
            self.legacy_card.grid()

    def refresh_engine(self):
        try:
            folder = find_backend()
            self.engine_status.set(f"下载引擎与 FFmpeg 已就绪\n{folder}")
        except RuntimeError as error:
            self.engine_status.set(str(error))

    def quality_changed(self, _event):
        self.save_preferences()

    def snapshot(self):
        values = {key: var.get() for key, var in self.fields.items()}
        values["quality"] = next(key for key, label in QUALITY.items() if label == self.quality_label.get())
        values["output"] = next(key for key, label in audio.OUTPUTS.items() if label == self.output_label.get())
        values["mp3_bitrate"] = int(values["mp3_bitrate"])
        if values["mp3_bitrate"] not in audio.MP3_BITRATES:
            raise ValueError("请选择有效的 MP3 码率。")
        values["folder"] = values["folder"].strip()
        values["category"] = values["category"].strip()
        values["region"] = values["region"].strip().lower()
        values["wrapper"] = values["wrapper"].strip()
        if not re.fullmatch(r"[a-z]{2}", values["region"]):
            raise ValueError("账号地区请输入两个字母，例如 au、cn 或 us。")
        if values["wrapper"]:
            path = Path(values["wrapper"])
            if not path.is_absolute() or path.suffix.lower() != ".exe" or not path.is_file():
                raise ValueError("请选择有效的本地服务 EXE 文件。")
        if self.preferences.get("session"):
            values["session"] = self.preferences["session"]
        return values

    def store(self, values):
        previous_session = self.preferences.get("session")
        settings.save(values)
        self.preferences = values
        self.fields["quality"].set(values["quality"])
        self.fields["output"].set(values["output"])
        self.fields["region"].set(values["region"])
        self.fields["native_account"].set(values["native_account"])
        self.refresh_account()
        if previous_session != values.get("session"):
            self.library_browser.invalidate()
            if self.active_page == "library":
                self.library_browser.ensure_loaded()

    def error(self, title, problem):
        messagebox.showerror(title, str(problem), parent=self.window)

    def save_preferences(self):
        try:
            values = self.snapshot()
            if values["folder"]:
                destination_for(values["folder"], values["category"])
            self.store(values)
            self.status.set("设置已保存")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法保存设置", error)

    def browse_folder(self):
        old = self.fields["folder"].get()
        chosen = filedialog.askdirectory(parent=self.window, title="选择下载保存目录",
                                         initialdir=old if old and Path(old).is_dir() else str(Path.home()))
        if chosen:
            self.fields["folder"].set(str(Path(chosen)))
            self.save_preferences()

    def open_folder(self):
        try:
            base = self.fields["folder"].get().strip()
            target = destination_for(base, self.fields["category"].get().strip())
            os.startfile(target if target.is_dir() else Path(base))
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法打开目录", error)

    def browse_wrapper(self):
        chosen = filedialog.askopenfilename(parent=self.window, title="选择本地服务程序",
                                            filetypes=(("Windows 应用程序", "*.exe"),))
        if chosen:
            self.fields["wrapper"].set(chosen)
            self.save_preferences()

    def set_auth_busy(self, active):
        self.auth_busy = active
        state = "disabled" if active else "normal"
        for widget in (self.user_entry, self.password_entry, self.code_entry, self.login_button,
                       self.cancel_auth_button, self.legacy_toggle):
            widget.configure(state=state)
        self.refresh_account()

    def reset_login(self):
        self.pending_region = None
        self.password.set("")
        self.auth_code.set("")
        self.code_label.grid_remove()
        self.code_entry.grid_remove()
        self.cancel_auth_button.pack_forget()
        self.login_button.configure(text="登录")

    def login_account(self):
        if self.auth_busy or self.busy:
            return
        region = self.fields["region"].get().strip().lower()
        if not re.fullmatch(r"[a-z]{2}", region):
            self.error("无法登录", "请输入两位字母的账号地区，例如 au、cn 或 us。")
            return
        code = None
        if self.pending_region:
            username = password = ""
            region = self.pending_region
            code = self.auth_code.get().strip()
            if not re.fullmatch(r"[0-9]{6}", code):
                self.error("无法登录", "请输入收到的 6 位验证码。")
                return
        else:
            username, password = self.username.get().strip(), self.password.get()
            if not username or not password:
                self.error("无法登录", "请输入 Apple 账号和密码。")
                return
        self.password.set("")
        self.auth_code.set("")
        self.auth_stop.clear()
        self.set_auth_busy(True)
        self.auth_status.set("正在连接本地登录服务…")

        def run():
            try:
                self.login_service.start(self.auth_stop, lambda value: self.post("auth_status", value))
                if self.auth_stop.is_set():
                    return
                self.post("auth_status", "正在等待 Apple 登录结果…")
                result = self.login_service.login(username, password, code=code)
                if result.get("logged_in"):
                    state = self.login_service.status()
                    regions = state.get("regions") or []
                    if regions and region not in regions:
                        region_to_save = regions[0]
                    else:
                        region_to_save = region
                else:
                    region_to_save = region
                self.post("login_result", (result, region_to_save))
            except (OSError, RuntimeError, ValueError) as error:
                self.post("auth_status", str(error))
            finally:
                self.post("auth_done", None)

        threading.Thread(target=run, daemon=True).start()

    def finish_login(self, result, region):
        if result.get("needs_code"):
            self.pending_region = region
            self.code_label.grid()
            self.code_entry.grid()
            self.cancel_auth_button.pack(side="left", padx=7)
            self.login_button.configure(text="验证并登录")
            self.auth_status.set("Apple 要求双重认证，请输入设备或短信收到的验证码。")
            return
        self.reset_login()
        if not result.get("logged_in"):
            self.auth_status.set("Apple Music 登录未完成。请检查账号或验证码。")
            return
        try:
            values = {**self.preferences, "native_account": True, "region": region}
            self.store(values)
            self.native_status = result
            self.refresh_account()
            self.username.set("")
            self.auth_status.set("登录成功。此会话将在下次启动时恢复。")
        except (OSError, ValueError) as error:
            self.auth_status.set(str(error))

    def logout_account(self):
        if self.auth_busy or self.busy:
            return
        if not self.preferences.get("native_account"):
            self.clear_account()
            return
        self.auth_stop.clear()
        self.set_auth_busy(True)
        self.auth_status.set("正在退出登录…")

        def run():
            try:
                self.login_service.start(self.auth_stop, lambda value: self.post("auth_status", value))
                if not self.auth_stop.is_set():
                    self.login_service.logout()
                    self.post("logout_result", None)
            except (OSError, RuntimeError, ValueError) as error:
                self.post("auth_status", str(error))
            finally:
                self.post("auth_done", None)

        threading.Thread(target=run, daemon=True).start()

    def save_account(self, imported=None):
        if self.auth_busy or self.busy:
            return
        try:
            values = self.snapshot()
            token = imported if imported is not None else self.token.get()
            if token.strip():
                values["session"] = settings.protect_session(token)
            elif not values.get("session"):
                raise ValueError("请输入会话令牌，或从 apmyx 导入。")
            values["native_account"] = False
            self.fields["native_account"].set(False)
            self.store(values)
            self.token.set("")
            self.auth_status.set("已切换到兼容会话。")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法保存账户", error)

    def import_account(self):
        if self.auth_busy or self.busy:
            return
        try:
            imported = self.saved_apmyx_session()
            self.fields["region"].set(imported.get("storefront") or self.fields["region"].get())
            self.save_account(imported["media-user-token"])
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法导入 apmyx", error)

    def clear_account(self):
        try:
            values = dict(self.preferences)
            values.pop("session", None)
            values["native_account"] = False
            self.fields["native_account"].set(False)
            self.native_status = None
            self.store(values)
            self.token.set("")
            self.reset_login()
            self.auth_status.set("已退出登录。")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法清除会话", error)

    def post(self, kind, value):
        self.messages.put((kind, value))

    def start_service(self):
        if self.service_busy or self.closing:
            return
        legacy = not self.preferences.get("native_account") and self.preferences.get("session")
        engine = None
        if legacy:
            try:
                engine = self.get_engine()
            except (OSError, RuntimeError) as error:
                self.service_status.set(str(error))
                return
        command = self.fields["wrapper"].get().strip()
        self.service_status.set("正在检查服务…")
        self.service_busy = True
        self.service_stop.clear()

        def run():
            try:
                report = lambda value: self.post("service", value)
                if engine is None:
                    self.login_service.start(self.service_stop, report)
                    status = self.login_service.status()
                    self.post("native_status", status)
                    report("登录服务已就绪" if status["logged_in"] else "登录服务已启动，请登录 Apple Music。")
                else:
                    engine.prepare_service(command, self.service_stop, report)
            except (OSError, RuntimeError, ValueError) as error:
                self.post("service", str(error))
            finally:
                self.post("service_done", None)

        threading.Thread(target=run, daemon=True).start()

    def get_engine(self):
        if self.engine is None:
            self.engine = Engine(ROOT, find_backend())
        return self.engine

    def start_download(self, links=None):
        if self.busy:
            return
        if self.auth_busy:
            self.status.set("请先完成 Apple Music 登录。")
            return
        try:
            if links is None:
                selected = {self.queue_items[row] for row in self.queue_tree.selection()}
                links = [url for url, state in self.queue_states.items() if
                         (url in selected if selected else state != "已完成")]
            if not links:
                self.status.set("队列里没有待下载项目。")
                return
            job = self.snapshot()
            if not job.get("native_account") and not job.get("session"):
                self.show("account")
                raise ValueError("请先登录 Apple Music。")
            if not job["folder"]:
                self.browse_folder()
                job = self.snapshot()
                if not job["folder"]:
                    return
            destination_for(job["folder"], job["category"])
            job["links"] = links
            engine = self.get_engine()
            self.store(job)
        except (OSError, RuntimeError, ValueError) as error:
            self.error("请检查下载设置", error)
            return
        self.stop.clear()
        self.set_busy(True)
        self.status.set("正在准备下载…")
        threading.Thread(target=self.worker, args=(job, engine)).start()

    def worker(self, job, engine):
        try:
            self.post("done", run_download(job, self.stop, self.post, engine, self.login_service))
        except Cancelled:
            self.post("done", (0, 0, 0))
        except (OSError, RuntimeError, ValueError) as error:
            self.post("failed", str(error))
        except Exception as error:
            self.post("failed", f"程序遇到意外错误（{type(error).__name__}）。")

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.remove_button.configure(state=state)
        self.quality_box.configure(state="disabled" if busy else "readonly")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(14)
        else:
            self.progress.stop()

    def cancel(self):
        self.stop.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消…")

    def close(self):
        self.closing = True
        self.stop_preview()
        self.service_stop.set()
        self.auth_stop.set()
        self.pending_region = None
        if self.busy:
            self.cancel()
        self.finish_close()

    def finish_close(self):
        if not self.closing or self.busy or self.service_busy or self.auth_busy:
            return False
        try:
            if self.engine is not None:
                self.engine.close_service()
        except (OSError, RuntimeError):
            pass
        try:
            self.login_service.close()
        except (OSError, RuntimeError):
            pass
        for timer in (self.start_timer, self.pump_timer):
            if timer:
                self.window.after_cancel(timer)
        self.window.destroy()
        return True

    def log_message(self, value):
        self.history.append(value)
        if len(self.history) > 1000:
            self.history.pop(0)
        if self.history_text is not None and self.history_text.winfo_exists():
            self.history_text.configure(state="normal")
            self.history_text.insert("end", "\n" + value)
            self.history_text.see("end")
            self.history_text.configure(state="disabled")

    def pump(self):
        for _ in range(100):
            try:
                kind, value = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "status" and not self.stop.is_set():
                self.status.set(value)
            elif kind == "log":
                self.log_message(value)
            elif kind == "queue_status":
                self.set_queue_state(*value)
            elif kind == "browser_result":
                browser, *result = value
                browser.receive(*result)
            elif kind == "browser_art":
                browser, *art = value
                browser.receive_art(*art)
            elif kind == "preview":
                self.play_preview(*value)
            elif kind == "service":
                self.service_status.set(value)
            elif kind == "native_status":
                self.native_status = value
                self.refresh_account()
            elif kind == "auth_status":
                self.auth_status.set(value)
            elif kind == "login_result" and not self.closing:
                self.finish_login(*value)
            elif kind == "logout_result":
                self.clear_account()
            elif kind == "auth_done":
                self.set_auth_busy(False)
                if self.pending_region:
                    self.code_entry.focus_set()
                if self.finish_close():
                    return
            elif kind == "service_done":
                self.service_busy = False
                if self.finish_close():
                    return
            elif kind in ("done", "failed"):
                self.set_busy(False)
                if self.finish_close():
                    return
                if kind == "done":
                    state = "已取消" if self.stop.is_set() else "处理完成"
                    saved, existing, errors = value
                    value = f"{state} · 新增 {saved} · 已存在 {existing} · 失败 {errors}"
                self.status.set(value)
                self.log_message(value)
        self.pump_timer = self.window.after(80, self.pump)


if __name__ == "__main__":
    configure_windows()
    window = tk.Tk()
    Studio(window)
    window.mainloop()
