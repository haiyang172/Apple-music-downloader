"""Review and remove byte-identical duplicate audio files."""

import os
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from dedup_review import delete_duplicates, format_size, scan_folder
from preview_layout import PreviewLayout


class DedupReview:
    """Dialog that scans a folder and deletes only user-marked duplicate files."""

    def __init__(self, app, folder=""):
        self.app = app
        self.closed = self.busy = self.scanning = False
        self._close_when_idle = False
        self.messages = SimpleQueue()
        self.serial = 0
        self.stop = threading.Event()
        self.scan_stop = threading.Event()
        self.poll_timer = None
        self.groups = []
        self.rows = {}
        self.folder = tk.StringVar(app.window, folder)
        self.status = tk.StringVar(app.window, "选择目录后点「扫描」，重复文件按内容完全一致分组。")
        self.summary = tk.StringVar(app.window, "")

        self.layout = PreviewLayout(app, "曲库去重审阅", self.close, width=720)
        self.window = self.layout.window
        self.window.resizable(True, True)
        card, actions = self.layout.card, self.layout.actions
        card.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(card, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        toolbar.columnconfigure(0, weight=1)
        self.folder_entry = ttk.Entry(toolbar, textvariable=self.folder, font=(app.font, 12))
        self.folder_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = app.action(toolbar, "选择目录", self.choose_folder)
        self.browse_button.grid(row=0, column=1, padx=(8, 0))
        self.scan_button = app.action(toolbar, "扫描", self.start_scan, primary=True)
        self.scan_button.grid(row=0, column=2, padx=(6, 0))

        table = ttk.Frame(card, style="Card.TFrame")
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("mark", "path", "size", "group")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended",
                                 height=12, style="Queue.Treeview")
        for field, title, width in (("mark", "操作", 80), ("path", "文件", 360),
                                    ("size", "大小", 90), ("group", "分组", 80)):
            self.tree.heading(field, text=title, anchor="w")
            self.tree.column(field, width=width, minwidth=50, stretch=field == "path")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Button-1>", self._toggle)
        self.tree.bind("<space>", self._toggle)
        self.tree.bind("<Control-a>", self._select_all)

        app.label(card, variable=self.summary, muted=True, wrap=650).grid(row=2, column=0, sticky="ew", pady=(10, 0))

        actions.columnconfigure(0, weight=1)
        app.label(actions, variable=self.status, muted=True, wrap=600).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(0, 9))
        self.close_button = app.action(actions, "关闭", self.close)
        self.close_button.grid(row=1, column=0, sticky="w")
        self.cancel_button = app.action(actions, "取消", self.cancel)
        self.cancel_button.grid(row=1, column=1, padx=(8, 0))
        self.delete_button = app.action(actions, "删除所选重复文件", self.remove_marked, primary=True)
        self.delete_button.grid(row=1, column=2, sticky="e", padx=(8, 0))
        self._refresh_controls()

        self.layout.show()
        self.poll_timer = self.window.after(80, self.poll)
        if folder:
            self.start_scan()

    def choose_folder(self):
        if self.busy or self.scanning:
            return
        value = filedialog.askdirectory(parent=self.window, title="选择要审阅的目录",
                                        initialdir=self.folder.get() or str(Path.home()))
        if value:
            self.folder.set(value)
            self.start_scan()

    def start_scan(self):
        if self.closed or self.busy:
            return
        folder = Path(self.folder.get().strip() or "")
        if not folder.is_dir():
            self.status.set("请先选择一个存在的目录。")
            return
        self.scanning = True
        self.busy = True
        self.serial += 1
        serial = self.serial
        stop = threading.Event()
        self.scan_stop = stop
        self.stop = threading.Event()
        self.groups = []
        self.rows = {}
        self.tree.delete(*self.tree.get_children())
        self.summary.set("")
        self.status.set("正在扫描目录…")
        self._refresh_controls()

        def run():
            try:
                result = scan_folder(folder, stop=stop, on_status=lambda text: self.messages.put((serial, "status", text)))
                if not stop.is_set():
                    self.messages.put((serial, "scan_done", (result, "")))
            except Exception as error:
                self.messages.put((serial, "scan_done", (None, str(error))))

        self._start(run)

    def _start(self, target):
        try:
            threading.Thread(target=target, daemon=True).start()
        except (OSError, RuntimeError) as error:
            self.messages.put((self.serial, "scan_done", (None, f"无法启动扫描：{error}")))

    def _render(self, result):
        self.groups = result["groups"]
        for group_index, group in enumerate(self.groups, 1):
            for file_index, path in enumerate(group["files"]):
                keeper = file_index == 0
                row = self.tree.insert("", "end", values=(
                    "保留" if keeper else "删除", path, format_size(group["size"]), f"#{group_index}"))
                self.rows[row] = {"path": path, "fingerprint": group["fingerprint"],
                                  "keeper": keeper, "deleting": not keeper}
        self.summary.set(f"扫描 {result['scanned']} 个音频文件，发现 {result['duplicates']} 个可删除的重复文件。")

    def _toggle(self, event):
        if self.busy or self.scanning:
            return "break"
        row = self.tree.identify_row(event.y)
        if row and row in self.rows:
            item = self.rows[row]
            if not item["keeper"]:
                item["deleting"] = not item["deleting"]
                self.tree.set(row, "mark", "删除" if item["deleting"] else "保留")
                self._refresh_controls()
        return "break"

    def _select_all(self, _event=None):
        if not self.busy and not self.scanning:
            for row, item in self.rows.items():
                if not item["keeper"] and not item["deleting"]:
                    item["deleting"] = True
                    self.tree.set(row, "mark", "删除")
            self._refresh_controls()
        return "break"

    def remove_marked(self):
        if self.closed or self.busy:
            return
        entries = [(item["path"], item["fingerprint"]) for item in self.rows.values()
                   if item["deleting"] and not item["keeper"]]
        if not entries:
            self.status.set("请先勾选要删除的重复文件。")
            return
        self.busy = True
        self.serial += 1
        serial = self.serial
        self.stop = stop = threading.Event()
        self.status.set(f"正在删除 {len(entries)} 个重复文件…")
        self._refresh_controls()

        def run():
            try:
                removed, failures = delete_duplicates(entries, stop=stop,
                                                      on_status=lambda text: self.messages.put((serial, "status", text)))
                self.messages.put((serial, "delete_done", (removed, failures)))
            except Exception as error:
                self.messages.put((serial, "delete_done", (None, str(error))))

        self._start(run)

    def cancel(self):
        if self.busy:
            self.stop.set()
            self.scan_stop.set()
            self.status.set("正在取消…")

    def close(self, _event=None):
        if self.closed:
            return True
        if self.busy:
            self._close_when_idle = True
            self.cancel()
            return False
        self.closed = True
        self.serial += 1
        self.scan_stop.set()
        if self.poll_timer is not None:
            self.window.after_cancel(self.poll_timer)
            self.poll_timer = None
        self.window.destroy()
        if getattr(self.app, "dedup_review_dialog", None) is self:
            self.app.dedup_review_dialog = None
        return True

    def _refresh_controls(self):
        if self.closed or not hasattr(self, "delete_button"):
            return
        marked = sum(item["deleting"] and not item["keeper"] for item in self.rows.values())
        idle = not self.busy and not self.scanning
        self.scan_button.configure(state="normal" if idle else "disabled")
        self.browse_button.configure(state="normal" if idle else "disabled")
        self.folder_entry.configure(state="normal" if idle else "disabled")
        self.delete_button.configure(text=f"删除所选重复文件（{marked}）",
                                     state="normal" if idle and marked else "disabled")
        self.cancel_button.configure(state="normal" if self.busy and not self.stop.is_set() else "disabled")

    def poll(self):
        self.poll_timer = None
        if self.closed:
            return
        while True:
            try:
                serial, kind, value = self.messages.get_nowait()
            except Empty:
                break
            if serial != self.serial:
                continue
            if kind == "status":
                self.status.set(value)
            elif kind == "scan_done":
                result, error = value
                self.scanning = False
                self.busy = False
                if self._close_when_idle:
                    self.close()
                    return
                if error:
                    self.status.set(error)
                else:
                    self._render(result)
                    self.status.set("扫描完成。保留每组第一个文件，其余默认标记删除；点击行切换。")
            elif kind == "delete_done":
                removed, failures = value
                self.busy = False
                if self._close_when_idle:
                    self.close()
                    return
                if failures and isinstance(removed, list):
                    self.status.set(f"删除 {len(removed)} 个文件 · " + "；".join(failures))
                elif isinstance(removed, list):
                    self.status.set(f"已删除 {len(removed)} 个重复文件。")
                else:
                    self.status.set(str(failures))
                if isinstance(removed, list) and removed:
                    self.start_scan()
        self._refresh_controls()
        self.poll_timer = self.window.after(80, self.poll)
