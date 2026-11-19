"""Qt Apple Music library: connect a Music User Token and browse/queue songs."""

import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

import settings
from apple_download import SOURCE, read_links
from music_catalog import CatalogClient
from music_library import LibraryClient, LibrarySessionRequired

ROOT = Path(__file__).resolve().parents[1]


class AppleLibraryDialog(QDialog):
    def __init__(self, parent, core):
        super().__init__(parent)
        self.core = core
        self.region = core.preferences.get("region", "au")
        self.catalog = CatalogClient()
        self.rows = {}
        self.busy = False
        self.messages = []
        self.stop = threading.Event()
        self.setWindowTitle("Apple Music 资料库")
        self.resize(640, 520)

        layout = QVBoxLayout(self)
        title = QLabel("Apple Music 资料库")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setPlaceholderText("Music User Token（或使用网页登录）")
        self.token.setEchoMode(QLineEdit.Password)
        row.addWidget(self.token, 1)
        self.web_button = QPushButton("网页登录并连接")
        self.web_button.clicked.connect(self.connect_web)
        row.addWidget(self.web_button)
        self.token_button = QPushButton("连接令牌")
        self.token_button.clicked.connect(self.connect_token)
        row.addWidget(self.token_button)
        layout.addLayout(row)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("歌曲", "艺人", "专辑"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.load_button = QPushButton("读取歌曲")
        self.load_button.clicked.connect(self.load_songs)
        actions.addWidget(self.load_button)
        actions.addStretch(1)
        self.queue_button = QPushButton("加入队列")
        self.queue_button.clicked.connect(self.enqueue)
        actions.addWidget(self.queue_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        try:
            token = settings.session(core.preferences)
        except ValueError:
            token = ""
        if token:
            self.status.setText("已连接资料库，可点「读取歌曲」。")
        else:
            self.status.setText("请先网页登录或粘贴 Music User Token。")
        self._refresh()

    def _token(self):
        try:
            return settings.session(self.core.preferences)
        except ValueError:
            return ""

    def connect_web(self):
        if self.busy:
            return
        self.busy = True
        self.stop = threading.Event()
        self.status.setText("正在打开 Apple Music 官方登录窗口…")
        self._refresh()
        core, stop, messages = self.core, self.stop, self.messages

        def run():
            try:
                import apple_web_login
                token = apple_web_login.connect_library(
                    ROOT, settings.DATA / "apple-web-profile", self.region, self.catalog,
                    stop, lambda _text: None)
                messages.append(("token", token))
            except Exception as error:
                messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def connect_token(self):
        if self.busy:
            return
        token = self.token.text().strip()
        self.busy = True
        self.messages.append(("token", token))
        self._refresh()

    def _store(self, token):
        try:
            protected = settings.protect_session(token)
            values = settings.load()
            values["session"] = protected
            settings.save(values)
            self.core.preferences = values
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return False
        return True

    def load_songs(self):
        if self.busy:
            return
        token = self._token()
        if not token:
            self.status.setText("请先连接资料库。")
            return
        self.busy = True
        self.stop = threading.Event()
        self.status.setText("正在读取资料库歌曲…")
        self._refresh()
        client = LibraryClient(token, self.region, catalog=self.catalog)
        messages = self.messages

        def run():
            try:
                rows, _next = client.items("song")
                messages.append(("songs", rows))
            except LibrarySessionRequired:
                messages.append(("error", "Apple Music 资料库会话已失效，请重新连接。"))
            except Exception as error:
                messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def enqueue(self):
        items = [self.rows[index.row()] for index in self.table.selectionModel().selectedRows()
                 if index.row() in self.rows]
        rows = []
        for item in items:
            try:
                url = read_links(item["url"])[0]
            except (KeyError, ValueError):
                continue
            rows.append({"source": SOURCE, "url": url, "title": item.get("title", ""),
                         "artist": item.get("artist", ""), "duration": item.get("duration", 0)})
        if not rows:
            self.status.setText("所选歌曲没有可入队的目录链接。")
            return
        try:
            ids = self.core.enqueue(rows)
            self.status.setText(f"已加入队列 {len(ids)} 项")
        except (OSError, RuntimeError, ValueError) as error:
            self.status.setText(str(error))

    def _refresh(self):
        idle = not self.busy
        self.web_button.setEnabled(idle)
        self.token_button.setEnabled(idle)
        self.load_button.setEnabled(idle)
        self.queue_button.setEnabled(bool(self.rows) and idle)
        self.token.setEnabled(idle)

    def poll(self):
        while self.messages:
            kind, value = self.messages.pop(0)
            if kind == "token":
                if value and self._store(value):
                    self.status.setText("资料库已连接。可点「读取歌曲」。")
                self.busy = False
            elif kind == "songs":
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
                self.status.setText(f"已读取 {len(self.rows)} 首歌曲")
            elif kind == "error":
                self.busy = False
                self.status.setText(value)
        self._refresh()
