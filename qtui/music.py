"""Qt page for QQ Music / NetEase Music: public search, link resolution and queueing."""

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import netease_catalog
import qq_catalog
import shared_music

SOURCES = {"qq": "QQ 音乐", "netease": "网易云音乐"}


class MusicPage(QWidget):
    def __init__(self, core, source):
        super().__init__()
        self.core = core
        self.source = source
        self.rows = {}
        self.busy = False
        self.messages = []
        self.stop = threading.Event()

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(SOURCES[source])
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        self.account_button = QPushButton("账户")
        self.account_button.clicked.connect(self.open_account)
        header.addWidget(self.account_button)
        layout.addLayout(header)

        bar = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("输入关键词，或粘贴歌曲、歌单链接")
        self.entry.returnPressed.connect(self.resolve)
        bar.addWidget(self.entry, 1)
        self.resolve_button = QPushButton("查找")
        self.resolve_button.clicked.connect(self.resolve)
        bar.addWidget(self.resolve_button)
        layout.addLayout(bar)

        self.caption = QLabel("歌曲")
        layout.addWidget(self.caption)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("歌曲", "艺人", "时长"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.queue_button = QPushButton("加入队列")
        self.queue_button.clicked.connect(lambda: self.enqueue(False))
        actions.addWidget(self.queue_button)
        self.download_button = QPushButton("下载所选")
        self.download_button.clicked.connect(lambda: self.enqueue(True))
        actions.addWidget(self.download_button)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._refresh()

    def open_account(self):
        from qtui.account import MusicAccountDialog
        dialog = MusicAccountDialog(self, self.source)
        dialog.exec()

    def resolve(self):
        value = self.entry.text().strip()
        if not value or self.busy:
            return
        self.busy = True
        self.stop = threading.Event()
        self.status.setText("正在查找…")
        self._refresh()
        source = shared_music.shared_music_source(value)
        catalog = qq_catalog if self.source == "qq" else netease_catalog

        def run():
            try:
                if source == self.source:
                    resolver = (catalog.resolve_public_qq if self.source == "qq"
                                else catalog.resolve_public_netease)
                    result = resolver(value, stop=self.stop)
                elif source:
                    self.messages.append(("error", f"请在 {SOURCES[source]} 页面解析此链接。"))
                    return
                else:
                    result = catalog.search_songs(value, stop=self.stop)
                self.messages.append(("result", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def enqueue(self, begin):
        rows = [self.rows[index.row()] for index in self.table.selectionModel().selectedRows()
                if index.row() in self.rows]
        if not rows:
            self.status.setText("请先选择要加入队列的歌曲。")
            return
        try:
            ids = self.core.enqueue(rows)
            if begin:
                self.core.start(ids)
            self.status.setText(f"已加入队列 {len(ids)} 项" + ("并开始下载" if begin else ""))
        except (OSError, RuntimeError, ValueError) as error:
            self.status.setText(str(error))

    def _refresh(self):
        idle = not self.busy
        self.resolve_button.setEnabled(idle)
        self.queue_button.setEnabled(bool(self.rows) and idle)
        self.download_button.setEnabled(bool(self.rows) and idle)

    def poll(self):
        while self.messages:
            kind, value = self.messages.pop(0)
            if kind == "result":
                self.busy = False
                self.rows.clear()
                self.table.setRowCount(0)
                self.caption.setText(value.get("title", "歌曲"))
                queue_rows = list(shared_music.queue_rows(
                    self.source, value.get("rows", []), collection_url=value.get("url", ""),
                    batch_title=value.get("title", ""), total=value.get("total")))
                for item in queue_rows:
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self.table.setItem(row, 0, QTableWidgetItem(item.get("title", "")))
                    self.table.setItem(row, 1, QTableWidgetItem(item.get("artist", "")))
                    duration = item.get("duration")
                    self.table.setItem(row, 2, QTableWidgetItem(self._duration(duration)))
                    self.rows[row] = item
                total = value.get("total")
                suffix = f" / {total}" if isinstance(total, int) else ""
                self.status.setText(f"已显示 {len(self.rows)}{suffix} 项")
            elif kind == "error":
                self.busy = False
                self.status.setText(value)
        self._refresh()

    @staticmethod
    def _duration(value):
        if isinstance(value, (int, float)) and value > 0:
            minutes, seconds = divmod(int(round(value)), 60)
            return f"{minutes}:{seconds:02d}"
        return ""
