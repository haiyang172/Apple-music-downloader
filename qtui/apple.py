"""Qt Apple Music page: public catalogue search and song queueing."""

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from apple_download import SOURCE, read_links
from music_catalog import CatalogClient


class ApplePage(QWidget):
    def __init__(self, core):
        super().__init__()
        self.core = core
        self.catalog = CatalogClient()
        self.region = core.preferences.get("region", "au")
        self.rows = {}
        self.busy = False
        self.messages = []
        self.stop = threading.Event()

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("Apple Music")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        bar = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("输入歌曲、歌手或专辑名称")
        self.entry.returnPressed.connect(self.resolve)
        bar.addWidget(self.entry, 1)
        self.resolve_button = QPushButton("搜索")
        self.resolve_button.clicked.connect(self.resolve)
        bar.addWidget(self.resolve_button)
        layout.addLayout(bar)

        self.caption = QLabel("歌曲")
        layout.addWidget(self.caption)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("歌曲", "艺人", "专辑"))
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

    def resolve(self):
        term = self.entry.text().strip()
        if not term or self.busy:
            return
        self.busy = True
        self.stop = threading.Event()
        self.status.setText("正在搜索…")
        self._refresh()

        def run():
            try:
                rows = self.catalog.search(term, self.region, "song")
                self.messages.append(("result", rows))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def enqueue(self, begin):
        items = [self.rows[index.row()] for index in self.table.selectionModel().selectedRows()
                 if index.row() in self.rows]
        if not items:
            self.status.setText("请先选择要加入队列的歌曲。")
            return
        rows = []
        for item in items:
            try:
                url = read_links(item["url"])[0]
            except ValueError:
                continue
            rows.append({"source": SOURCE, "url": url, "title": item.get("title", ""),
                         "artist": item.get("artist", ""), "duration": item.get("duration", 0)})
        if not rows:
            self.status.setText("所选项目无法加入队列。")
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
                for item in value:
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self.table.setItem(row, 0, QTableWidgetItem(item.get("title", "")))
                    self.table.setItem(row, 1, QTableWidgetItem(item.get("artist", "")))
                    self.table.setItem(row, 2, QTableWidgetItem(item.get("album", "")))
                    self.rows[row] = item
                self.status.setText(f"已显示 {len(self.rows)} 首歌曲")
            elif kind == "error":
                self.busy = False
                self.status.setText(value)
        self._refresh()
