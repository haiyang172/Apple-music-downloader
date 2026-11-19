"""Explicit local audio tag edits with durable history and undo."""

import os
from pathlib import Path
from queue import Empty, SimpleQueue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from preview_layout import PreviewLayout


FIELDS = {
    "title": "歌名", "artist": "歌手", "album": "专辑", "genre": "流派",
    "date": "年份", "tracknumber": "音轨",
}
STATES = {
    "applied": "已修改", "undone": "已撤销", "cancelled": "已取消",
    "rolled_back": "已还原", "failed": "未完成", "conflict": "文件已变化",
}
SUPPORTED = frozenset((".mp3", ".m4a", ".flac", ".wav", ".opus"))


class TagEditor:
    def __init__(self, app, store, paths=()):
        self.app, self.store = app, store
        self.closed = self.busy = self.scanning = self.history_loading = False
        self._close_when_idle = False
        self.serial = 0
        self.messages = SimpleQueue()
        self.stop = threading.Event()
        self.scan_stop = threading.Event()
        self.history_stop = threading.Event()
        self.poll_timer = None
        self.scan_message = None
        self.files, self.file_paths, self.history = {}, {}, {}
        self.layout = PreviewLayout(app, "本地音频标签", self.close, width=760)
        self.window = self.layout.window
        self.window.resizable(True, True)
        card, actions = self.layout.card, self.layout.actions
        card.columnconfigure(0, weight=1)
        self.status = tk.StringVar(self.window, "添加音频文件，选择需要修改的字段。")
        self.selection_text = tk.StringVar(self.window, "尚未选择文件")
        self.history_detail = tk.StringVar(self.window, "历史修改会保留备份，关闭后仍可撤销。")
        self.values = {field: tk.StringVar(self.window) for field in FIELDS}
        self.enabled = {field: tk.BooleanVar(self.window, False) for field in FIELDS}
        self.hints = {field: tk.StringVar(self.window) for field in FIELDS}
        self.entries, self.checks = {}, {}
        self.notebook = ttk.Notebook(card)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.edit_page = ttk.Frame(self.notebook, style="Card.TFrame", padding=(0, 12))
        self.history_page = ttk.Frame(self.notebook, style="Card.TFrame", padding=(0, 12))
        self.notebook.add(self.edit_page, text="修改标签")
        self.notebook.add(self.history_page, text="修改历史")
        self.notebook.bind("<<NotebookTabChanged>>", self._refresh_controls)
        self._build_edit()
        self._build_history()
        actions.columnconfigure(0, weight=1)
        self.status_label = app.label(actions, variable=self.status, muted=True, wrap=660)
        self.status_label.configure(height=2)
        self.status_label.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 9))
        self.close_button = app.action(actions, "关闭", self.close)
        self.close_button.grid(row=1, column=0, sticky="w")
        self.cancel_button = app.action(actions, "取消操作", self.cancel)
        self.cancel_button.grid(row=1, column=1, padx=(8, 0))
        self.apply_button = app.action(actions, "应用到 0 个文件", self.apply, primary=True)
        self.apply_button.grid(row=1, column=2, columnspan=2, sticky="e", padx=(8, 0))
        self.undo_button = app.action(actions, "撤销所选批次", self.undo, primary=True)
        self.undo_button.grid(row=1, column=2, columnspan=2, sticky="e", padx=(8, 0))
        self._refresh_controls()
        self.layout.show()
        self.poll_timer = self.window.after(80, self.poll)
        self.refresh_history()
        if paths:
            self.add_files(paths)

    def _build_edit(self):
        page, app = self.edit_page, self.app
        page.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(page, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        toolbar.columnconfigure(0, weight=1)
        app.label(toolbar, variable=self.selection_text, wrap=420).grid(row=0, column=0, sticky="w")
        self.add_button = app.action(toolbar, "添加文件", self.choose_files)
        self.add_button.grid(row=0, column=1, padx=(8, 0))
        self.remove_button = app.action(toolbar, "移除所选", self.remove_selected)
        self.remove_button.grid(row=0, column=2, padx=(8, 0))
        table = ttk.Frame(page, style="Card.TFrame")
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        columns = ("name", "title", "artist", "album")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended",
                                 height=7, style="Queue.Treeview")
        for field, title in zip(columns, ("文件名", "歌名", "歌手", "专辑")):
            self.tree.heading(field, text=title, anchor="w")
            self.tree.column(field, width=170 if field == "name" else 140, minwidth=75)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, command=self.tree.yview, style="Music.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, command=self.tree.xview, orient="horizontal")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Delete>", lambda _event: self.remove_selected())
        form = ttk.Frame(page, style="Card.TFrame")
        form.grid(row=2, column=0, sticky="ew", pady=(13, 0))
        form.columnconfigure(1, weight=1)
        for row, (field, title) in enumerate(FIELDS.items()):
            self.checks[field] = ttk.Checkbutton(form, text=title, variable=self.enabled[field],
                                                command=self._refresh_controls, style="Card.TCheckbutton")
            self.checks[field].grid(row=row, column=0, sticky="w", padx=(0, 12), pady=(0, 7))
            self.entries[field] = ttk.Entry(form, textvariable=self.values[field])
            self.entries[field].grid(row=row, column=1, sticky="ew", pady=(0, 7))
            app.label(form, variable=self.hints[field], muted=True, wrap=100).grid(
                row=row, column=2, sticky="w", padx=(9, 0), pady=(0, 7))
        app.label(page, "只修改勾选字段；留空清除。文件名保持不变，可在历史中撤销。", muted=True,
                  wrap=650).grid(row=3, column=0, sticky="ew", pady=(3, 0))

    def _build_history(self):
        page, app = self.history_page, self.app
        page.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(page, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        toolbar.columnconfigure(0, weight=1)
        app.label(toolbar, "每次应用为一个批次，可从备份还原原文件。", muted=True, wrap=490).grid(
            row=0, column=0, sticky="w")
        self.history_button = app.action(toolbar, "刷新历史", self.refresh_history)
        self.history_button.grid(row=0, column=1, padx=(8, 0))
        table = ttk.Frame(page, style="Card.TFrame")
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        columns = ("time", "count", "state", "batch")
        self.history_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse",
                                         height=10, style="Queue.Treeview")
        for field, title, width in (("time", "时间", 180), ("count", "文件数", 65),
                                    ("state", "状态", 95), ("batch", "批次", 135)):
            self.history_tree.heading(field, text=title, anchor="w")
            self.history_tree.column(field, width=width, minwidth=55, stretch=field in ("time", "batch"))
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, command=self.history_tree.yview, style="Music.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.history_tree.configure(yscrollcommand=scroll.set)
        self.history_tree.bind("<<TreeviewSelect>>", self._history_selected)
        app.label(page, variable=self.history_detail, muted=True, wrap=650).grid(
            row=2, column=0, sticky="ew", pady=(12, 0))

    def _selected_files(self):
        return [(row, self.files[row]) for row in self.tree.selection() if self.files.get(row) is not None]

    def _select_all(self, _event=None):
        if not self.busy:
            self.tree.selection_set(self.tree.get_children())
        return "break"

    def _selection_changed(self, _event=None):
        if self.busy:
            return
        selected = self._selected_files()
        self.selection_text.set(f"已选 {len(selected)} 个文件 · 共 {len(self.file_paths)} 个")
        for field in FIELDS:
            values = {str(info.get(field) or "") for _, info in selected}
            self.values[field].set(next(iter(values)) if len(values) == 1 else "")
            self.hints[field].set("各文件不同" if len(values) > 1 else "")
            self.enabled[field].set(False)
        self._refresh_controls()

    def _history_selected(self, _event=None):
        selected = self.history_tree.selection()
        record = self.history.get(selected[0]) if selected else None
        if record is None:
            self.history_detail.set("历史修改会保留备份，关闭后仍可撤销。")
        elif record.get("error"):
            self.history_detail.set(str(record["error"]))
        elif record.get("state") == "applied":
            self.history_detail.set("可还原此批次的原文件。若文件在修改后再次变化，撤销会停止以保护现有内容。")
        else:
            self.history_detail.set("此批次当前无法撤销。")
        self._refresh_controls()

    def _refresh_controls(self, _event=None):
        if self.closed or not hasattr(self, "apply_button"):
            return
        selected = self._selected_files()
        count = len(selected)
        editable = bool(count and not self.busy and not self.scanning)
        for field in FIELDS:
            self.checks[field].configure(state="normal" if editable else "disabled")
            self.entries[field].configure(state="normal" if editable and self.enabled[field].get() else "disabled")
        self.add_button.configure(state="disabled" if self.busy or self.scanning else "normal")
        self.remove_button.configure(state="normal" if self.tree.selection() and not self.busy and not self.scanning else "disabled")
        self.history_button.configure(state="disabled" if self.busy or self.history_loading else "normal")
        self.cancel_button.configure(state="normal" if self.busy and not self.stop.is_set() else "disabled")
        self.apply_button.configure(text=f"应用到 {count} 个文件",
                                    state="normal" if editable and any(value.get() for value in self.enabled.values()) else "disabled")
        selected_history = self.history_tree.selection()
        record = self.history.get(selected_history[0]) if selected_history else None
        self.undo_button.configure(state="normal" if not self.busy and not self.scanning and not self.history_loading
                                   and record and record.get("state") == "applied" else "disabled")
        if self.notebook.select() == str(self.history_page):
            self.apply_button.grid_remove()
            self.undo_button.grid()
        else:
            self.undo_button.grid_remove()
            self.apply_button.grid()

    def choose_files(self):
        if self.closed or self.busy or self.scanning:
            return
        paths = filedialog.askopenfilenames(parent=self.window, title="添加音频文件",
                                            filetypes=(("音频文件", "*.mp3 *.m4a *.flac *.wav *.opus"), ("所有文件", "*.*")))
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        if self.closed or self.busy or self.scanning:
            return False
        known = {os.path.normcase(value) for value in self.file_paths.values()}
        added, skipped = [], 0
        for value in paths:
            path = Path(os.path.abspath(value))
            key = os.path.normcase(str(path))
            if path.suffix.lower() not in SUPPORTED:
                skipped += 1
            elif key not in known:
                row = self.tree.insert("", "end", values=(path.name, "正在读取…", "", ""))
                self.files[row], self.file_paths[row] = None, str(path)
                known.add(key)
                added.append((row, str(path)))
        if added:
            self.tree.selection_set(self.tree.get_children())
            self._scan(added)
        elif skipped:
            self.status.set("支持 MP3、M4A、FLAC、WAV 和 Opus 音频文件。")
        self._selection_changed()
        return bool(added)

    def remove_selected(self):
        if self.closed or self.busy or self.scanning:
            return
        for row in self.tree.selection():
            self.files.pop(row, None)
            self.file_paths.pop(row, None)
            self.tree.delete(row)
        self._selection_changed()

    def _scan(self, paths, *, final_message=None):
        self.scanning = True
        self.scan_message = final_message
        self.scan_stop = stop = threading.Event()
        store, messages, serial = self.store, self.messages, self.serial
        self.status.set(f"正在读取 {len(paths)} 个文件…")
        self._refresh_controls()

        def run():
            failures = 0
            for row, path in paths:
                if stop.is_set():
                    break
                try:
                    info = store.read_audio(path)
                    messages.put((serial, "file", (row, info, "")))
                except Exception as error:
                    failures += 1
                    messages.put((serial, "file", (row, None, str(error))))
            messages.put((serial, "scan_done", failures))

        self._start_thread(run, "scan_done")

    def refresh_history(self):
        if self.closed or self.busy or self.history_loading:
            return
        self.history_loading = True
        self.history_stop = stop = threading.Event()
        store, messages, serial = self.store, self.messages, self.serial
        self._refresh_controls()

        def run():
            try:
                records = store.list_history()
                if not stop.is_set():
                    messages.put((serial, "history", (records, "")))
            except Exception as error:
                messages.put((serial, "history", ([], str(error))))

        self._start_thread(run, "history")

    def apply(self):
        if self.closed or self.busy or self.scanning:
            return
        changes = {field: value.get() for field, value in self.values.items() if self.enabled[field].get()}
        edits = [{"path": info["path"], "changes": dict(changes), "expected": info["fingerprint"]}
                 for _, info in self._selected_files()]
        if changes and edits:
            self._write("apply", edits)

    def undo(self):
        if self.closed or self.busy or self.scanning or self.history_loading:
            return
        selected = self.history_tree.selection()
        record = self.history.get(selected[0]) if selected else None
        if record and record.get("state") == "applied":
            self._write("undo", record["id"])

    def _write(self, operation, payload):
        self.serial += 1
        self.history_stop.set()
        self.history_loading = False
        self.busy = True
        self.stop = stop = threading.Event()
        store, messages, serial = self.store, self.messages, self.serial
        self.status.set("正在修改标签并保存备份…" if operation == "apply" else "正在从备份还原原文件…")
        self._refresh_controls()

        def run():
            try:
                status = lambda value: messages.put((serial, "status", str(value)))
                result = getattr(store, operation)(payload, stop=stop, on_status=status)
                messages.put((serial, "write_done", (operation, result, "")))
            except Exception as error:
                message = str(error) or ("操作已取消。" if stop.is_set() else "无法完成标签修改。")
                messages.put((serial, "write_done", (operation, getattr(error, "record", None), message)))

        self._start_thread(run, "write_done", operation)

    def _start_thread(self, target, result_kind, operation=""):
        try:
            threading.Thread(target=target, daemon=True).start()
        except (OSError, RuntimeError) as error:
            if result_kind == "write_done":
                value = (operation, None, f"无法开始操作：{error}")
            elif result_kind == "history":
                value = ([], f"无法读取历史：{error}")
            else:
                value = len(self.file_paths)
            self.messages.put((self.serial, result_kind, value))

    def _render_history(self, records):
        selected = self.history_tree.selection()
        previous = selected[0] if selected else None
        self.history_tree.delete(*self.history_tree.get_children())
        self.history.clear()
        for record in sorted(records, key=lambda item: item.get("created_at", ""), reverse=True):
            batch = str(record["id"])
            self.history[batch] = record
            stamp = str(record.get("created_at", "")).replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC")
            self.history_tree.insert("", "end", iid=batch,
                                     values=(stamp, len(record.get("items") or []),
                                             STATES.get(record.get("state"), record.get("state", "—")), batch[:10]))
        selected = previous if previous in self.history else next(
            (batch for batch, record in self.history.items() if record.get("state") == "applied"),
            next(iter(self.history), None))
        if selected:
            self.history_tree.selection_set(selected)
        self._history_selected()

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
            if kind == "file":
                row, info, error = value
                if row in self.file_paths:
                    self.files[row] = info
                    values = (Path(self.file_paths[row]).name, "无法读取", "", "") if error else (
                        Path(info["path"]).name, info.get("title", ""), info.get("artist", ""), info.get("album", ""))
                    self.tree.item(row, values=values)
                    if error:
                        self.status.set(error)
            elif kind == "scan_done":
                self.scanning = False
                self._selection_changed()
                message = self.scan_message or f"已读取 {sum(info is not None for info in self.files.values())} 个文件"
                self.status.set(message + (f" · {value} 个文件无法读取" if value else
                                          "" if self.scan_message else " · 勾选需要修改的字段后应用。"))
                self.scan_message = None
            elif kind == "history":
                self.history_loading = False
                records, error = value
                self._render_history(records)
                if error:
                    self.status.set(error)
            elif kind == "status":
                self.status.set(value)
            elif kind == "write_done":
                self.busy = False
                operation, record, error = value
                if self._close_when_idle:
                    self._destroy()
                    return
                if error:
                    message = error
                else:
                    count = len((record or {}).get("items") or [])
                    state = (record or {}).get("state")
                    if state not in ("applied", "undone"):
                        message = str((record or {}).get("error") or STATES.get(state, "操作未完成。"))
                    else:
                        message = f"已修改 {count} 个文件。" if operation == "apply" else f"已还原 {count} 个文件。"
                self.status.set(message)
                self.refresh_history()
                if self.file_paths:
                    self._scan(list(self.file_paths.items()), final_message=message)
        self._refresh_controls()
        self.poll_timer = self.window.after(80, self.poll)

    def cancel(self):
        if self.busy:
            self.stop.set()
            self.status.set("正在取消并还原，请稍候…")
            self._refresh_controls()

    def close(self, _event=None):
        if self.closed:
            return True
        if self.busy:
            self._close_when_idle = True
            self.cancel()
            return False
        self._destroy()
        return True

    def _destroy(self):
        self.closed = True
        self.serial += 1
        self.scan_stop.set()
        self.history_stop.set()
        if self.poll_timer is not None:
            self.window.after_cancel(self.poll_timer)
            self.poll_timer = None
        self.window.destroy()
        if getattr(self.app, "tag_editor_dialog", None) is self:
            self.app.tag_editor_dialog = None
