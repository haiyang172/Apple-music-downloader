"""Preview current music-source declarations and capture one track's options."""

from pathlib import Path
from hashlib import sha256
import json
from queue import Empty, SimpleQueue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk

import audio
from apple_download import download_options
from storage import clean_name


def _size(value):
    if type(value) not in (int, float) or value <= 0:
        return "平台未提供"
    return f"{value / 1024 ** 2:.1f} MiB"


class MusicPreflight:
    def __init__(self, app, owner, row, choices):
        self.app, self.owner, self.row = app, owner, dict(row)
        self.source, self.choices = owner.page_source, dict(choices)
        self.session = owner.account.session
        self.stop = threading.Event()
        self.messages = SimpleQueue()
        self.serial = 0
        self.closed = False
        self.info = None
        self.poll_timer = self.start_timer = None
        self.path_state = None
        window = self.window = tk.Toplevel(app.window, background=app.background)
        window.withdraw()
        window.title("下载预览")
        window.transient(app.window)
        window.resizable(True, False)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", self.close)
        self.quality = tk.StringVar(window, app.source_quality_labels[self.source].get())
        self.output = tk.StringVar(window, app.output_label.get())
        self.bitrate = tk.StringVar(window, app.fields["mp3_bitrate"].get())
        self.message = tk.StringVar(window, "正在读取平台音轨信息…")
        self.source_info = tk.StringVar(window, "—")
        self.size_info = tk.StringVar(window, "—")
        self.output_size = tk.StringVar(window, "—")
        self.target = tk.StringVar(window)
        actions = app.panel(window, padding=(20, 12))
        actions.pack(side="bottom", fill="x", padx=12, pady=(0, 12))
        body = tk.Frame(window, background=app.background)
        body.pack(side="top", fill="both", expand=True, padx=12, pady=(12, 8))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, background=app.background, borderwidth=0, highlightthickness=0,
                                yscrollincrement=max(12, round(16 * app.scale)))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, command=self.canvas.yview, style="Music.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        card = app.panel(self.canvas, padding=(20, 17))
        self.body_item = self.canvas.create_window(0, 0, window=card, anchor="nw")
        self.canvas.bind("<Configure>", self.fit_body)
        card.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        card.columnconfigure(1, weight=1)
        app.label(card, self.row.get("title", "歌曲"), large=True, wrap=470).grid(
            row=0, column=0, columnspan=3, sticky="ew")
        app.label(card, self.row.get("artist") or "", muted=True, wrap=470).grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(4, 13))
        app.label(card, "来源音质").grid(row=2, column=0, sticky="w", padx=(0, 12))
        self.quality_box = ttk.Combobox(card, textvariable=self.quality, values=list(choices.values()),
                                        state="readonly", width=20)
        self.quality_box.grid(row=2, column=1, sticky="ew")
        self.quality_box.bind("<<ComboboxSelected>>", self.changed)
        self.refresh_button = app.action(card, "重新检查", self.inspect)
        self.refresh_button.grid(row=2, column=2, padx=(8, 0))
        app.label(card, variable=self.source_info, wrap=470).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(10, 3))
        app.label(card, variable=self.size_info, muted=True).grid(row=4, column=0, columnspan=3, sticky="w")
        app.label(card, "保存格式").grid(row=5, column=0, sticky="w", padx=(0, 12), pady=(16, 0))
        self.output_box = ttk.Combobox(card, textvariable=self.output, values=list(audio.OUTPUTS.values()),
                                       state="readonly", width=18)
        self.output_box.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(16, 0))
        self.output_box.bind("<<ComboboxSelected>>", self.update_output)
        self.bitrate_label = app.label(card, "MP3 码率")
        self.bitrate_label.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.bitrate_box = ttk.Combobox(card, textvariable=self.bitrate, values=audio.MP3_BITRATES,
                                       width=7, state="readonly")
        self.bitrate_box.grid(row=6, column=1, sticky="w", pady=(8, 0))
        self.bitrate_box.bind("<<ComboboxSelected>>", self.update_output)
        app.label(card, variable=self.output_size, muted=True).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(7, 0))
        app.label(card, variable=self.target, muted=True, wrap=470).grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=(13, 0))
        app.action(card, "保存目录", self.choose_folder).grid(row=8, column=2, padx=(8, 0), pady=(13, 0))
        app.label(card, variable=self.message, muted=True, wrap=470).grid(
            row=9, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        app.action(actions, "关闭", self.close).grid(row=0, column=0, sticky="w")
        self.queue_button = app.action(actions, "加入队列", lambda: self.enqueue(False))
        self.queue_button.grid(row=0, column=1, padx=(8, 0))
        self.download_button = app.action(actions, "下载", lambda: self.enqueue(True), primary=True)
        self.download_button.grid(row=0, column=2, padx=(8, 0))
        self.update_output()
        self.set_ready(False)
        window.update_idletasks()
        self.canvas.configure(height=min(card.winfo_reqheight(), round(430 * app.scale)))
        self.bind_scroll(card)
        window.update_idletasks()
        width = min(round(550 * app.scale), app.window.winfo_screenwidth() - 60)
        height = min(window.winfo_reqheight(), app.window.winfo_screenheight() - 80)
        window.minsize(min(round(500 * app.scale), width), min(round(250 * app.scale), height))
        window.geometry(f"{width}x{height}")
        window.deiconify()
        self.poll_timer = window.after(80, self.poll)
        self.start_timer = window.after(100, self.inspect)

    def fit_body(self, event):
        self.canvas.itemconfigure(self.body_item, width=event.width)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self.scroll, add="+")
        for child in widget.winfo_children():
            self.bind_scroll(child)

    def scroll(self, event):
        if event.delta:
            units = max(1, abs(event.delta) // 120)
            self.canvas.yview_scroll(-units if event.delta > 0 else units, "units")
        return "break"

    def valid(self):
        return not self.closed and not self.app.closing and self.owner.account.session == self.session

    def quality_key(self):
        return next(key for key, label in self.choices.items() if label == self.quality.get())

    def output_key(self):
        return next(key for key, label in audio.OUTPUTS.items() if label == self.output.get())

    def set_ready(self, ready):
        state = "normal" if ready else "disabled"
        self.queue_button.configure(state=state)
        self.download_button.configure(state=state)

    def changed(self, _event=None):
        self.stop.set()
        self.serial += 1
        self.info = None
        self.set_ready(False)
        self.refresh_button.configure(state="disabled")
        self.source_info.set("—")
        self.size_info.set("—")
        self.message.set("正在读取平台音轨信息…")
        if self.start_timer is not None:
            self.window.after_cancel(self.start_timer)
        self.start_timer = self.window.after(200, self.inspect)
        self.update_output()

    def inspect(self):
        if not self.valid():
            self.close()
            return
        if self.start_timer is not None:
            self.window.after_cancel(self.start_timer)
            self.start_timer = None
        self.stop.set()
        self.stop = stop = threading.Event()
        self.serial += 1
        serial, requested = self.serial, self.quality_key()
        self.info = None
        self.set_ready(False)
        self.refresh_button.configure(state="disabled")
        self.source_info.set("—")
        self.size_info.set("—")
        self.update_output()
        self.message.set("正在读取平台音轨信息…")
        source, url, duration = self.source, self.row["url"], self.row.get("duration")
        session, messages = self.session, self.messages

        def run():
            try:
                if source == "qq":
                    from qq_download import probe_qq as probe
                else:
                    from netease_download import probe_netease as probe
                status = lambda value: messages.put((serial, "status", value))
                result = probe(url, stop, status, session=session,
                               expected_duration=duration, source_quality=requested)
                messages.put((serial, "result", result))
            except (OSError, RuntimeError, ValueError) as error:
                messages.put((serial, "error", str(error)))
            except Exception as error:
                messages.put((serial, "error", f"无法读取音轨信息（{type(error).__name__}）。"))

        try:
            threading.Thread(target=run, daemon=True).start()
        except (OSError, RuntimeError) as error:
            self.message.set(f"无法开始检查：{error}")
            self.refresh_button.configure(state="normal")

    def poll(self):
        self.poll_timer = None
        if not self.valid():
            self.close()
            return
        while True:
            try:
                serial, kind, value = self.messages.get_nowait()
            except Empty:
                break
            if serial != self.serial:
                continue
            if kind == "result":
                self.refresh_button.configure(state="normal")
                self.info = value
                label = value.get("label") or value.get("codec") or "音轨"
                self.source_info.set(label if label.startswith("平台提供") else "平台提供 · " + label)
                self.size_info.set("源文件大小 · " + _size(value.get("size")))
                self.message.set("音轨规格将在下载后校验。")
                self.set_ready(True)
                self.update_output()
            elif kind == "error":
                self.refresh_button.configure(state="normal")
                self.message.set(value)
                self.info = None
                self.set_ready(False)
            else:
                self.message.set(value)
        state = (self.app.fields["folder"].get(), self.app.fields["category"].get(), self.output.get())
        if state != self.path_state:
            self.update_output()
        self.poll_timer = self.window.after(80, self.poll)

    def update_output(self, _event=None):
        output = self.output_key()
        for widget in (self.bitrate_label, self.bitrate_box):
            widget.grid() if output == "mp3" else widget.grid_remove()
        size = self.info.get("size") if self.info and output == "original" else None
        if output == "mp3" and self.info:
            seconds = self.info.get("duration")
            if type(seconds) in (int, float) and seconds > 0:
                size = seconds * int(self.bitrate.get()) * 1000 / 8
        self.output_size.set("预计音频大小 · 约 " + _size(size) if size else "输出大小将在保存后确认")
        folder, category = self.app.fields["folder"].get().strip(), self.app.fields["category"].get().strip()
        self.path_state = (self.app.fields["folder"].get(), self.app.fields["category"].get(), self.output.get())
        if not folder:
            self.target.set("请选择保存目录")
            return
        name = self.row.get("title") or "歌曲"
        if self.row.get("artist"):
            name = self.row["artist"] + " - " + name
        codec = self.info.get("codec") if self.info else None
        native_suffix = {"MP3": ".mp3", "FLAC": ".flac", "AAC": ".m4a", "AAC-LC": ".m4a"}.get(codec, ".音轨")
        suffix = {"aac": ".m4a", "original": native_suffix}.get(output, "." + output)
        if category and category != clean_name(category, 120):
            self.target.set("分类名称无效，请在设置中修改")
            return
        path = Path(folder) / category if category else Path(folder)
        self.target.set("预计保存 · " + str(path / (clean_name(name, 155) + suffix)))

    def choose_folder(self):
        self.app.browse_folder()
        self.update_output()

    def confirmed_row(self, options):
        row = dict(self.row, options=options)
        key, position = row.get("batch_key", ""), row.get("position") or 0
        match = next((task for task in self.app.tasks.values()
                      if (task.source, task.url, task.batch_key, task.position or 0)
                      == (self.source, row["url"], key, position)), None)
        if match is None:
            return row
        if match.options is None and match.state not in ("running", "complete"):
            for prepared in self.app.queue_store.prepare([match.id], options):
                self.app.render_task(prepared)
            return row

        def signature(values):
            values = values or {}
            legacy = "128" if self.source == "qq" else "legacy"
            result = {field: values.get(field) for field in ("folder", "category", "output", "volume")}
            result.update(music_sidecars=bool(values.get("music_sidecars")),
                          source_quality=values.get(f"{self.source}_quality", legacy))
            if values.get("output") == "mp3":
                result["mp3_bitrate"] = values.get("mp3_bitrate", audio.DEFAULT_MP3_BITRATE)
            return result

        if signature(match.options) != signature(options):
            encoded = json.dumps(signature(options), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            variant = sha256(encoded.encode("ascii")).hexdigest()[:20]
            row["batch_key"] = (key or self.source) + ":format:" + variant
        return row

    def enqueue(self, begin):
        if not self.valid() or self.info is None or self.info.get("requested") != self.quality_key():
            return
        if self.app.queue_adding:
            self.message.set("请等当前列表入队完成。")
            return
        try:
            choices = self.app.snapshot()
            if not choices["folder"]:
                self.choose_folder()
                choices = self.app.snapshot()
                if not choices["folder"]:
                    return
            choices.update(output=self.output_key(), mp3_bitrate=int(self.bitrate.get()))
            choices[f"{self.source}_quality"] = self.quality_key()
            options = download_options(choices)
            self.app.source_quality_labels[self.source].set(self.quality.get())
            self.app.output_label.set(self.output.get())
            self.app.fields["mp3_bitrate"].set(self.bitrate.get())
            self.app.store(choices)
            self.app.refresh_queue_options()
            row = self.confirmed_row(options)
            if self.app.enqueue_tasks([row], begin):
                self.close()
            else:
                self.message.set("请等当前列表入队完成。")
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            self.message.set(str(error))

    def close(self, _event=None):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        for timer in (self.poll_timer, self.start_timer):
            if timer is not None:
                self.window.after_cancel(timer)
        self.poll_timer = self.start_timer = None
        self.window.destroy()
        if getattr(self.app, "music_preflight_dialog", None) is self:
            self.app.music_preflight_dialog = None
