"""Qt tag editor dialog backed by the shared TagEditStore.

Mirrors the Tkinter TagEditor's two pages (edit fields, history/undo/recover)
without touching the reversible tag-edit engine.
"""

import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import settings
from tag_edit import TagEditStore

FIELDS = (("title", "歌名"), ("artist", "歌手"), ("album", "专辑"),
          ("genre", "流派"), ("date", "年份"), ("tracknumber", "音轨"))
STATES = {
    "applying": "写入中断", "undoing": "撤销中断", "preparing": "准备中断",
    "applied": "已修改", "undone": "已撤销", "cancelled": "已取消",
    "rolled_back": "已还原", "failed": "未完成", "conflict": "文件已变化",
}
RECOVERABLE = ("applying", "undoing", "preparing")
SUPPORTED = frozenset({".mp3", ".m4a", ".flac", ".wav", ".opus"})


class TagEditorDialog(QDialog):
    def __init__(self, parent, paths=()):
        super().__init__(parent)
        self.setWindowTitle("本地音频标签")
        self.resize(820, 560)
        self.store = TagEditStore(settings.DATA / "tag-edits")
        self.files = {}  # row -> info dict
        self.file_paths = {}  # row -> path str
        self.history = {}  # batch id -> record
        self.busy = self.scanning = False
        self.messages = []
        self.stop = None

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_edit(), "修改标签")
        self.tabs.addTab(self._build_history(), "修改历史")

        footer = QHBoxLayout()
        self.status = QLabel("添加音频文件，勾选需要修改的字段。")
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        self.apply_button = QPushButton("应用到 0 个文件")
        self.apply_button.clicked.connect(self.apply)
        footer.addWidget(self.apply_button)
        self.undo_button = QPushButton("撤销所选批次")
        self.undo_button.clicked.connect(self.undo)
        self.recover_button = QPushButton("恢复中断批次")
        self.recover_button.clicked.connect(self.recover)
        footer.addWidget(self.undo_button)
        footer.addWidget(self.recover_button)
        root.addLayout(footer)

        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

        self.refresh_history()
        if paths:
            self.add_files(paths)
        self._refresh_controls()

    def _build_edit(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self.selection_text = QLabel("尚未选择文件")
        toolbar.addWidget(self.selection_text, 1)
        self.add_button = QPushButton("添加文件")
        self.add_button.clicked.connect(self.choose_files)
        toolbar.addWidget(self.add_button)
        self.remove_button = QPushButton("移除所选")
        self.remove_button.clicked.connect(self.remove_selected)
        toolbar.addWidget(self.remove_button)
        layout.addLayout(toolbar)

        self.tree = QTableWidget(0, 4)
        self.tree.setHorizontalHeaderLabels(("文件名", "歌名", "歌手", "专辑"))
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.verticalHeader().setVisible(False)
        header = self.tree.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.tree, 1)

        form = QVBoxLayout()
        self.checks, self.entries = {}, {}
        for key, title in FIELDS:
            row = QHBoxLayout()
            check = QCheckBox(title)
            check.setFixedWidth(110)
            check.toggled.connect(self._refresh_controls)
            entry = QLineEdit()
            row.addWidget(check)
            row.addWidget(entry, 1)
            form.addLayout(row)
            self.checks[key] = check
            self.entries[key] = entry
        layout.addLayout(form)
        hint = QLabel("只修改勾选字段；留空清除。文件名保持不变，可在历史中撤销。")
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        return page

    def _build_history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("每次应用为一个批次，可从备份还原原文件。"), 1)
        self.refresh_button = QPushButton("刷新历史")
        self.refresh_button.clicked.connect(self.refresh_history)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.history_tree = QTableWidget(0, 4)
        self.history_tree.setHorizontalHeaderLabels(("时间", "文件数", "状态", "批次"))
        self.history_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_tree.verticalHeader().setVisible(False)
        header = self.history_tree.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.history_tree.itemSelectionChanged.connect(self._refresh_controls)
        layout.addWidget(self.history_tree, 1)
        self.history_detail = QLabel("历史修改会保留备份，关闭后仍可撤销。")
        self.history_detail.setWordWrap(True)
        layout.addWidget(self.history_detail)
        self.history_rows = {}
        return page

    # ---- file list ----

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加音频文件", str(Path.home()),
            "音频文件 (*.mp3 *.m4a *.flac *.wav *.opus);;所有文件 (*.*)")
        if paths:
            self.add_files(paths)

    def add_files(self, paths):
        known = {os.path.normcase(value) for value in self.file_paths.values()}
        added = []
        for value in paths:
            path = Path(os.path.abspath(value))
            key = os.path.normcase(str(path))
            if path.suffix.lower() not in SUPPORTED or key in known:
                continue
            known.add(key)
            row = self.tree.rowCount()
            self.tree.insertRow(row)
            self.tree.setItem(row, 0, QTableWidgetItem(path.name))
            for column in (1, 2, 3):
                self.tree.setItem(row, column, QTableWidgetItem("正在读取…"))
            self.files[row] = None
            self.file_paths[row] = str(path)
            added.append((row, str(path)))
        if added:
            self.tree.selectAll()
            self._scan(added)
        self._selection_changed()

    def remove_selected(self):
        for row in sorted({index.row() for index in self.tree.selectedIndexes()}, reverse=True):
            self.tree.removeRow(row)
            self.files.pop(row, None)
            self.file_paths.pop(row, None)
        self._selection_changed()

    def _scan(self, entries):
        self.scanning = True
        self._refresh_controls()

        def run():
            for row, path in entries:
                try:
                    info = self.store.read_audio(path)
                except Exception as error:
                    self.messages.append(("file", (row, None, str(error))))
                else:
                    self.messages.append(("file", (row, info, "")))
            self.messages.append(("scan_done", ()))

        threading.Thread(target=run, daemon=True).start()

    def _selected_rows(self):
        rows = {index.row() for index in self.tree.selectedIndexes()}
        return rows or set(self.files)

    def _selection_changed(self):
        rows = self._selected_rows()
        infos = [self.files[row] for row in rows if self.files.get(row)]
        self.selection_text.setText(f"已选 {len(infos)} 个文件 · 共 {len(self.files)} 个")
        for key, _title in FIELDS:
            values = {str(info.get(key) or "") for info in infos}
            if len(values) == 1:
                self.entries[key].setText(next(iter(values)))
            else:
                self.entries[key].setText("")
            self.checks[key].setChecked(False)
        self._refresh_controls()

    def apply(self):
        if self.busy:
            return
        changes = {key: self.entries[key].text() for key, _title in FIELDS if self.checks[key].isChecked()}
        edits = [{"path": info["path"], "changes": dict(changes), "expected": info["fingerprint"]}
                 for info in (self.files[row] for row in self._selected_rows()) if info]
        if changes and edits:
            self._run(lambda stop, status: self.store.apply(edits, stop=stop, on_status=status))

    def undo(self):
        if self.busy:
            return
        record = self._selected_history()
        if record and record.get("state") == "applied":
            self._run(lambda stop, status: self.store.undo(record["id"], stop=stop, on_status=status))

    def recover(self):
        if self.busy:
            return
        record = self._selected_history()
        if record and record.get("state") in RECOVERABLE:
            self._run(lambda stop, status: self.store.recover(stop=stop, on_status=status))

    def _run(self, operation):
        self.busy = True
        self.stop = stop = threading.Event()
        self.status.setText("正在处理标签…")
        self._refresh_controls()

        def run():
            try:
                result = operation(stop, lambda text: self.messages.append(("status", text)))
                self.messages.append(("write_done", result))
            except Exception as error:
                self.messages.append(("write_done", getattr(error, "record", None)))
                self.messages.append(("error", str(error) or "操作已取消。"))

        threading.Thread(target=run, daemon=True).start()

    def _selected_history(self):
        rows = {index.row() for index in self.history_tree.selectedIndexes()}
        if not rows:
            return None
        return self.history.get(self.history_rows.get(next(iter(rows))))

    def refresh_history(self):
        try:
            records = self.store.list_history()
        except Exception:
            records = []
        self.history.clear()
        self.history_rows = {}
        self.history_tree.setRowCount(0)
        for record in sorted(records, key=lambda item: item.get("created_at", ""), reverse=True):
            batch = str(record["id"])
            self.history[batch] = record
            row = self.history_tree.rowCount()
            self.history_tree.insertRow(row)
            self.history_rows[row] = batch
            stamp = str(record.get("created_at", "")).replace("T", " ").replace("+00:00", " UTC")
            self.history_tree.setItem(row, 0, QTableWidgetItem(stamp))
            self.history_tree.setItem(row, 1, QTableWidgetItem(str(len(record.get("items") or []))))
            self.history_tree.setItem(row, 2, QTableWidgetItem(STATES.get(record.get("state"), record.get("state", "—"))))
            self.history_tree.setItem(row, 3, QTableWidgetItem(batch[:10]))
        self._refresh_controls()

    def _refresh_controls(self):
        if not hasattr(self, "apply_button"):
            return
        rows = self._selected_rows()
        selected_infos = [self.files[row] for row in rows if self.files.get(row)]
        count = len(selected_infos)
        any_checked = any(self.checks[key].isChecked() for key, _title in FIELDS)
        idle = not self.busy and not self.scanning
        self.apply_button.setText(f"应用到 {count} 个文件")
        self.apply_button.setEnabled(bool(count and any_checked and idle))
        for key, _title in FIELDS:
            self.entries[key].setEnabled(bool(count and idle and self.checks[key].isChecked()))
        self.add_button.setEnabled(idle)
        self.remove_button.setEnabled(bool(self.tree.selectedIndexes()) and idle)
        self.refresh_button.setEnabled(idle)
        record = self._selected_history()
        self.undo_button.setEnabled(bool(idle and record and record.get("state") == "applied"))
        self.recover_button.setEnabled(bool(idle and record and record.get("state") in RECOVERABLE))

    def poll(self):
        while self.messages:
            message = self.messages.pop(0)
            kind = message[0]
            value = message[1] if len(message) > 1 else None
            if kind == "file":
                row, info, error = value
                if row in self.file_paths:
                    self.files[row] = info
                    if info:
                        self.tree.setItem(row, 0, QTableWidgetItem(Path(info["path"]).name))
                        self.tree.setItem(row, 1, QTableWidgetItem(info.get("title", "")))
                        self.tree.setItem(row, 2, QTableWidgetItem(info.get("artist", "")))
                        self.tree.setItem(row, 3, QTableWidgetItem(info.get("album", "")))
                    else:
                        self.tree.setItem(row, 1, QTableWidgetItem("无法读取"))
                        self.status.setText(error)
            elif kind == "scan_done":
                self.scanning = False
                self._selection_changed()
            elif kind == "status":
                self.status.setText(value)
            elif kind == "write_done":
                self.busy = False
                record = value
                if record is not None and isinstance(record, dict):
                    count = len(record.get("items") or [])
                    state = record.get("state")
                    if state in ("applied", "undone"):
                        self.status.setText(f"已修改 {count} 个文件。" if state == "applied" else f"已还原 {count} 个文件。")
                    else:
                        self.status.setText(str(record.get("error") or STATES.get(state, "操作未完成。")))
                else:
                    self.status.setText("已恢复中断批次。")
                self.refresh_history()
                if self.file_paths:
                    self._scan([(row, path) for row, path in self.file_paths.items()])
            elif kind == "error":
                self.busy = False
                self.status.setText(value)
                self.refresh_history()
        self._refresh_controls()
