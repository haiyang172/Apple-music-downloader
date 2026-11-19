"""Qt page for QQ Music / NetEase Music: public search, link resolution and queueing."""

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import netease_catalog
import qq_catalog
import settings
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
        self.playlist_view = False
        self.current_playlist = None

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(SOURCES[source])
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        self.account_button = QPushButton("账户")
        self.account_button.clicked.connect(self.open_account)
        header.addWidget(self.account_button)
        self.playlists_button = QPushButton("我的歌单")
        self.playlists_button.clicked.connect(self.show_playlists)
        header.addWidget(self.playlists_button)
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
        self.table.doubleClicked.connect(lambda _index: self.open_playlist())
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.preview_button = QPushButton("下载预览")
        self.preview_button.clicked.connect(self.preview)
        actions.addWidget(self.preview_button)
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

    def _load_session(self):
        from netease_auth import NeteaseAuth
        from qq_auth import QqAuth
        auth = (QqAuth(settings.DATA / "qq-music-session.dat") if self.source == "qq"
                else NeteaseAuth(settings.DATA / "netease-music-session.dat"))
        try:
            return auth.load()
        except Exception:
            return None

    def show_playlists(self):
        if self.busy:
            return
        session = self._load_session()
        if session is None:
            self.status.setText("请先登录后再查看歌单。")
            self.open_account()
            return
        self.busy = True
        self.playlist_view = True
        self.current_playlist = None
        self.stop = threading.Event()
        self.status.setText("正在读取歌单…")
        self._refresh()
        catalog = qq_catalog if self.source == "qq" else netease_catalog

        def run():
            try:
                result = catalog.list_playlists(session, stop=self.stop)
                self.messages.append(("playlists", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def open_playlist(self):
        if self.busy or not self.playlist_view:
            return
        rows = [index.row() for index in self.table.selectionModel().selectedRows()
                if index.row() in self.rows]
        if len(rows) != 1:
            self.status.setText("请先选择一个歌单。")
            return
        playlist = self.rows[rows[0]]
        session = self._load_session()
        if session is None:
            self.open_account()
            return
        self.busy = True
        self.playlist_view = False
        self.current_playlist = playlist
        self.stop = threading.Event()
        self.status.setText("正在读取歌单曲目…")
        self._refresh()
        catalog = qq_catalog if self.source == "qq" else netease_catalog

        def run():
            try:
                result = catalog.resolve_playlist(session, playlist, stop=self.stop)
                self.messages.append(("tracks", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def preview(self):
        rows = [index.row() for index in self.table.selectionModel().selectedRows() if index.row() in self.rows]
        if len(rows) != 1:
            self.status.setText("请先选择一首歌曲。")
            return
        from qtui.preview import MusicPreviewDialog
        dialog = MusicPreviewDialog(self, self.core, self.source, self.rows[rows[0]])
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
        if self.playlist_view:
            self.status.setText("请先打开歌单，再选择曲目入队。")
            return
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
        tracks = bool(self.rows) and not self.playlist_view
        self.resolve_button.setEnabled(idle)
        self.playlists_button.setEnabled(idle)
        self.preview_button.setEnabled(tracks and idle)
        self.queue_button.setEnabled(tracks and idle)
        self.download_button.setEnabled(tracks and idle)

    def _fill_tracks(self, rows):
        self.rows.clear()
        self.table.setRowCount(0)
        for item in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(item.get("title", "")))
            self.table.setItem(row, 1, QTableWidgetItem(item.get("artist", "")))
            self.table.setItem(row, 2, QTableWidgetItem(self._duration(item.get("duration"))))
            self.rows[row] = item

    def poll(self):
        while self.messages:
            kind, value = self.messages.pop(0)
            if kind == "result":
                self.busy = False
                self.playlist_view = False
                self.caption.setText(value.get("title", "歌曲"))
                queue_rows = list(shared_music.queue_rows(
                    self.source, value.get("rows", []), collection_url=value.get("url", ""),
                    batch_title=value.get("title", ""), total=value.get("total")))
                self._fill_tracks(queue_rows)
                total = value.get("total")
                suffix = f" / {total}" if isinstance(total, int) else ""
                self.status.setText(f"已显示 {len(self.rows)}{suffix} 项")
            elif kind == "playlists":
                self.busy = False
                self.playlist_view = True
                self.caption.setText("我的歌单")
                self.rows.clear()
                self.table.setRowCount(0)
                for playlist in value.get("rows", []):
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self.table.setItem(row, 0, QTableWidgetItem(playlist.get("title", "")))
                    self.table.setItem(row, 1, QTableWidgetItem(""))
                    self.table.setItem(row, 2, QTableWidgetItem(""))
                    self.rows[row] = playlist
                self.status.setText(f"共 {len(self.rows)} 个歌单，双击打开。")
            elif kind == "tracks":
                self.busy = False
                self.playlist_view = False
                session = self._load_session()
                owner = getattr(session, "user_id", None) or getattr(session, "musicid", None)
                collection_key = shared_music.private_collection_key(
                    self.source, owner, self.current_playlist)
                self.caption.setText(self.current_playlist.get("title", "歌单"))
                queue_rows = list(shared_music.queue_rows(
                    self.source, value.get("rows", []), private=True, collection_key=collection_key,
                    batch_title=self.current_playlist.get("title", ""), total=value.get("total")))
                self._fill_tracks(queue_rows)
                self.status.setText(f"已显示 {len(self.rows)} 首")
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
