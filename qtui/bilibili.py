"""Qt Bilibili page: resolve a video link into parts and queue them."""

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import bili_catalog
import bili_collections


class BilibiliPage(QWidget):
    def __init__(self, core):
        super().__init__()
        self.core = core
        self.rows = {}
        self.busy = False
        self.messages = []
        self.stop = threading.Event()

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("B 站")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        bar = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("粘贴视频链接、短链、BV 或 av 号")
        self.entry.returnPressed.connect(self.resolve)
        bar.addWidget(self.entry, 1)
        self.resolve_button = QPushButton("查看分 P")
        self.resolve_button.clicked.connect(self.resolve)
        bar.addWidget(self.resolve_button)
        layout.addLayout(bar)

        self.caption = QLabel("视频")
        layout.addWidget(self.caption)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("分 P", "歌名 / 标题", "时长"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
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
        value = self.entry.text().strip()
        if not value or self.busy:
            return
        self.busy = True
        self.stop = threading.Event()
        self.status.setText("正在读取…")
        self._refresh()

        def run():
            try:
                if bili_collections.is_collection_link(value):
                    result = bili_collections.resolve_collection(value, stop=self.stop)
                elif bili_catalog.is_video_link(value):
                    result = bili_catalog.resolve_video(value, session=None)
                else:
                    self.messages.append(("error", "请输入 B 站视频链接、短链、BV 或 av 号。"))
                    return
                self.messages.append(("result", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def enqueue(self, begin):
        rows = [self.rows[index.row()] for index in self.table.selectionModel().selectedRows()
                if index.row() in self.rows]
        if not rows:
            self.status.setText("请先选择要加入队列的分 P。")
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
                self.caption.setText(value.get("title", "视频"))
                for item in value.get("rows", []):
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    position = f"{item.get('position')}/{item.get('total')}" if item.get("position") else ""
                    self.table.setItem(row, 0, QTableWidgetItem(str(position)))
                    self.table.setItem(row, 1, QTableWidgetItem(item.get("audio_title") or item.get("title", "")))
                    self.table.setItem(row, 2, QTableWidgetItem(self._duration(item.get("duration"))))
                    self.rows[row] = item
                self.status.setText(f"已显示 {len(self.rows)} 个分 P")
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
