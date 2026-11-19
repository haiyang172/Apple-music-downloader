"""Qt Apple Music account dialog: username/password + optional 2FA.

Reuses the shared LoginService from the queue controller.
"""

import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout)

import settings


class AppleAccountDialog(QDialog):
    def __init__(self, parent, core):
        super().__init__(parent)
        self.core = core
        self.login_service = core.login_service
        self.messages = []
        self.busy = False
        self.stop = threading.Event()
        self.setWindowTitle("Apple Music 账户")
        self.resize(380, 300)

        layout = QVBoxLayout(self)
        title = QLabel("Apple Music")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        self.username = QLineEdit()
        self.username.setPlaceholderText("Apple 账户")
        layout.addWidget(self.username)
        self.password = QLineEdit()
        self.password.setPlaceholderText("密码")
        self.password.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.password)
        self.code = QLineEdit()
        self.code.setPlaceholderText("6 位验证码（需要时填写）")
        self.code.setVisible(False)
        layout.addWidget(self.code)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.login_button = QPushButton("登录")
        self.login_button.clicked.connect(self.login)
        row.addWidget(self.login_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        row.addWidget(self.close_button)
        layout.addLayout(row)

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._check_status()

    def _check_status(self):
        try:
            status = self.login_service.status()
        except Exception:
            status = {"running": False, "logged_in": False}
        if status.get("logged_in"):
            self.status.setText("已登录 Apple Music。")
        elif not status.get("running"):
            self.status.setText("登录服务未启动，点「登录」会先启动本地服务。")

    def login(self):
        if self.busy:
            return
        username = self.username.text().strip()
        password = self.password.text()
        code = self.code.text().strip() or None
        self.busy = True
        self.stop = threading.Event()
        self.status.setText("正在登录…")
        self._refresh()
        service, stop, messages = self.login_service, self.stop, self.messages

        def run():
            try:
                if not service.status().get("running"):
                    service.start(stop, lambda _text: None)
                result = service.login(username, password, code)
                messages.append(("result", result))
            except Exception as error:
                messages.append(("error", str(error)))

        threading.Thread(target=run, daemon=True).start()

    def _refresh(self):
        self.login_button.setEnabled(not self.busy)
        self.username.setEnabled(not self.busy)
        self.password.setEnabled(not self.busy)
        self.code.setEnabled(not self.busy)

    def poll(self):
        while self.messages:
            kind, value = self.messages.pop(0)
            if kind == "result":
                self.busy = False
                if value.get("needs_code"):
                    self.code.setVisible(True)
                    self.status.setText("需要双重验证码，请输入后再次点「登录」。")
                elif value.get("logged_in"):
                    self.status.setText("已登录 Apple Music。")
                    self.code.setVisible(False)
                else:
                    self.status.setText("登录未完成，请检查账户信息。")
            elif kind == "error":
                self.busy = False
                self.status.setText(value)
        self._refresh()
