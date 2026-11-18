"""Preview a Bilibili part, its audio naming and per-task output options."""

from hashlib import sha256
import json
from pathlib import Path
from queue import Empty, SimpleQueue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import audio
from apple_download import download_options, task_category
from bili_catalog import parse_video_link
from music_preflight_ui import _size
from preview_layout import PreviewLayout
from storage import clean_name


SOURCES = {"video": "视频 · H.264 / AAC", "audio": "兼容音轨 · AAC", "flac": "原始音轨 · FLAC"}


class BiliPreflight:
    def __init__(self, app, owner, row, formats):
        self.app, self.owner, self.row, self.formats = app, owner, dict(row), dict(formats)
        self.session = owner.account.session
        self.closed = False
        self.stop = threading.Event()
        self.messages = SimpleQueue()
        self.serial = 0
        self.poll_timer = self.start_timer = None
        self.info = None
        self.path_state = None
        choices = app.snapshot()
        initial_mode = choices.get("bili_mode", "video")
        initial_output = "mp3" if initial_mode == "mp3" else choices.get("output", "original")
        initial_mode = "audio" if initial_mode == "mp3" else initial_mode
        self.layout = PreviewLayout(app, "B 站下载预览", self.close)
        self.window = self.layout.window
        card, actions = self.layout.card, self.layout.actions
        card.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(self.window, SOURCES[initial_mode])
        self.output = tk.StringVar(self.window, audio.OUTPUTS[initial_output])
        self.bitrate = tk.StringVar(self.window, str(choices["mp3_bitrate"]))
        self.initial_song = owner._audio_title(row) or ""
        self.initial_artist = owner._artist(row) or ""
        self.song = tk.StringVar(self.window, self.initial_song)
        self.artist = tk.StringVar(self.window, self.initial_artist)
        self.category = tk.StringVar(self.window, choices["category"])
        self.auto_category = tk.BooleanVar(self.window, choices.get("bili_auto_category", True))
        self.attachments = {field: tk.BooleanVar(self.window, choices.get(field, False))
                            for field in ("bili_danmaku", "bili_subtitle", "bili_archive")}
        self.specification = tk.StringVar(self.window, "—")
        self.size = tk.StringVar(self.window, "—")
        self.target = tk.StringVar(self.window)
        self.message = tk.StringVar(self.window, "正在读取平台音轨信息…")
        app.label(card, row.get("title") or "视频分 P", large=True, wrap=470).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 13))
        app.label(card, "来源").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.mode_box = ttk.Combobox(card, textvariable=self.mode, values=list(SOURCES.values()),
                                     state="readonly", width=23)
        self.mode_box.grid(row=1, column=1, sticky="ew")
        self.mode_box.bind("<<ComboboxSelected>>", self.changed)
        self.refresh_button = app.action(card, "重新检查", self.inspect)
        self.refresh_button.grid(row=1, column=2, padx=(8, 0))
        app.label(card, variable=self.specification, wrap=470).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        app.label(card, variable=self.size, muted=True).grid(row=3, column=0, columnspan=3, sticky="w")
        self.audio_controls = ttk.Frame(card, style="Card.TFrame")
        self.audio_controls.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.audio_controls.columnconfigure(1, weight=1)
        app.label(self.audio_controls, "保存格式").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.output_box = ttk.Combobox(self.audio_controls, textvariable=self.output,
                                       values=list(audio.OUTPUTS.values()), state="readonly", width=20)
        self.output_box.grid(row=0, column=1, sticky="ew")
        self.output_box.bind("<<ComboboxSelected>>", self.update_target)
        self.bitrate_label = app.label(self.audio_controls, "MP3 码率")
        self.bitrate_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.bitrate_box = ttk.Combobox(self.audio_controls, textvariable=self.bitrate,
                                        values=audio.MP3_BITRATES, state="readonly", width=7)
        self.bitrate_box.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.bitrate_box.bind("<<ComboboxSelected>>", self.update_target)
        app.label(self.audio_controls, "歌名").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self.song_entry = ttk.Entry(self.audio_controls, textvariable=self.song)
        self.song_entry.grid(row=2, column=1, sticky="ew", pady=(12, 0))
        app.label(self.audio_controls, "歌手").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.artist_entry = ttk.Entry(self.audio_controls, textvariable=self.artist)
        self.artist_entry.grid(row=3, column=1, sticky="ew", pady=(8, 0))
        for entry in (self.song_entry, self.artist_entry):
            entry.bind("<KeyRelease>", self.update_target)
        app.label(card, "分类").grid(row=5, column=0, sticky="w", pady=(14, 0))
        self.category_entry = ttk.Entry(card, textvariable=self.category)
        self.category_entry.grid(row=5, column=1, sticky="ew", pady=(14, 0))
        self.category_entry.bind("<KeyRelease>", self.update_target)
        ttk.Checkbutton(card, text="自动分类", variable=self.auto_category, command=self.update_target,
                        style="Card.TCheckbutton").grid(row=5, column=2, padx=(8, 0), pady=(14, 0))
        extras = ttk.Frame(card, style="Card.TFrame")
        extras.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        for field, title in (("bili_danmaku", "弹幕"), ("bili_subtitle", "字幕"), ("bili_archive", "封面和资料")):
            ttk.Checkbutton(extras, text=title, variable=self.attachments[field], style="Card.TCheckbutton").pack(
                side="left", padx=(0, 12))
        app.label(card, variable=self.target, muted=True, wrap=470).grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        app.action(card, "保存目录", self.choose_folder).grid(row=7, column=2, padx=(8, 0), pady=(14, 0))
        app.label(card, variable=self.message, muted=True, wrap=470).grid(
            row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        app.action(actions, "关闭", self.close).grid(row=0, column=0, sticky="w")
        self.queue_button = app.action(actions, "加入队列", lambda: self.enqueue(False))
        self.queue_button.grid(row=0, column=1, padx=(8, 0))
        self.download_button = app.action(actions, "下载", lambda: self.enqueue(True), primary=True)
        self.download_button.grid(row=0, column=2, padx=(8, 0))
        self.update_target()
        self.set_ready(False)
        self.layout.show()
        self.poll_timer = self.window.after(80, self.poll)
        self.start_timer = self.window.after(100, self.inspect)

    def mode_key(self):
        return next(key for key, label in SOURCES.items() if label == self.mode.get())

    def output_key(self):
        return next(key for key, label in audio.OUTPUTS.items() if label == self.output.get())

    def valid(self):
        return not self.closed and not self.app.closing and self.owner.account.session == self.session

    def set_ready(self, ready):
        state = "normal" if ready else "disabled"
        self.queue_button.configure(state=state)
        self.download_button.configure(state=state)

    def changed(self, _event=None):
        self.stop.set()
        self.serial += 1
        self.info = None
        self.set_ready(False)
        self.specification.set("—")
        self.size.set("—")
        self.refresh_button.configure(state="disabled")
        self.message.set("正在读取平台音轨信息…")
        if self.start_timer is not None:
            self.window.after_cancel(self.start_timer)
        self.start_timer = self.window.after(200, self.inspect)
        self.update_target()

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
        serial, mode = self.serial, self.mode_key()
        session, url, messages = self.session, self.row["url"], self.messages
        self.info = None
        self.set_ready(False)
        self.refresh_button.configure(state="disabled")
        self.specification.set("—")
        self.size.set("—")
        self.message.set("正在读取平台音轨信息…")

        def run():
            try:
                from bili_download import probe_video
                result = probe_video(url, mode, stop, lambda text: messages.put((serial, "status", text)), session=session)
                messages.put((serial, "result", result))
            except (OSError, RuntimeError, ValueError) as error:
                messages.put((serial, "error", str(error)))
            except Exception as error:
                messages.put((serial, "error", f"无法读取播放信息（{type(error).__name__}）。"))

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
                self.info = value
                label = "平台提供 · " + value["label"]
                if value.get("width") and value.get("height"):
                    label += f" · {value['width']} × {value['height']}"
                self.specification.set(label)
                self.size.set("音视频源文件大小 · " + _size(value.get("size")))
                self.message.set(value.get("warning") or "音轨规格将在下载后校验。")
                self.set_ready(True)
                self.refresh_button.configure(state="normal")
                self.update_target()
            elif kind == "error":
                self.info = None
                self.set_ready(False)
                self.refresh_button.configure(state="normal")
                self.message.set(value)
            else:
                self.message.set(value)
        if self.path_state != self.app.fields["folder"].get():
            self.update_target()
        self.poll_timer = self.window.after(80, self.poll)

    def edited_row(self):
        row = dict(self.row)
        song, artist = self.song.get().strip(), self.artist.get().strip()
        if self.mode_key() != "video" and (song, artist) != (self.initial_song, self.initial_artist):
            row.update(audio_title=song, artist=artist, song_edited=True)
        return row

    def selected_options(self, choices):
        result = dict(choices, bili_mode=self.mode_key(),
                      bili_output="original" if self.mode_key() == "video" else self.output_key(),
                      mp3_bitrate=int(self.bitrate.get()), category=self.category.get().strip(),
                      bili_auto_category=self.auto_category.get(), bili_category_rules=2)
        result.update({field: value.get() for field, value in self.attachments.items()})
        return result

    def update_target(self, _event=None):
        mode, output = self.mode_key(), self.output_key()
        if mode == "video":
            self.audio_controls.grid_remove()
        else:
            self.audio_controls.grid()
        for widget in (self.bitrate_label, self.bitrate_box):
            widget.grid() if output == "mp3" else widget.grid_remove()
        folder = self.app.fields["folder"].get().strip()
        self.path_state = self.app.fields["folder"].get()
        if not folder:
            self.target.set("请选择保存目录")
            return
        category = self.category.get().strip()
        if category and category != clean_name(category, 120):
            self.target.set("分类只能使用一级有效文件夹名")
            return
        row = self.edited_row()
        record = {"source": "bilibili", "title": "", "source_category": "", "media_category": "",
                  "batch_key": "", "batch_title": "", **row, "options": self.selected_options({})}
        target_category = task_category(SimpleNamespace(**record))
        name = row.get("title") or "视频"
        video_id, part = parse_video_link(row["url"])
        if (row.get("total", 0) and row["total"] > 1
                and row.get("batch_key", "").startswith(f"bilibili:{video_id}") and part is not None):
            name += f" [P{part}]"
        song = self.song.get().strip()
        if mode != "video" and song:
            artist = self.artist.get().strip()
            name = f"{artist} - {song}" if artist else song
        suffix = ".mp4" if mode == "video" else (
            {"audio": ".m4a", "flac": ".flac"}[mode] if output == "original" else
            {"aac": ".m4a"}.get(output, "." + output))
        path = Path(folder) / target_category if target_category else Path(folder)
        self.target.set("预计保存 · " + str(path / (clean_name(name, 155) + suffix)))

    def choose_folder(self):
        self.app.browse_folder()
        self.update_target()

    def confirmed_row(self, row, options):
        row = dict(row, options=options)
        key, position = row.get("batch_key", ""), row.get("position") or 0
        match = next((task for task in self.app.tasks.values()
                      if (task.source, task.url, task.batch_key, task.position or 0)
                      == ("bilibili", row["url"], key, position)), None)
        if match is None:
            return row
        fields = ("folder", "category", "volume", "bili_mode", "bili_output", "bili_danmaku", "bili_subtitle",
                  "bili_archive", "bili_auto_category", "bili_category_rules")

        def signature(values, title, artist, edited):
            values = values or {}
            chosen = {field: values.get(field) for field in fields}
            chosen["bili_output"] = values.get("bili_output", "original")
            if chosen["bili_output"] == "mp3":
                chosen["mp3_bitrate"] = values.get("mp3_bitrate", audio.DEFAULT_MP3_BITRATE)
            return [chosen, title, artist, bool(edited)]

        old = signature(match.options, match.audio_title, match.artist, match.song_edited)
        new = signature(options, row.get("audio_title"), row.get("artist", ""), row.get("song_edited"))
        if match.options is None and match.state != "running" and match.state != "complete" and not match.files:
            for task in self.app.queue_store.prepare([match.id], options):
                self.app.render_task(task)
        elif old != new:
            encoded = json.dumps(new, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            row["batch_key"] = (key or "bilibili") + ":format:" + sha256(encoded.encode("ascii")).hexdigest()[:20]
        return row

    def enqueue(self, begin):
        if not self.valid() or self.info is None or self.info.get("mode") != self.mode_key():
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
            selected = self.selected_options(choices)
            options = download_options(selected)
            row = self.confirmed_row(self.edited_row(), options)
            # The main toolbar keeps its native-format presets; conversion is
            # captured explicitly by this task and the common audio preferences.
            saved = dict(selected, bili_output="original")
            if self.mode_key() != "video":
                saved["output"] = self.output_key()
                self.app.output_label.set(self.output.get())
            self.app.bili_format.set(self.formats[self.mode_key()])
            self.app.fields["mp3_bitrate"].set(self.bitrate.get())
            self.app.store(saved)
            self.app.refresh_queue_options()
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
        if getattr(self.app, "bili_preflight_dialog", None) is self:
            self.app.bili_preflight_dialog = None
