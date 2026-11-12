"""AMDL Studio desktop application."""

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


ROOT = Path(__file__).resolve().parent
APP_TITLE = "AMDL Studio"
for library in (ROOT.parent / "playlist-download" / "python-deps", ROOT / "lib"):
    if library.is_dir():
        sys.path.insert(0, str(library))

import audio
from engine import Cancelled, Engine
import settings
from storage import destination_for, publish, volume_identity


QUALITY = {"AAC": "AAC-LC · 兼容", "ALAC": "ALAC · 无损", "ATMOS": "Dolby Atmos"}
PAGES = {"download": "下载", "account": "账户", "settings": "设置", "components": "组件"}
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
    raise RuntimeError("找不到下载引擎或 FFmpeg。请运行 AMDL Studio 安装包。")


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


def run_download(job, stop, post, engine=None):
    paths = destination_for(job["folder"], job["category"])
    disk = volume_identity(Path(job["folder"]))
    engine = engine or Engine(ROOT, find_backend())
    config = {"media-user-token": settings.session(job), "storefront": job["region"]}
    if not config["media-user-token"]:
        raise ValueError("请先在「账户」里导入或保存 Apple Music 会话。")
    quality = job["quality"]
    if quality in ("ALAC", "ATMOS"):
        engine.prepare_service(job["wrapper"], stop, lambda status: post("status", status))

    saved = existing = errors = 0
    for index, url in enumerate(job["links"], 1):
        if stop.is_set():
            break
        prefix = f"链接 {index}/{len(job['links'])}"
        post("status", prefix + " · 正在读取曲目")

        def on_event(event):
            if event.get("event") == "progress":
                data = event.get("data", {})
                label = data.get("name") or data.get("phase")
                if label:
                    post("status", prefix + " · " + str(label)[:75])
            elif event.get("event") == "warning":
                post("log", str(event.get("message", "")))

        with tempfile.TemporaryDirectory(prefix="AMDL-Studio-") as working:
            stage = Path(working)
            try:
                result = engine.download(config, url, quality, stage, stop, on_event)
            except Cancelled:
                break
            except (OSError, RuntimeError, ValueError) as error:
                errors += 1
                post("log", f"{prefix} · {error}")
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
    return saved, existing, errors


class Studio:
    def __init__(self, window):
        self.window = window
        self.stop = threading.Event()
        self.service_stop = threading.Event()
        self.messages = queue.SimpleQueue()
        self.engine = None
        self.busy = False
        self.closing = False
        self.service_busy = False
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
        self.pages = {}
        self.navigation = {}

        window.title(APP_TITLE)
        window.geometry("940x650")
        window.minsize(790, 600)
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.rowconfigure(0, weight=1)
        window.columnconfigure(1, weight=1)

        installed_fonts = set(font.families(window))
        self.font = "Inter" if "Inter" in installed_fonts else "Segoe UI"
        self.configure_style()

        rail = tk.Frame(window, background=NAV, width=155)
        rail.grid(row=0, column=0, sticky="ns")
        rail.grid_propagate(False)
        rail.columnconfigure(0, weight=1)
        rail.rowconfigure(7, weight=1)
        tk.Label(rail, text="AMDL Studio", font=(self.font, 15, "bold"),
                 background=NAV, foreground="white").grid(row=0, column=0, padx=14, pady=(24, 23), sticky="w")
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

        self.make_download()
        self.make_account()
        self.make_settings()
        self.make_components()
        self.refresh_account()
        self.refresh_engine()
        for key in ("folder", "category"):
            self.fields[key].trace_add("write", self.update_path)
        self.update_path()
        self.show("download")
        self.links.focus_set()
        window.after(80, self.pump)
        if self.fields["auto_service"].get() and self.fields["quality"].get() in ("ALAC", "ATMOS"):
            window.after(250, self.start_service)

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
        style.configure("TEntry", padding=4, fieldbackground=PANEL)
        style.configure("TCombobox", padding=4, fieldbackground=PANEL, background=PANEL)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], selectforeground=[("readonly", INK)],
                  selectbackground=[("readonly", PANEL)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#dce8e3",
                        lightcolor=ACCENT, darkcolor=ACCENT, bordercolor="#dce8e3")

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
        self.header(page, "下载音乐", "粘贴 Apple Music 的歌曲、专辑或歌单链接，每行一个。")
        input_card = self.card(page, 2)
        input_card.columnconfigure(0, weight=1)
        self.links = self.textbox(input_card, 4, undo=True)
        self.links.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.label(input_card, "下载音质").grid(row=1, column=0, sticky="w", pady=(9, 0))
        self.quality_box = ttk.Combobox(input_card, textvariable=self.quality_label,
                                        values=list(QUALITY.values()), state="readonly", width=18)
        self.quality_box.grid(row=1, column=1, padx=(8, 10), pady=(9, 0))
        self.quality_box.bind("<<ComboboxSelected>>", self.quality_changed)
        self.action(input_card, "选择保存目录", self.browse_folder).grid(row=1, column=2, pady=(9, 0))
        self.action(input_card, "打开目录", self.open_folder).grid(row=1, column=3, padx=(6, 0), pady=(9, 0))
        self.label(input_card, variable=self.path_summary, muted=True).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        buttons = tk.Frame(page, bg=BG)
        buttons.grid(row=3, column=0, sticky="ew", pady=(1, 9))
        self.start_button = self.action(buttons, "开始下载", self.start_download, primary=True)
        self.start_button.pack(side="left")
        self.cancel_button = self.action(buttons, "取消", self.cancel)
        self.cancel_button.pack(side="left", padx=7)
        self.cancel_button.configure(state="disabled")
        self.label(buttons, variable=self.account_status, muted=True, background=BG).pack(side="right")

        page.rowconfigure(4, weight=1)
        history = self.card(page, 4)
        history.columnconfigure(0, weight=1)
        history.rowconfigure(1, weight=1)
        self.label(history, "下载记录").grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.log = self.textbox(history, 4, state="disabled")
        self.log.grid(row=1, column=0, sticky="nsew")
        bar = ttk.Scrollbar(history, command=self.log.yview)
        bar.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set)
        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.grid(row=5, column=0, sticky="ew")
        self.label(page, variable=self.status, background=BG, muted=True).grid(
            row=6, column=0, sticky="w", pady=(4, 0))

    def make_account(self):
        page = self.pages["account"]
        self.header(page, "Apple Music 账户", "可导入本机 apmyx 的会话，或在 Apple Music 登录后添加会话令牌。")
        card = self.card(page, 2)
        card.columnconfigure(1, weight=1)
        self.label(card, variable=self.account_status).grid(row=0, column=0, columnspan=2, sticky="w")
        self.label(card, "账号地区").grid(row=1, column=0, sticky="w", pady=(10, 4))
        ttk.Entry(card, textvariable=self.fields["region"], width=7).grid(row=1, column=1, sticky="w", padx=12)
        self.label(card, "会话令牌").grid(row=2, column=0, sticky="w")
        ttk.Entry(card, textvariable=self.token, show="•").grid(row=2, column=1, sticky="ew", padx=(12, 0))
        self.label(card, "令牌由 Windows 为当前用户加密保存。", muted=True).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 7))
        actions = tk.Frame(card, bg=PANEL)
        actions.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.action(actions, "保存会话", self.save_account, primary=True).pack(side="left")
        self.action(actions, "导入 apmyx", self.import_account).pack(side="left", padx=7)
        self.action(actions, "打开 Apple Music 登录",
                    lambda: webbrowser.open("https://music.apple.com/")).pack(side="left")
        self.action(actions, "清除会话", self.clear_account).pack(side="right")

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
        self.header(page, "下载组件", "在这里检查下载引擎、FFmpeg 和无损／全景声服务。")
        card = self.card(page, 2)
        card.columnconfigure(1, weight=1)
        self.label(card, variable=self.engine_status, wrap=545).grid(row=0, column=0, columnspan=2, sticky="w")
        self.action(card, "重新检查", self.refresh_engine).grid(row=0, column=2, sticky="e")
        self.label(card, "服务程序").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(card, textvariable=self.fields["wrapper"]).grid(row=1, column=1, sticky="ew", padx=10, pady=(10, 0))
        self.action(card, "浏览", self.browse_wrapper).grid(row=1, column=2, pady=(10, 0))
        self.label(card, "留空时使用本机已有的 Ubuntu-AMDL。", muted=True).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=10, pady=(4, 5))
        ttk.Checkbutton(card, text="打开程序时启动所选音质需要的服务",
                        variable=self.fields["auto_service"]).grid(row=3, column=0, columnspan=3, sticky="w")
        self.label(card, variable=self.service_status, muted=True).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(7, 6))
        buttons = tk.Frame(card, bg=PANEL)
        buttons.grid(row=5, column=0, columnspan=3, sticky="w")
        self.action(buttons, "保存设置", self.save_preferences, primary=True).pack(side="left")
        self.action(buttons, "启动服务", self.start_service).pack(side="left", padx=7)

    def show(self, page):
        self.pages[page].tkraise()
        for key, button in self.navigation.items():
            button.configure(background=NAV_ACTIVE if key == page else NAV)

    def update_path(self, *_):
        base = self.fields["folder"].get().strip()
        category = self.fields["category"].get().strip()
        self.path_summary.set(str(Path(base) / category) if base else "请选择保存目录")

    def refresh_account(self):
        self.account_status.set("会话已配置" if self.preferences.get("session") else "请配置 Apple Music 会话")

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
        settings.save(values)
        self.preferences = values
        self.fields["quality"].set(values["quality"])
        self.fields["output"].set(values["output"])
        self.refresh_account()

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

    def save_account(self, imported=None):
        try:
            values = self.snapshot()
            token = imported if imported is not None else self.token.get()
            if token.strip():
                values["session"] = settings.protect_session(token)
            elif not values.get("session"):
                raise ValueError("请输入会话令牌，或从 apmyx 导入。")
            self.store(values)
            self.token.set("")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法保存账户", error)

    def import_account(self):
        try:
            for folder in backend_locations()[1:]:
                if not (folder / "config.yaml").is_file():
                    continue
                imported = Engine(ROOT, folder).read_config()
                token = imported.get("media-user-token", "")
                if token:
                    self.fields["region"].set(imported.get("storefront") or self.fields["region"].get())
                    self.save_account(token)
                    return
            raise ValueError("本机 apmyx 配置中没有可导入的会话。")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法导入 apmyx", error)

    def clear_account(self):
        try:
            values = self.snapshot()
            values.pop("session", None)
            self.store(values)
            self.token.set("")
        except (OSError, RuntimeError, ValueError) as error:
            self.error("无法清除会话", error)

    def post(self, kind, value):
        self.messages.put((kind, value))

    def start_service(self):
        if self.service_busy:
            return
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
                engine.prepare_service(command, self.service_stop, lambda value: self.post("service", value))
            except (OSError, RuntimeError, ValueError) as error:
                self.post("service", str(error))
            finally:
                self.post("service_done", None)

        threading.Thread(target=run, daemon=True).start()

    def get_engine(self):
        if self.engine is None:
            self.engine = Engine(ROOT, find_backend())
        return self.engine

    def start_download(self):
        try:
            job = self.snapshot()
            if not job["folder"]:
                raise ValueError("请先选择保存目录。")
            destination_for(job["folder"], job["category"])
            job["links"] = read_links(self.links.get("1.0", "end"))
            if not job.get("session"):
                self.show("account")
                raise ValueError("请先配置 Apple Music 会话。")
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
            self.post("done", run_download(job, self.stop, self.post, engine))
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
        self.links.configure(state=state)
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
        self.service_stop.set()
        if self.busy:
            self.cancel()
        self.finish_close()

    def finish_close(self):
        if not self.closing or self.busy or self.service_busy:
            return False
        try:
            if self.engine is not None:
                self.engine.close_service()
        except (OSError, RuntimeError):
            pass
        self.window.destroy()
        return True

    def log_message(self, value):
        self.log.configure(state="normal")
        self.log.insert("end", value + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

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
            elif kind == "service":
                self.service_status.set(value)
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
        self.window.after(80, self.pump)


if __name__ == "__main__":
    window = tk.Tk()
    Studio(window)
    window.mainloop()
