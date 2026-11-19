"""Choose a local media conversion and hand each file to the shared queue."""

import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from local_convert import formats_for


FORMATS = {
    "mp3": "MP3", "m4a": "M4A（AAC）", "flac": "FLAC", "wav": "WAV", "opus": "Opus",
    "mp4": "MP4", "mkv": "MKV", "webm": "WebM",
}


class LocalConverter:
    def __init__(self, app, page):
        self.app = app
        self.files = {}
        self.folder = tk.StringVar(page)
        self.format = tk.StringVar(page)
        self.status = tk.StringVar(page, "拖入音频或视频文件，或点「添加文件」。")

        page.rowconfigure(1, weight=1)
        header = ttk.Frame(page, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        app.label(header, "本地转码", large=True, background=app.background).grid(row=0, column=0, sticky="w")
        app.action(header, "添加文件", self.choose_files).grid(row=0, column=1, padx=(8, 0))
        app.action(header, "编辑标签", self.edit_tags).grid(row=0, column=2, padx=(8, 0))
        app.action(header, "去重审阅", lambda: self.app.open_dedup_review()).grid(row=0, column=3, padx=(8, 0))

        card = app.panel(page)
        card.grid(row=1, column=0, sticky="nsew")
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(card, columns=("name", "type"), show="headings",
                                 selectmode="extended", style="Queue.Treeview")
        self.tree.heading("name", text="文件名", anchor="w")
        self.tree.heading("type", text="类型", anchor="center")
        self.tree.column("name", width=320, minwidth=140)
        self.tree.column("type", width=round(90 * app.scale), minwidth=60, stretch=False, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(card, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Delete>", lambda _: self.remove_selected())
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_formats())

        options = ttk.Frame(card, style="MusicToolbar.TFrame")
        options.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        options.columnconfigure(2, weight=1)
        self.format_box = ttk.Combobox(options, textvariable=self.format, state="disabled", width=13)
        self.format_box.grid(row=0, column=0, sticky="w")
        app.action(options, "保存位置", self.choose_folder).grid(row=0, column=1, padx=7)
        self.path_text = tk.StringVar(page, "原文件所在目录")
        ttk.Label(options, textvariable=self.path_text, style="MusicStatus.TLabel", width=1).grid(
            row=0, column=2, sticky="ew", padx=(0, 7))
        app.action(options, "同目录", lambda: self._set_folder("")).grid(row=0, column=3)

        foot = ttk.Frame(page, style="Surface.TFrame")
        foot.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        foot.columnconfigure(0, weight=1)
        app.label(foot, variable=self.status, background=app.background, muted=True, wrap=350).grid(
            row=0, column=0, sticky="w")
        app.action(foot, "移除所选", self.remove_selected).grid(row=0, column=1, padx=7)
        self.start_button = app.action(foot, "开始转码", self.start, primary=True)
        self.start_button.grid(row=0, column=2)
        self._update_formats()

    def choose_files(self):
        paths = filedialog.askopenfilenames(parent=self.tree, title="添加本地影音文件")
        if paths:
            self.add_files(paths)

    def edit_tags(self):
        paths = [path for path, _ in self._selected_files()
                 if path.suffix.lower() in {".mp3", ".m4a", ".flac", ".wav", ".opus"}]
        self.app.edit_audio_tags(paths)

    def add_files(self, paths):
        known = {os.path.normcase(str(path)) for path, _ in self.files.values()}
        added, skipped = [], 0
        for value in paths:
            path = Path(os.path.abspath(value))
            key = os.path.normcase(str(path))
            formats = formats_for(path) if path.is_file() else set()
            if not formats:
                skipped += 1
            elif key not in known:
                row = self.tree.insert("", "end", values=(path.name, path.suffix.upper().lstrip(".")))
                self.files[row] = (path, set(formats))
                known.add(key)
                added.append(row)
        if added:
            self.tree.selection_set(tuple(added))
            self.tree.see(added[0])
        summary = f"{len(self.files)} 个文件" if self.files else "请选择支持的音频或视频文件。"
        if skipped:
            summary += f" · 跳过 {skipped} 个不支持的项目"
        self.status.set(summary)
        self._update_formats()
        return bool(added)

    def _selected_files(self):
        selected = self.tree.selection() or self.tree.get_children()
        return [self.files[row] for row in selected]

    def _format_keys(self):
        common = set(FORMATS)
        selected = self._selected_files()
        for _, options in selected:
            common.intersection_update(options)
        return [key for key in FORMATS if key in common] if selected else []

    def _update_formats(self):
        labels = [FORMATS[key] for key in self._format_keys()]
        self.format_box.configure(values=labels, state="readonly" if labels else "disabled")
        if self.format.get() not in labels:
            self.format.set(labels[0] if labels else "")
        self.start_button.configure(state="normal" if labels else "disabled")

    def _select_all(self, _event):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def remove_selected(self):
        for row in self.tree.selection():
            del self.files[row]
            self.tree.delete(row)
        self.status.set(f"{len(self.files)} 个文件")
        self._update_formats()

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.tree, title="选择转码保存位置", initialdir=self.folder.get() or None)
        if folder:
            self._set_folder(folder)

    def _set_folder(self, folder):
        self.folder.set(folder)
        self.path_text.set(folder or "原文件所在目录")

    def quick_actions(self):
        keys = self._format_keys()
        if not keys:
            return
        menu = tk.Menu(self.tree, tearoff=False)
        for key in keys:
            menu.add_command(label=FORMATS[key], command=lambda selected=key: self._choose_and_start(selected))
        try:
            menu.tk_popup(*self.tree.winfo_pointerxy())
        finally:
            menu.grab_release()

    def _choose_and_start(self, key):
        self.format.set(FORMATS[key])
        self.start()

    def start(self):
        key = next((key for key in self._format_keys() if FORMATS[key] == self.format.get()), None)
        if key:
            paths = [path for path, _ in self._selected_files()]
            count = self.app.queue_local(paths, key, self.folder.get())
            if count:
                self.status.set(f"已加入 {count} 个转码任务")
