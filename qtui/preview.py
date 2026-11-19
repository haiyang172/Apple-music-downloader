"""Qt music preview dialog: show a track's source declaration before queueing."""

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


def _size(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return "未知"
    value = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "未知"


class MusicPreviewDialog(QDialog):
    def __init__(self, parent, core, source, row, session=None):
        super().__init__(parent)
        self.core = core
        self.source = source
        self.row = row
        self.session = session
        self.info = None
        self.messages = []
        self.stop = threading.Event()
        self.setWindowTitle(f"{row.get('title', '歌曲')} · 下载预览")
        self.resize(420, 260)

        layout = QVBoxLayout(self)
        self.title = QLabel(row.get("title", "歌曲"))
        self.title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self.title)
        self.source_info = QLabel("—")
        layout.addWidget(self.source_info)
        self.size_info = QLabel("源文件大小 · —")
        layout.addWidget(self.size_info)
        self.duration_info = QLabel("")
        layout.addWidget(self.duration_info)
        self.status = QLabel("正在读取平台音轨信息…")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row_layout = QHBoxLayout()
        row_layout.addStretch(1)
        self.queue_button = QPushButton("加入队列")
        self.queue_button.clicked.connect(self.enqueue)
        self.queue_button.setEnabled(False)
        row_layout.addWidget(self.queue_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        row_layout.addWidget(self.close_button)
        layout.addLayout(row_layout)

        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.inspect()

    def inspect(self):
        from netease_download import probe_netease
        from qq_download import probe_qq
        probe = probe_qq if self.source == "qq" else probe_netease
        url, duration = self.row["url"], self.row.get("duration")
        session, messages, stop = self.session, self.messages, self.stop

        def run():
            try:
                result = probe(url, stop, lambda text: messages.append(("status", text)),
                               session=session, expected_duration=duration, source_quality="auto")
                messages.append(("result", result))
            except Exception as error:
                messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def enqueue(self):
        if self.info is None:
            return
        source = "qq" if self.source == "qq" else "netease"
        rows = [{"source": source, "url": self.row["url"], "title": self.row.get("title", ""),
                 "artist": self.row.get("artist", ""), "duration": self.row.get("duration", 0)}]
        try:
            ids = self.core.enqueue(rows)
            self.status.setText(f"已加入队列 {len(ids)} 项")
        except (OSError, RuntimeError, ValueError) as error:
            self.status.setText(str(error))

    def poll(self):
        while self.messages:
            kind, value = self.messages.pop(0)
            if kind == "status":
                self.status.setText(value)
            elif kind == "result":
                self.info = value
                label = value.get("label") or value.get("codec") or "音轨"
                self.source_info.setText("平台提供 · " + label)
                self.size_info.setText("源文件大小 · " + _size(value.get("size")))
                self.duration_info.setText(f"音轨时长 · {value.get('duration', '—')} 秒" if value.get("duration") else "")
                self.status.setText("音轨规格将在下载后校验。")
                self.queue_button.setEnabled(True)
            elif kind == "error":
                self.status.setText(value)
                self.info = None
