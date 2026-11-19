"""Qt account dialog for QQ Music / NetEase Music QR login.

Reuses the Tk-independent auth backends (QqAuth / NeteaseAuth) and only
reimplements the QR display and polling loop in Qt.
"""

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from qrcodegen import QrCode

import settings
from bili_auth import BiliAuth
from netease_auth import NeteaseAuth
from qq_auth import QqAuth

AUTH = {
    "qq": lambda: QqAuth(settings.DATA / "qq-music-session.dat"),
    "netease": lambda: NeteaseAuth(settings.DATA / "netease-music-session.dat"),
    "bilibili": lambda: BiliAuth(settings.DATA / "bilibili-session.dat"),
}
NAMES = {"qq": "QQ 音乐", "netease": "网易云音乐", "bilibili": "B 站"}


def qr_pixmap(url, scale=3):
    code = QrCode.encode_text(url, QrCode.Ecc.MEDIUM)
    modules = code.get_size()
    quiet = 4
    pixel = max(2, round(3 * scale))
    size = (modules + 2 * quiet) * pixel
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(Qt.white)
    for y in range(modules):
        for x in range(modules):
            if code.get_module(x, y):
                for dy in range(pixel):
                    for dx in range(pixel):
                        image.setPixel((x + quiet) * pixel + dx, (y + quiet) * pixel + dy, 0xFF10141C)
    return QPixmap.fromImage(image)


class MusicAccountDialog(QDialog):
    def __init__(self, parent, source):
        super().__init__(parent)
        self.source = source
        self.auth = AUTH[source]()
        self.qr = None
        self.session = None
        self.polling = False
        self.setWindowTitle(f"{NAMES[source]} 账户")
        self.resize(360, 420)

        layout = QVBoxLayout(self)
        self.title = QLabel(NAMES[source])
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self.title)

        self.qr_label = QLabel("正在获取二维码…")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(240, 240)
        layout.addWidget(self.qr_label)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.refresh_button = QPushButton("重新获取二维码")
        self.refresh_button.clicked.connect(self.begin)
        row.addWidget(self.refresh_button)
        self.logout_button = QPushButton("退出登录")
        self.logout_button.clicked.connect(self.logout)
        row.addWidget(self.logout_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        layout.addLayout(row)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

        try:
            self.session = self.auth.load()
        except Exception:
            self.session = None
        if self.session is not None:
            self._signed_in(getattr(self.session, "_name", None) or "已登录")
        else:
            self.begin()

    def begin(self):
        self.polling = False
        self.qr = None
        try:
            self.qr = self.auth.begin()
        except Exception as error:
            self.qr_label.setText("")
            self.status.setText(str(error))
            self.refresh_button.setEnabled(True)
            return
        if hasattr(self.qr, "image"):
            pixmap = QPixmap()
            pixmap.loadFromData(self.qr.image)
            self.qr_label.setPixmap(pixmap)
        else:
            self.qr_label.setPixmap(qr_pixmap(self.qr.url))
        self.status.setText("请用手机应用扫码登录")
        self.polling = True
        self.refresh_button.setEnabled(False)

    def poll(self):
        if not self.polling or self.qr is None:
            return
        try:
            result = self.auth.poll(self.qr)
        except Exception as error:
            self.polling = False
            self.status.setText(str(error))
            self.refresh_button.setEnabled(True)
            return
        if result.state == "success":
            self.polling = False
            try:
                self.auth.save(result.session)
                account = self.auth.check(result.session)
                self.session = result.session
                self._signed_in(getattr(account, "name", "") or "已登录")
            except Exception as error:
                self.status.setText(str(error))
                self.refresh_button.setEnabled(True)
        elif result.state == "scanned":
            self.status.setText("已扫码，请在手机上确认")
        elif result.state == "expired":
            self.polling = False
            self.status.setText("二维码已过期，请重新获取")
            self.refresh_button.setEnabled(True)

    def _signed_in(self, name):
        self.qr_label.setPixmap(QPixmap())
        self.qr_label.setText("✓ 已登录")
        self.status.setText(f"{NAMES[self.source]} · {name}")
        self.refresh_button.setEnabled(False)
        self.logout_button.setEnabled(True)

    def logout(self):
        try:
            self.auth.clear()
        except Exception as error:
            self.status.setText(str(error))
            return
        self.session = None
        self.logout_button.setEnabled(False)
        self.begin()
