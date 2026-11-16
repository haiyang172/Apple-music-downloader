"""Media Download Desk desktop application."""

import ctypes
import hashlib
import msvcrt
import os
from pathlib import Path
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk
import webbrowser
import winsound


ROOT = Path(__file__).resolve().parent
APP_TITLE = "影音下载台"
for library in (ROOT.parent / "playlist-download" / "python-deps", ROOT / "lib"):
    if library.is_dir():
        sys.path.insert(0, str(library))

import audio
from account_ui import VerificationDialog, build_account_page, show_library_connection
from apple_download import SOURCE, download_options, expand_link, forget_bilibili_cache, read_links, run_tasks, task_category
from bili_auth import BiliAuth
from bili_catalog import is_video_link, resolve_video
from bili_collections import is_collection_link, resolve_collection
from bili_ui import BiliBrowser
from download_queue import QueueStore
from engine import Engine
from library_cache import LibraryCache
from local_convert_ui import FORMATS as LOCAL_FORMATS, LocalConverter
from login_service import LoginService
from music_browser import MusicBrowser, TreeSort, prepare_preview
from music_catalog import CatalogClient
from netease_auth import NeteaseAuth
from playlist_export import save_m3u8
from qq_auth import QqAuth
from round_theme import install as install_round_theme, sidebar_badge
import settings
from shared_music import shared_music_source
from shared_music_ui import SharedMusicBrowser
from storage import clean_name, destination_for
from video_links_ui import VideoLinkPage, video_link_source
from windows_drop import install_drop_target


QUALITY = {"AAC": "AAC-LC · 兼容", "ALAC": "ALAC · 无损", "ATMOS": "Dolby Atmos"}
BILI_FORMATS = {"video": "视频 · MP4", "audio": "音频 · M4A", "flac": "音频 · 原始 FLAC", "mp3": "音频 · MP3"}
YOUTUBE_FORMATS = {"video": "视频 · MP4", "audio": "音频 · M4A", "mp3": "音频 · MP3"}
QUEUE_SOURCES = {"all": "全部来源", SOURCE: "Apple Music", "bilibili": "哔哩哔哩", "qq": "QQ 音乐", "netease": "网易云音乐",
                 "youtube": "YouTube", "local": "本地转码"}
TASK_LABELS = {"pending": "待下载", "running": "下载中", "complete": "已完成",
               "failed": "失败", "interrupted": "待继续", "cancelled": "已取消"}
PAGES = {"music": "Apple Music", "library": "Apple 资料库", "bilibili": "B 站", "qq": "QQ 音乐", "netease": "网易云音乐",
         "youtube": "YouTube", "douyin": "抖音", "download": "下载队列", "local": "本地转码",
         "account": "账户", "settings": "设置", "components": "组件"}
BG = "#eff5f2"
PANEL = "#ffffff"
INK = "#152d32"
MUTED = "#405d60"
NAV = "#f8fcfa"
NAV_ACTIVE = "#dcefe6"
NAV_ACCOUNT = "#edf6f1"
NAV_SIGNED_IN = "#08664f"
ACCENT = "#11715e"
BORDER = "#cdded6"


def backend_locations():
    installed = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "apmyx"
    return ROOT / "backend", installed, ROOT.parent / "GUI-downloader"


def find_backend():
    bridge = Path("_internal/backend/bin/bridge/bridge.exe")
    for folder in backend_locations():
        if (folder / bridge).is_file() and (folder / "tools/ffmpeg.exe").is_file():
            return folder
    raise RuntimeError("找不到下载引擎或 FFmpeg。请运行影音下载台安装包。")


def configure_windows():
    if os.name != "nt":
        return
    bundled_tcl = ROOT / "runtime" / "tcl"
    for variable, directory, entry in (
        ("TCL_LIBRARY", "tcl8.6", "init.tcl"),
        ("TK_LIBRARY", "tk8.6", "tk.tcl"),
    ):
        library = bundled_tcl / directory
        if (library / entry).is_file():
            os.environ[variable] = str(library)
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = (ctypes.c_wchar_p,)
        set_app_id("amdl.desktop")
    except (AttributeError, OSError):
        pass


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
        self.bili_auth = BiliAuth(settings.DATA / "bilibili-session.dat")
        self.music_auth = {"qq": QqAuth(settings.DATA / "qq-music-session.dat"),
                           "netease": NeteaseAuth(settings.DATA / "netease-music-session.dat")}
        self.library_cache = LibraryCache(settings.DATA / "library.sqlite3")
        self.background = BG
        self.queue_store = QueueStore(settings.DATA / "downloads.sqlite3")
        recovered = self.queue_store.recover()
        self.tasks = {}
        self.resolving_links = False
        self.links_stop = threading.Event()
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
        self.verification = None
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
        self.bili_format = tk.StringVar(window, BILI_FORMATS.get(self.preferences["bili_mode"], BILI_FORMATS["video"]))
        self.youtube_format = tk.StringVar(window, YOUTUBE_FORMATS.get(self.preferences["youtube_mode"], YOUTUBE_FORMATS["video"]))
        self.queue_source = tk.StringVar(window, QUEUE_SOURCES["all"])
        self.account_status = tk.StringVar(window)
        self.profile_text = tk.StringVar(window, "登录")
        self.profile_connected = False
        self.path_summary = tk.StringVar(window)
        self.status = tk.StringVar(window, load_error or (f"已恢复 {recovered} 项中断任务" if recovered else "就绪"))
        self.engine_status = tk.StringVar(window)
        self.service_status = tk.StringVar(window, "按下载音质启动")
        self.token = tk.StringVar(window)
        self.username = tk.StringVar(window)
        self.password = tk.StringVar(window)
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
        self.font = next((family for family in ("Microsoft YaHei UI", "Inter", "Segoe UI")
                          if family in installed_fonts), "TkDefaultFont")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            font.nametofont(name, root=window).configure(family=self.font, size=11)
        self.configure_style()

        self.make_sidebar()

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
        self.bili_browser = BiliBrowser(self, self.pages["bilibili"], BILI_FORMATS)
        self.shared_music_browsers = {source: SharedMusicBrowser(self, self.pages[source], source=source)
                                      for source in ("qq", "netease")}
        self.video_link_pages = {source: VideoLinkPage(self, self.pages[source], source, YOUTUBE_FORMATS.values())
                                 for source in ("youtube", "douyin")}
        self.local_converter = LocalConverter(self, self.pages["local"])
        self.drop_cleanup = None
        self.make_download()
        for task in self.queue_store.list_tasks():
            self.render_task(task)
        self.update_queue_count()
        build_account_page(self)
        self.make_settings()
        self.make_components()
        self.refresh_account()
        self.refresh_engine()
        for key in ("folder", "category"):
            self.fields[key].trace_add("write", self.update_path)
        self.update_path()
        self.show("music")
        if self.preferences.get("session"):
            self.library_browser.ensure_loaded()
        self.pump_timer = window.after(80, self.pump)
        self.start_timer = None
        if self.fields["auto_service"].get() and (
            self.fields["native_account"].get() or self.fields["quality"].get() in ("ALAC", "ATMOS")
        ):
            self.start_timer = window.after(250, self.start_service)

    def make_sidebar(self):
        self.rail = ttk.Frame(self.window, style="Rail.TFrame")
        self.rail.grid(row=0, column=0, sticky="ns")
        self.rail.grid_propagate(False)
        self.rail.columnconfigure(0, weight=1)
        self.rail.rowconfigure(1, weight=1)

        self.brand = tk.Frame(self.rail, background=NAV)
        self.brand.grid(row=0, column=0, sticky="w")
        self.brand_label = tk.Label(self.brand, background=NAV, borderwidth=0, padx=0, pady=0)
        self.brand_label.pack(side="left")
        self.brand_font = font.Font(root=self.window, family=self.font, size=11, weight="bold")
        self.brand_title = tk.Label(self.brand, text=APP_TITLE, font=self.brand_font,
                                    background=NAV, foreground=INK, padx=0, pady=0)
        self.brand_title.pack(side="left")

        viewport = tk.Frame(self.rail, background=NAV)
        viewport.grid(row=1, column=0, sticky="nsew")
        viewport.rowconfigure(0, weight=1)
        viewport.columnconfigure(0, weight=1)
        self.nav_canvas = tk.Canvas(viewport, background=NAV, borderwidth=0, highlightthickness=0,
                                    yscrollincrement=round(18 * self.scale), takefocus=False)
        self.nav_canvas.grid(row=0, column=0, sticky="nsew")
        self.nav_scrollbar = ttk.Scrollbar(viewport, command=self.nav_canvas.yview, style="Rail.Vertical.TScrollbar",
                                           takefocus=False)
        self.nav_scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, round(3 * self.scale)))
        self.nav_scrollbar.grid_remove()
        self.nav_canvas.configure(yscrollcommand=self.nav_scrollbar.set)
        self.nav_content = tk.Frame(self.nav_canvas, background=NAV)
        self.nav_content.columnconfigure(0, weight=1)
        canvas_item = self.nav_canvas.create_window(0, 0, window=self.nav_content, anchor="nw")
        self.nav_canvas.bind("<Configure>", lambda event: self._fit_nav(canvas_item, event.width))
        self.nav_content.bind("<Configure>", lambda _event: self._fit_nav(canvas_item, self.nav_canvas.winfo_width()))
        for widget in (self.nav_canvas, self.nav_content):
            widget.bind("<MouseWheel>", self._scroll_nav)

        primary_pages = [(key, label) for key, label in PAGES.items() if key != "account"]
        for row, (key, label) in enumerate(primary_pages):
            button = ttk.Button(self.nav_content, text=label, command=lambda page=key: self.show(page),
                                style="Nav.TButton", cursor="hand2")
            button.grid(row=row, column=0, sticky="ew")
            button.bind("<MouseWheel>", self._scroll_nav)
            button.bind("<FocusIn>", lambda event: self._reveal_nav(event.widget))
            self.navigation[key] = button

        self.profile_button = ttk.Button(
            self.rail, textvariable=self.profile_text, command=lambda: self.show("account"),
            style="Account.TButton", compound="left", cursor="hand2", takefocus=True,
        )
        self.profile_button.grid(row=2, column=0, sticky="ew")
        self.navigation["account"] = self.profile_button
        self.sidebar_assets = {
            prefix: {int(path.stem.rsplit("-", 1)[1]): path for path in (ROOT / "assets").glob(f"{prefix}-[0-9]*.png")
                     if path.stem.rsplit("-", 1)[1].isdigit()}
            for prefix in ("amdl", "account")
        }
        self.sidebar_scale = None
        self.refresh_sidebar_art()
        self.window.bind("<Configure>", self.refresh_sidebar_art, add="+")

    def _fit_nav(self, canvas_item, width):
        height = self.nav_content.winfo_reqheight()
        self.nav_canvas.itemconfigure(canvas_item, width=width)
        self.nav_canvas.configure(scrollregion=(0, 0, width, height))
        if height > self.nav_canvas.winfo_height():
            self.nav_scrollbar.grid()
        else:
            self.nav_canvas.yview_moveto(0)
            self.nav_scrollbar.grid_remove()

    def _scroll_nav(self, event):
        if event.delta and self.nav_scrollbar.winfo_ismapped():
            steps = max(1, abs(event.delta) // 120)
            self.nav_canvas.yview_scroll(-steps if event.delta > 0 else steps, "units")
            return "break"

    def _reveal_nav(self, button):
        if button is self.profile_button or not self.nav_scrollbar.winfo_ismapped():
            return
        top = self.nav_canvas.canvasy(0)
        height = self.nav_canvas.winfo_height()
        first, last = button.winfo_y(), button.winfo_y() + button.winfo_height()
        content_height = self.nav_content.winfo_reqheight()
        if first < top:
            self.nav_canvas.yview_moveto(max(0, first - round(18 * self.scale)) / content_height)
        elif last > top + height:
            self.nav_canvas.yview_moveto((last - height + round(18 * self.scale)) / content_height)

    def refresh_sidebar_art(self, event=None):
        if event is not None and event.widget is not self.window:
            return
        scale = self.scale
        if os.name == "nt" and event is not None:
            try:
                user32 = ctypes.windll.user32
                dpi = user32.GetDpiForWindow(ctypes.c_void_p(self.window.winfo_id()))
                if dpi:
                    scale = dpi / 96
            except (AttributeError, OSError):
                pass
        if scale == self.sidebar_scale:
            return
        self.sidebar_scale = scale
        inset = round(10 * scale)
        gap = round(7 * scale)
        brand_size = round(32 * scale)
        width = max(round(144 * scale), brand_size + self.brand_font.measure(APP_TITLE) + gap + 2 * inset + 4)
        self.rail.configure(width=width)
        self.rail.grid_configure(padx=(inset, 0), pady=inset)
        self.brand.grid_configure(padx=inset, pady=(round(13 * scale), round(15 * scale)))
        self.brand_title.pack_configure(padx=(gap, 0))
        for key, button in self.navigation.items():
            padding = (gap, inset) if key == "account" else (gap, 0) if key == "download" else round(scale)
            button.grid_configure(padx=(inset + gap, inset) if key == "library" else inset, pady=padding)
        self.brand_icon, self.profile_icon = (
            self.sidebar_icon(prefix, size, scale)
            for prefix, size in (("amdl", brand_size), ("account", round(23 * scale)))
        )
        self.brand_label.configure(image=self.brand_icon)
        self.profile_button.configure(image=self.profile_icon)

    def sidebar_icon(self, prefix, size, scale):
        assets = self.sidebar_assets[prefix]
        if not assets:
            return None
        glyph_size = max((asset for asset in assets if asset <= size), default=min(assets))
        glyph = tk.PhotoImage(master=self.window, file=assets[glyph_size])
        radius = round(8 * scale) if prefix == "amdl" else size / 2
        return sidebar_badge(self.window, glyph, size, "#163f3b", radius)

    def configure_style(self):
        style = ttk.Style(self.window)
        style.theme_use("clam")
        style.configure(".", font=(self.font, 11), foreground=INK)
        style.configure("TButton", padding=(13, 7))
        self.round_style_images = install_round_theme(
            style, self.window, self.scale, bg=BG, panel=PANEL, ink=INK, muted=MUTED, accent=ACCENT,
            border=BORDER, nav=NAV, nav_active=NAV_ACTIVE, nav_account=NAV_ACCOUNT,
            nav_signed_in=NAV_SIGNED_IN,
        )
        style.configure("MusicSearch.TEntry", padding=(13, 8))
        for name in ("Nav", "NavActive"):
            weight = "bold" if name == "NavActive" else "normal"
            style.configure(f"{name}.TButton", font=(self.font, 11, weight))
        for name in ("Account", "AccountActive", "AccountSignedIn", "AccountSignedInActive"):
            style.configure(f"{name}.TButton", font=(self.font, 11, "bold"))
        style.configure("TCheckbutton", background=PANEL, foreground=INK)
        style.configure("Surface.TFrame", background=BG)
        style.configure("MusicToolbar.TFrame", background=PANEL)
        style.configure("MusicTitle.TLabel", background=PANEL, foreground=INK, font=(self.font, 15, "bold"))
        style.configure("MusicStatus.TLabel", background=PANEL, foreground=MUTED, font=(self.font, 10))
        style.layout("Music.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure("Music.Vertical.TScrollbar", background="#d2e2db", troughcolor=PANEL,
                        bordercolor=PANEL, lightcolor="#d2e2db", darkcolor="#d2e2db", relief="flat",
                        arrowsize=round(9 * self.scale), gripcount=0)
        style.map("Music.Vertical.TScrollbar", background=[("active", "#b0cbc0")])
        style.layout("Rail.Vertical.TScrollbar", style.layout("Music.Vertical.TScrollbar"))
        style.configure("Rail.Vertical.TScrollbar", background=NAV_ACTIVE, troughcolor=NAV,
                        bordercolor=NAV, lightcolor=NAV, darkcolor=NAV, relief="flat",
                        arrowsize=round(7 * self.scale), gripcount=0)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e0ebe6",
                        lightcolor=ACCENT, darkcolor=ACCENT, bordercolor="#e0ebe6", thickness=6)
        for name, height in (("Music", 64), ("Queue", 38)):
            heading = "#f1f7f4" if name == "Music" else PANEL
            style.configure(f"{name}.Treeview", rowheight=round(height * self.scale), font=(self.font, 11),
                            fieldbackground=PANEL, background=PANEL, foreground=INK, relief="flat",
                            borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
            style.configure(f"{name}.Treeview.Heading", padding=(round(8 * self.scale), round(7 * self.scale)),
                            font=(self.font, 10, "bold"), background=heading, foreground=MUTED,
                            bordercolor=heading, lightcolor=heading, darkcolor=heading, relief="flat")
            style.map(f"{name}.Treeview", background=[("selected", "#dbefe5")], foreground=[("selected", INK)])
            style.map(f"{name}.Treeview.Heading", background=[("active", "#e7f2ed")])

    def label(self, parent, text="", *, variable=None, large=False, muted=False, background=PANEL, wrap=615):
        options = {"textvariable": variable} if variable is not None else {"text": text}
        return tk.Label(parent, **options, background=background, foreground=MUTED if muted else INK,
                        justify="left", anchor="w", wraplength=wrap,
                        font=(self.font, 21, "bold") if large else (self.font, 10 if muted else 11))

    def header(self, page, title, detail):
        self.label(page, title, large=True, background=BG).grid(row=0, column=0, sticky="w")
        self.label(page, detail, muted=True, background=BG).grid(row=1, column=0, sticky="w", pady=(4, 13))

    def panel(self, parent, padding=(14, 12)):
        return ttk.Frame(parent, style="Card.TFrame", padding=padding)

    def card(self, page, row):
        card = self.panel(page)
        card.grid(row=row, column=0, sticky="nsew", pady=(0, 9))
        return card

    def action(self, parent, text, command, primary=False):
        container = parent
        inside_card = False
        while container is not None and container is not self.window:
            if isinstance(container, ttk.Frame) and container.cget("style") == "Card.TFrame":
                inside_card = True
                break
            container = container.master
        if inside_card:
            style = "CardPrimary.TButton" if primary else "Card.TButton"
        else:
            style = "Primary.TButton" if primary else "TButton"
        return ttk.Button(parent, text=text, command=command, style=style)

    def textbox(self, parent, height, **options):
        return tk.Text(parent, height=height, wrap="word", padx=8, pady=5, borderwidth=0,
                       highlightthickness=0, bg=PANEL, fg=INK, insertbackground=INK,
                       font=(self.font, 11), **options)

    def make_download(self):
        page = self.pages["download"]
        page.rowconfigure(2, weight=1)
        title = ttk.Frame(page, style="Surface.TFrame")
        title.grid(row=0, column=0, sticky="ew")
        title.columnconfigure(0, weight=1)
        self.label(title, "下载队列", large=True, background=BG).grid(row=0, column=0, sticky="w")
        source_filter = ttk.Combobox(title, textvariable=self.queue_source, values=list(QUEUE_SOURCES.values()),
                                     state="readonly", width=12, style="Surface.TCombobox")
        source_filter.grid(row=0, column=1, padx=(8, 0))
        source_filter.bind("<<ComboboxSelected>>", lambda _: self.apply_queue_source())
        self.add_link_button = self.action(title, "添加链接", self.link_dialog)
        self.add_link_button.grid(row=0, column=2, padx=(8, 0))
        self.remove_button = self.action(title, "移除", self.remove_queued)
        self.remove_button.grid(row=0, column=3, padx=(8, 0))

        options = ttk.Frame(page, style="Surface.TFrame")
        options.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        options.columnconfigure(1, weight=1)
        self.apple_format_label = self.label(options, "Apple Music 音质", background=BG, muted=True)
        self.apple_format_label.grid(row=0, column=0, sticky="w", padx=(0, 7))
        self.quality_box = ttk.Combobox(options, textvariable=self.quality_label,
                                        values=list(QUALITY.values()), state="readonly", width=17,
                                        style="Surface.TCombobox")
        self.quality_box.grid(row=0, column=1, sticky="w")
        self.quality_box.bind("<<ComboboxSelected>>", self.quality_changed)
        self.bili_format_label = self.label(options, "B 站格式", background=BG, muted=True)
        self.bili_format_label.grid(row=1, column=0, sticky="w", padx=(0, 7), pady=(6, 0))
        self.bili_format_box = ttk.Combobox(options, textvariable=self.bili_format, values=list(BILI_FORMATS.values()),
                                            state="readonly", width=15, style="Surface.TCombobox")
        self.bili_format_box.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.bili_format_box.bind("<<ComboboxSelected>>", self.quality_changed)
        self.bili_options = ttk.Frame(options, style="Surface.TFrame")
        self.bili_options.grid(row=2, column=0, columnspan=5, sticky="w", pady=(5, 0))
        self.bili_danmaku_box = ttk.Checkbutton(self.bili_options, text="保存弹幕（ASS）", variable=self.fields["bili_danmaku"],
                                               command=self.save_preferences, style="Surface.TCheckbutton")
        self.bili_danmaku_box.pack(side="left")
        self.bili_subtitle_box = ttk.Checkbutton(self.bili_options, text="保存字幕（SRT）", variable=self.fields["bili_subtitle"],
                                               command=self.save_preferences, style="Surface.TCheckbutton")
        self.bili_subtitle_box.pack(side="left", padx=10)
        self.bili_category_box = ttk.Checkbutton(self.bili_options, text="自动分类", variable=self.fields["bili_auto_category"],
                                                command=self.save_preferences, style="Surface.TCheckbutton")
        self.bili_category_box.pack(side="left")
        self.youtube_format_label = self.label(options, "YouTube 格式", background=BG, muted=True)
        self.youtube_format_label.grid(row=3, column=0, sticky="w", padx=(0, 7), pady=(6, 0))
        self.youtube_format_box = ttk.Combobox(options, textvariable=self.youtube_format, values=list(YOUTUBE_FORMATS.values()),
                                               state="readonly", width=15, style="Surface.TCombobox")
        self.youtube_format_box.grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.youtube_format_box.bind("<<ComboboxSelected>>", self.quality_changed)
        options.columnconfigure(1, weight=0)
        options.columnconfigure(2, weight=1)
        self.label(options, variable=self.path_summary, background=BG, muted=True, wrap=245).grid(
            row=0, rowspan=2, column=2, sticky="w", padx=10)
        self.action(options, "保存目录", self.browse_folder).grid(row=0, rowspan=2, column=3)
        self.action(options, "打开", self.open_folder).grid(row=0, rowspan=2, column=4, padx=(6, 0))

        results = self.panel(page, padding=(8, 6))
        results.grid(row=2, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        self.queue_tree = ttk.Treeview(results, columns=("title", "artist", "batch", "state"),
                                       show="headings", selectmode="extended", style="Queue.Treeview")
        headings = {}
        for name, label, width, minimum in (
            ("title", "名称", 250, 140), ("artist", "来源 / 艺人", 116, 70),
            ("batch", "歌单 / 分 P", 200, 110), ("state", "状态", 84, 74),
        ):
            headings[name] = label
            self.queue_tree.heading(name, text=label)
            self.queue_tree.column(name, width=width, minwidth=minimum, stretch=name != "state")
        self.queue_sort = TreeSort(self.queue_tree, headings,
                                   lambda row, column: self.queue_tree.set(row, column))
        self.queue_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(results, command=self.queue_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.queue_tree.configure(yscrollcommand=scroll.set)
        self.queue_tree.bind("<Delete>", lambda _: self.remove_queued())
        self.queue_tree.bind("<Double-1>", self.open_task_details)
        self.queue_tree.bind("<Button-3>", self.queue_context_menu)

        buttons = ttk.Frame(page, style="Surface.TFrame")
        buttons.grid(row=3, column=0, sticky="ew", pady=(9, 7))
        self.start_button = self.action(buttons, "开始 / 继续", self.start_download, primary=True)
        self.start_button.pack(side="left")
        self.cancel_button = self.action(buttons, "暂停", self.pause)
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
        accepted = []
        for item in items:
            try:
                url = read_links(item.get("url", ""))[0]
            except ValueError:
                continue
            accepted.append(dict(item, source=SOURCE, url=url))
        return self.enqueue_tasks(accepted, begin)

    def queue_bili(self, rows, begin=False):
        return self.enqueue_tasks(rows, begin)

    def queue_local(self, paths, output, folder):
        rows = []
        start_ids = []
        try:
            job = self.snapshot()
            for path in paths:
                info = path.stat()
                destination = str(Path(folder) if folder else path.parent)
                options = download_options({**job, "folder": destination, "category": ""})
                options.update(local_format=output, local_size=info.st_size, local_mtime=info.st_mtime_ns)
                key = f"{os.path.normcase(destination)}\0{output}\0{info.st_size}\0{info.st_mtime_ns}"
                batch = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
                rows.append({"source": "local", "url": str(path), "title": path.name, "batch_key": batch,
                             "batch_title": LOCAL_FORMATS[output], "options": options})
            tasks = self._add_tasks(rows)
            start_ids = [task.id for task in tasks.values() if task.state not in ("running", "complete")]
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            self.error("无法加入转码队列", error)
            return 0
        if tasks:
            self.show_download_source("local")
            if start_ids:
                self.queue_tree.selection_set(*(str(task_id) for task_id in start_ids))
                self.start_download(start_ids)
            else:
                self.status.set("已有相同的已完成转码任务。")
        return len(tasks)

    def on_file_drop(self, paths):
        if self.closing:
            return
        self.show("local")
        if self.local_converter.add_files(paths):
            self.local_converter.quick_actions()

    def show_download_source(self, source):
        self.queue_source.set(QUEUE_SOURCES[source])
        self.apply_queue_source()
        self.show("download")

    def apply_queue_source(self):
        source = self.selected_queue_source()
        shown = [str(task.id) for task in self.tasks.values() if source == "all" or task.source == source]
        hidden = set(self.queue_tree.selection()) - set(shown)
        if hidden:
            self.queue_tree.selection_remove(*hidden)
        self.queue_tree.set_children("", *shown)
        self.queue_sort.apply()
        for key, widgets in ((SOURCE, (self.apple_format_label, self.quality_box)),
                             ("bilibili", (self.bili_format_label, self.bili_format_box, self.bili_options)),
                             ("youtube", (self.youtube_format_label, self.youtube_format_box))):
            for widget in widgets:
                if source in ("all", key):
                    widget.grid()
                else:
                    widget.grid_remove()

    def selected_queue_source(self):
        return next(key for key, label in QUEUE_SOURCES.items() if label == self.queue_source.get())

    def enqueue_tasks(self, rows, begin):
        try:
            tasks = self._add_tasks(rows)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.error("无法加入队列", error)
            return 0
        if tasks and begin:
            sources = {task.source for task in tasks.values()}
            self.show_download_source(next(iter(sources)) if len(sources) == 1 else "all")
            self.start_download(tasks)
        return len(tasks)

    def _add_tasks(self, rows):
        tasks = {task.id: task for task in self.queue_store.add(rows)}
        for task in tasks.values():
            self.render_task(task)
        self.queue_sort.apply()
        self.update_queue_count()
        return tasks

    def render_task(self, task):
        row = str(task.id)
        batch = task.batch_title
        if task.position:
            unit = "项" if task.source in ("bilibili", "youtube") else "首"
            position = f"{task.position}/{task.total}" if task.total else f"第 {task.position} {unit}"
            batch = f"{position} · {batch}"
        artist = task.artist if task.source == SOURCE else QUEUE_SOURCES.get(task.source, task.source)
        values = (task.title, artist, batch, TASK_LABELS[task.state])
        if task.id in self.tasks:
            self.queue_tree.item(row, values=values)
        else:
            self.queue_tree.insert("", "end", iid=row, values=values)
        self.tasks[task.id] = task
        source = self.selected_queue_source()
        if source not in ("all", task.source):
            self.queue_tree.detach(row)

    def update_queue_count(self):
        pending = sum(task.state in ("pending", "running", "interrupted") for task in self.tasks.values())
        self.navigation["download"].configure(text=f"下载队列  {pending}" if pending else "下载队列")

    def remove_queued(self):
        if self.busy:
            return
        rows = self.queue_tree.selection()
        try:
            self.queue_store.remove(int(row) for row in rows)
        except sqlite3.Error as error:
            self.error("无法移除下载任务", error)
            return
        for row in rows:
            task = self.tasks.pop(int(row))
            if task.state in ("failed", "interrupted", "cancelled"):
                forget_bilibili_cache(self.queue_store, task)
            self.queue_tree.delete(row)
        self.update_queue_count()

    def open_task_details(self, event):
        row = self.queue_tree.identify_row(event.y)
        if row:
            self.show_task_details(row)

    def show_task_details(self, row):
        task = self.tasks[int(row)]
        lines = [task.title, TASK_LABELS[task.state]]
        if task.uploader:
            lines.append(f"投稿者 · {task.uploader}")
        if task.source_category:
            lines.append(f"分区 · {task.source_category}")
        if task.options:
            if task.source == "bilibili":
                lines.append(BILI_FORMATS.get(task.options.get("bili_mode"), BILI_FORMATS["video"]))
                if task.audio_title:
                    title = f"{task.artist} - {task.audio_title}" if task.artist else task.audio_title
                    lines.append(f"识别歌名 · {title}")
                if task.options.get("bili_danmaku"):
                    lines.append("保存弹幕（ASS）")
                if task.options.get("bili_subtitle"):
                    lines.append("保存字幕（SRT）")
            elif task.source == "youtube":
                lines.append(YOUTUBE_FORMATS.get(task.options.get("youtube_mode"), YOUTUBE_FORMATS["video"]))
            elif task.source == "local":
                lines.extend((task.url, LOCAL_FORMATS.get(task.options.get("local_format"), "")))
            lines.append(str(Path(task.options["folder"]) / task_category(task)))
        if task.error:
            lines.append(task.error)
        lines.extend(task.files)
        messagebox.showinfo("下载任务", "\n".join(lines), parent=self.window)

    def queue_context_menu(self, event):
        row = self.queue_tree.identify_row(event.y)
        if not row:
            return
        if row not in self.queue_tree.selection():
            self.queue_tree.selection_set(row)
        menu = tk.Menu(self.queue_tree, tearoff=False)
        menu.add_command(label="查看任务详情", command=lambda: self.show_task_details(row))
        if any(self.tasks[int(item)].files for item in self.queue_tree.selection()):
            menu.add_command(label="用已保存文件转码", command=self.convert_saved_selection)
            menu.add_command(label="导出播放列表（M3U8）", command=self.export_queue_playlist)
        remote = all(self.tasks[int(item)].source != "local" for item in self.queue_tree.selection())
        menu.add_command(label="按当前设置重新排队", command=self.requeue_selected,
                         state="normal" if remote and not self.busy else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def convert_saved_selection(self):
        paths = [path for row in self.queue_tree.selection() for path in self.tasks[int(row)].files]
        if paths:
            self.show("local")
            self.local_converter.add_files(paths)

    def export_queue_playlist(self):
        selected = set(self.queue_tree.selection())
        tasks = [self.tasks[int(row)] for row in self.queue_tree.get_children() if row in selected]
        batches = {task.batch_title for task in tasks if task.files}
        batch = next(iter(batches)) if len(batches) == 1 else ""
        initial_dir = next((Path(task.files[0]).parent for task in tasks if task.files), None)
        if initial_dir is None:
            return
        path = filedialog.asksaveasfilename(parent=self.window, title="导出播放列表",
                                            initialdir=str(initial_dir), initialfile=f"{clean_name(batch or '播放列表')}.m3u8",
                                            defaultextension=".m3u8", filetypes=[("M3U8 播放列表", "*.m3u8")])
        if not path:
            return
        try:
            count = save_m3u8(path, tasks)
        except (OSError, ValueError) as error:
            self.error("无法导出播放列表", error)
        else:
            self.status.set(f"已导出 {count} 首 · {Path(path).name}")

    def requeue_selected(self):
        if self.busy:
            return
        if any(self.tasks[int(row)].source == "local" for row in self.queue_tree.selection()):
            return
        try:
            job = self.snapshot()
            if not job["folder"]:
                self.browse_folder()
                job = self.snapshot()
                if not job["folder"]:
                    return
            options = download_options(job)
            self.store(job)
            tasks = self.queue_store.requeue((int(row) for row in self.queue_tree.selection()), options)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            self.error("无法重新排队", error)
            return
        for task in tasks:
            self.render_task(task)
        self.queue_tree.selection_set(tuple(str(task.id) for task in tasks))
        self.queue_sort.apply()
        self.update_queue_count()
        self.status.set(f"已按当前设置重新排队 {len(tasks)} 首")

    def add_link_text(self, text, begin=False):
        if self.resolving_links:
            self.status.set("正在读取上一批链接…")
            self.show("download")
            return False
        links = list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))
        if not links:
            self.error("添加链接", "请粘贴受支持的音乐或视频链接。")
            return False
        if len(links) == 1:
            link = links[0]
            if is_collection_link(link):
                self.show("bilibili")
                return self.bili_browser.load_link(link)
            source = shared_music_source(link)
            if source:
                browser = self.shared_music_browsers[source]
                self.show(source)
                browser.link.set(link)
                browser.resolve()
                return True
            source = video_link_source(link)
            if source:
                self.show(source)
                self.video_link_pages[source].load_link(link)
                return True
        self.show_download_source("all")
        self.resolving_links = True
        self.add_link_button.configure(state="disabled")
        self.status.set("正在读取链接…")
        region = self.catalog_region()
        bili_session = self.bili_browser.account.session

        def resolve():
            rows, errors = [], []
            for link in links:
                if self.closing:
                    return
                try:
                    if is_collection_link(link):
                        collection = resolve_collection(link, session=bili_session, stop=self.links_stop)
                        rows.extend(collection["rows"])
                        if collection.get("warning"):
                            errors.append(collection["warning"])
                    elif is_video_link(link):
                        rows.extend(resolve_video(link, session=bili_session)["rows"])
                    else:
                        apple_link, = read_links(link)
                        rows.extend(dict(row, source=SOURCE) for row in expand_link(apple_link, self.catalog, region))
                except (OSError, RuntimeError, ValueError) as error:
                    errors.append(str(error))
            self.post("links_resolved", (rows, errors, begin))

        threading.Thread(target=resolve, daemon=True).start()
        return True

    def finish_links(self, rows, errors, begin):
        self.resolving_links = False
        self.add_link_button.configure(state="normal")
        if self.closing:
            return
        for error in errors:
            self.log_message(error)
        count = self.enqueue_tasks(rows, begin=begin)
        if not self.busy:
            if errors:
                self.status.set(f"已加入 {count} 项 · {errors[0]}" if len(errors) == 1 else
                                f"已加入 {count} 项 · {len(errors)} 个链接未能读取")
            else:
                self.status.set(f"已加入队列 {count} 项")

    def link_dialog(self):
        popup = tk.Toplevel(self.window)
        popup.title("添加链接")
        popup.transient(self.window)
        popup.configure(background=BG)
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(0, weight=1)
        field = self.panel(popup, padding=(6, 6))
        field.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)
        entry = self.textbox(field, 5, undo=True, width=58)
        entry.pack(fill="both", expand=True)

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
        popup.configure(background=BG)
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(0, weight=1)
        panel = self.panel(popup, padding=(6, 6))
        panel.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        history = self.textbox(panel, 12, width=65)
        history.pack(fill="both", expand=True)
        saved = []
        for task in self.tasks.values():
            if task.state not in ("complete", "failed", "interrupted"):
                continue
            saved.append(f"{TASK_LABELS[task.state]} · {task.title}")
            if task.error:
                saved.append(task.error)
            saved.extend(task.files)
        history.insert("1.0", "\n".join(saved + self.history))
        history.configure(state="disabled")
        self.history_text = history
        self.history_window = popup

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
        self._reveal_nav(self.navigation[page])
        for key, button in self.navigation.items():
            if key != "account":
                button.configure(style="NavActive.TButton" if key == page else "Nav.TButton")
        self.update_profile_style()
        if page == "music":
            self.music_browser.ensure_loaded()
        elif page == "library":
            self.library_browser.ensure_loaded()
        elif page == "account" and not self.auth_busy:
            if self.verification is not None:
                self.verification.focus()
            elif not self.preferences.get("native_account") and not self.preferences.get("session"):
                self.user_entry.focus_set()

    def update_profile_style(self):
        name = "AccountSignedIn" if self.profile_connected else "Account"
        if self.active_page == "account":
            name += "Active"
        self.profile_button.configure(style=name + ".TButton")

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

    def connect_library(self):
        show_library_connection(self)

    def update_path(self, *_):
        base = self.fields["folder"].get().strip()
        category = self.fields["category"].get().strip()
        self.path_summary.set(str(Path(base) / category) if base else "请选择保存目录")

    def refresh_account(self):
        remembered = self.preferences.get("native_account")
        legacy = self.preferences.get("session")
        signed_in = bool(remembered and self.native_status and self.native_status.get("logged_in"))
        if signed_in:
            self.account_status.set("Apple Music 已登录")
            caption = "已登录"
        elif remembered and self.service_busy:
            self.account_status.set("正在恢复 Apple Music 会话")
            caption = "恢复中"
        elif remembered and self.native_status is not None:
            self.account_status.set("请重新登录 Apple Music")
            caption = "重新登录"
        elif remembered:
            self.account_status.set("有已记住的登录")
            caption = "账户"
        elif legacy:
            self.account_status.set("兼容会话已配置")
            caption = "已连接"
        else:
            self.account_status.set("请登录 Apple Music")
            caption = "登录"
        self.profile_text.set("处理中" if self.auth_busy else caption)
        self.profile_connected = signed_in or bool(legacy)
        self.update_profile_style()
        self.logout_button.configure(state="normal" if not self.auth_busy and (remembered or legacy) else "disabled")

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
        values["bili_mode"] = next(key for key, label in BILI_FORMATS.items() if label == self.bili_format.get())
        values["youtube_mode"] = next(key for key, label in YOUTUBE_FORMATS.items() if label == self.youtube_format.get())
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
            if self.active_page == "library" or values.get("session"):
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
        for widget in (self.user_entry, self.password_entry, self.region_entry, self.login_button, self.legacy_toggle):
            widget.configure(state=state)
        if self.verification is not None:
            self.verification.set_busy(active)
        self.refresh_account()

    def reset_login(self):
        self.pending_region = None
        if self.verification is not None:
            self.verification.dismiss()
            self.verification = None

    def cancel_verification(self):
        self.reset_login()
        self.auth_status.set("已取消验证。")
        self.set_auth_busy(True)

        def run():
            try:
                self.login_service.cancel_login()
            finally:
                self.post("auth_done", None)

        threading.Thread(target=run, daemon=True).start()

    def login_account(self):
        if self.auth_busy or self.busy:
            return
        if self.verification is not None:
            self.verification.focus()
            return
        region = self.fields["region"].get().strip().lower()
        if not re.fullmatch(r"[a-z]{2}", region):
            self.error("无法登录", "请输入两位字母的账号地区，例如 au、cn 或 us。")
            return
        username, password = self.username.get().strip(), self.password.get()
        if not username or not password:
            self.error("无法登录", "请输入 Apple 账号和密码。")
            return
        self.request_login(username, password, region)

    def submit_verification(self, code):
        if not self.auth_busy and self.pending_region is not None:
            self.request_login("", "", self.pending_region, code)

    def request_login(self, username, password, region, code=None):
        self.auth_stop.clear()
        self.set_auth_busy(True)
        self.auth_status.set("正在验证…" if code else "正在连接本地登录服务…")

        def run():
            try:
                self.login_service.start(self.auth_stop, lambda value: self.post("auth_status", value))
                if self.auth_stop.is_set():
                    return
                if not code:
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
                self.post("auth_error", (str(error), bool(code and self.login_service.login_id)))
            finally:
                self.post("auth_done", None)

        threading.Thread(target=run, daemon=True).start()

    def finish_login(self, result, region):
        if result.get("needs_code"):
            self.pending_region = region
            if self.verification is None:
                self.verification = VerificationDialog(self, self.submit_verification, self.cancel_verification)
                self.verification.set_busy(self.auth_busy)
            else:
                self.verification.set_error("验证码未通过，请重试。")
            self.auth_status.set("等待验证码。")
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
            if values.get("session"):
                self.library_browser.refresh()
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

    def save_account(self):
        if self.auth_busy or self.busy:
            return
        try:
            values = self.snapshot()
            token = self.token.get()
            if token.strip():
                values["session"] = settings.protect_session(token)
            elif not values.get("session"):
                raise ValueError("请输入 Apple Music 网页会话令牌。")
            self.store(values)
            self.token.set("")
            self.auth_status.set("已连接 Apple Music 网页会话。")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法保存账户", error)

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
            self.password.set("")
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
        self.refresh_account()
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

    def start_download(self, task_ids=None):
        if self.busy:
            return
        try:
            if task_ids is None:
                selected = set(self.queue_tree.selection())
                task_ids = [int(row) for row in self.queue_tree.get_children() if not selected or row in selected]
            tasks = [self.tasks[task_id] for task_id in task_ids
                     if self.tasks[task_id].state not in ("running", "complete")]
            if not tasks:
                self.status.set("队列里没有待下载项目。")
                return
            job = self.snapshot()
            apple_only = all(task.source == SOURCE for task in tasks)
            apple_ready = not self.auth_busy and (job.get("native_account") or job.get("session"))
            if not apple_ready:
                if apple_only:
                    if self.auth_busy:
                        self.status.set("请先完成 Apple Music 登录。")
                        return
                    self.show("account")
                    raise ValueError("请先登录 Apple Music。")
                if any(task.source == SOURCE for task in tasks):
                    self.log_message("Apple Music 任务等待登录，本次继续处理其他来源。")
                    tasks = [task for task in tasks if task.source != SOURCE]
            qq_account = self.shared_music_browsers["qq"].account
            sessions = {source: browser.account.session for source, browser in self.shared_music_browsers.items()}
            sessions["bilibili"] = self.bili_browser.account.session
            if sessions["qq"] is None and any(task.source == "qq" for task in tasks):
                tasks = [task for task in tasks if task.source != "qq"]
                if not tasks:
                    self.status.set("请先登录 QQ 音乐。")
                    self.show("qq")
                    qq_account.open()
                    return
                self.log_message("QQ 音乐任务等待登录，本次继续处理其他来源。")
            if any(task.options is None for task in tasks):
                if not job["folder"]:
                    self.browse_folder()
                    job = self.snapshot()
                    if not job["folder"]:
                        return
                options = download_options(job)
                self.store(job)
                tasks = self.queue_store.prepare((task.id for task in tasks), options)
                for task in tasks:
                    self.render_task(task)
            engine = self.get_engine()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            self.error("请检查下载设置", error)
            return
        self.stop.clear()
        self.set_busy(True)
        self.status.set("正在准备下载…")
        threading.Thread(target=self.worker, args=(tasks, job, engine, sessions)).start()

    def worker(self, tasks, job, engine, sessions):
        try:
            result = run_tasks(self.queue_store, tasks, job, engine, self.login_service, self.stop, self.post,
                               sessions=sessions)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            failure = str(error)
        except Exception as error:
            failure = f"程序遇到意外错误（{type(error).__name__}）。"
        else:
            self.post("done", result)
            return
        try:
            if self.queue_store.recover():
                for task in self.queue_store.list_tasks():
                    if task.state == "interrupted":
                        self.post("queue_task", task)
        except sqlite3.Error:
            pass
        self.post("failed", failure)

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.remove_button.configure(state=state)
        self.quality_box.configure(state="disabled" if busy else "readonly")
        self.bili_format_box.configure(state="disabled" if busy else "readonly")
        self.youtube_format_box.configure(state="disabled" if busy else "readonly")
        self.bili_danmaku_box.configure(state=state)
        self.bili_subtitle_box.configure(state=state)
        self.bili_category_box.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(14)
        else:
            self.progress.stop()

    def pause(self):
        self.stop.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在暂停…")

    def close(self):
        self.closing = True
        self.links_stop.set()
        self.bili_browser.stop.set()
        for page in self.video_link_pages.values():
            page.stop.set()
        self.bili_browser.account.close()
        for browser in self.shared_music_browsers.values():
            browser.close()
        self.music_browser.stop_view_updates()
        self.library_browser.stop_view_updates()
        self.stop_preview()
        self.service_stop.set()
        self.auth_stop.set()
        self.reset_login()
        if self.busy:
            self.pause()
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
        if self.drop_cleanup is not None:
            self.drop_cleanup()
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
            elif kind == "queue_task":
                self.render_task(value)
                if self.queue_sort.column == "state":
                    self.queue_sort.apply()
                self.update_queue_count()
            elif kind == "links_resolved":
                self.finish_links(*value)
            elif kind == "bili_result":
                self.bili_browser.receive(*value)
            elif kind == "youtube_result":
                self.video_link_pages["youtube"].receive(*value)
            elif kind == "bili_auth":
                self.bili_browser.account.receive(*value)
            elif kind == "qq_auth":
                self.shared_music_browsers["qq"].account.receive(*value)
            elif kind == "netease_auth":
                self.shared_music_browsers["netease"].account.receive(*value)
            elif kind == "shared_music_result":
                source, *result = value
                if source in self.shared_music_browsers:
                    self.shared_music_browsers[source].receive(*result)
            elif kind == "browser_batch":
                browser, *result = value
                browser.receive_batch(*result)
            elif kind == "browser_result":
                browser, *result = value
                browser.receive(*result)
            elif kind == "browser_library":
                browser, *progress = value
                browser.receive_library(*progress)
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
            elif kind == "auth_error":
                message, retry_code = value
                if self.verification is not None and retry_code:
                    self.verification.set_error(message)
                else:
                    self.reset_login()
                self.auth_status.set(message)
            elif kind == "login_result" and not self.closing:
                self.finish_login(*value)
            elif kind == "logout_result":
                self.clear_account()
            elif kind == "auth_done":
                self.set_auth_busy(False)
                if self.verification is not None:
                    self.verification.focus()
                if self.finish_close():
                    return
            elif kind == "service_done":
                self.service_busy = False
                self.refresh_account()
                if self.finish_close():
                    return
            elif kind in ("done", "failed"):
                self.set_busy(False)
                if self.finish_close():
                    return
                if kind == "done":
                    state = "已暂停" if self.stop.is_set() else "处理结束"
                    saved, existing, errors, waiting = value
                    value = f"{state} · 新增 {saved} · 已存在 {existing} · 失败 {errors}"
                    if waiting:
                        value += f" · 等待原保存磁盘 {waiting}"
                self.status.set(value)
                self.log_message(value)
        self.pump_timer = self.window.after(80, self.pump)


def main():
    configure_windows()
    window = tk.Tk()
    window.withdraw()
    try:
        settings.DATA.mkdir(parents=True, exist_ok=True)
        with (settings.DATA / "downloads.lock").open("a+b") as lock:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                messagebox.showinfo(APP_TITLE, "影音下载台已在运行。", parent=window)
                return
            app = Studio(window)
            window.deiconify()
            window.update_idletasks()
            try:
                app.drop_cleanup = install_drop_target(window, app.on_file_drop)
            except OSError:
                app.local_converter.status.set("点「添加文件」选择要转码的音频或视频。")
            window.mainloop()
    except (OSError, sqlite3.Error) as error:
        messagebox.showerror(APP_TITLE, f"无法打开下载队列。\n{error}", parent=window)
    finally:
        if window.tk.call("info", "commands", "."):
            window.destroy()


if __name__ == "__main__":
    main()
