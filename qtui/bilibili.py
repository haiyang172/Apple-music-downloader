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
        self.favorites_view = False
        self.current_folder = None

        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel("B 站")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch(1)
        self.account_button = QPushButton("账户")
        self.account_button.clicked.connect(self.open_account)
        header.addWidget(self.account_button)
        self.favorites_button = QPushButton("我的收藏夹")
        self.favorites_button.clicked.connect(self.show_favorites)
        header.addWidget(self.favorites_button)
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
        self.table.doubleClicked.connect(lambda _index: self.open_folder())
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

    def open_account(self):
        from qtui.account import MusicAccountDialog
        dialog = MusicAccountDialog(self, "bilibili")
        dialog.exec()

    def _load_session(self):
        import settings
        from bili_auth import BiliAuth
        try:
            return BiliAuth(settings.DATA / "bilibili-session.dat").load()
        except Exception:
            return None

    def _load_account(self):
        import settings
        from bili_auth import BiliAuth
        session = self._load_session()
        if session is None:
            return None
        try:
            return BiliAuth(settings.DATA / "bilibili-session.dat").check(session)
        except Exception:
            return None

    def show_favorites(self):
        if self.busy:
            return
        account = self._load_account()
        session = self._load_session()
        if account is None or session is None:
            self.status.setText("请先扫码登录 B 站。")
            self.open_account()
            return
        self.busy = True
        self.favorites_view = True
        self.current_folder = None
        self.stop = threading.Event()
        self.status.setText("正在读取收藏夹…")
        self._refresh()

        def run():
            try:
                result = bili_collections.list_my_favorites(session, account, stop=self.stop)
                self.messages.append(("favorites", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def open_folder(self):
        if self.busy or not self.favorites_view:
            return
        rows = [index.row() for index in self.table.selectionModel().selectedRows()
                if index.row() in self.rows]
        if len(rows) != 1:
            self.status.setText("请先选择一个收藏夹。")
            return
        folder = self.rows[rows[0]]
        account = self._load_account()
        session = self._load_session()
        if account is None or session is None:
            self.open_account()
            return
        self.busy = True
        self.favorites_view = False
        self.current_folder = folder
        self.stop = threading.Event()
        self.status.setText("正在读取收藏夹内容…")
        self._refresh()

        def run():
            try:
                result = bili_collections.resolve_my_favorite(session, account, folder, stop=self.stop)
                self.messages.append(("folder", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

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
                session = self._load_session()
                if bili_collections.is_collection_link(value):
                    result = bili_collections.resolve_collection(value, stop=self.stop, session=session)
                elif bili_catalog.is_video_link(value):
                    result = bili_catalog.resolve_video(value, session=session)
                else:
                    self.messages.append(("error", "请输入 B 站视频链接、短链、BV 或 av 号。"))
                    return
                self.messages.append(("result", result))
            except Exception as error:
                self.messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def enqueue(self, begin):
        if self.favorites_view:
            self.status.setText("请先打开收藏夹，再选择视频入队。")
            return
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
        tracks = bool(self.rows) and not self.favorites_view
        self.resolve_button.setEnabled(idle)
        self.favorites_button.setEnabled(idle)
        self.queue_button.setEnabled(tracks and idle)
        self.download_button.setEnabled(tracks and idle)

    def poll(self):
        while self.messages:
            kind, value = self.messages.pop(0)
            if kind == "result":
                self.busy = False
                self.favorites_view = False
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
            elif kind == "favorites":
                self.busy = False
                self.favorites_view = True
                self.caption.setText(value.get("title", "我的收藏夹"))
                self.rows.clear()
                self.table.setRowCount(0)
                for folder in value.get("rows", []):
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self.table.setItem(row, 0, QTableWidgetItem(""))
                    self.table.setItem(row, 1, QTableWidgetItem(folder.get("title", "")))
                    self.table.setItem(row, 2, QTableWidgetItem(str(folder.get("total", ""))))
                    self.rows[row] = folder
                self.status.setText(f"共 {len(self.rows)} 个收藏夹，双击打开。")
            elif kind == "folder":
                self.busy = False
                self.favorites_view = False
                self.rows.clear()
                self.table.setRowCount(0)
                self.caption.setText(value.get("title", "收藏夹"))
                for item in value.get("rows", []):
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    position = f"{item.get('position')}/{item.get('total')}" if item.get("position") else ""
                    self.table.setItem(row, 0, QTableWidgetItem(str(position)))
                    self.table.setItem(row, 1, QTableWidgetItem(item.get("audio_title") or item.get("title", "")))
                    self.table.setItem(row, 2, QTableWidgetItem(self._duration(item.get("duration"))))
                    self.rows[row] = item
                self.status.setText(f"已显示 {len(self.rows)} 个视频")
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
