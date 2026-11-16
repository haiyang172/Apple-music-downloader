"""Scan an official login code and retain the resulting local session."""

import base64
import math
import threading
import time
import tkinter as tk
from tkinter import ttk

from qrcodegen import QrCode


def qr_image(master, url, scale):
    code = QrCode.encode_text(url, QrCode.Ecc.MEDIUM)
    modules = code.get_size()
    quiet = 4
    pixel = max(2, round(3 * scale))
    width = (modules + 2 * quiet) * pixel
    rows = []
    for y in range(-quiet, modules + quiet):
        row = b"".join((b"\0\0\0" if code.get_module(x, y) else b"\xff\xff\xff") * pixel
                       for x in range(-quiet, modules + quiet))
        rows.append(row * pixel)
    data = f"P6\n{width} {width}\n255\n".encode("ascii") + b"".join(rows)
    return tk.PhotoImage(master=master, data=data, format="PPM")


class QrAccountPanel:
    def __init__(self, app, parent, auth, *, changed=None):
        self.app = app
        self.auth = auth
        self.changed = changed
        self.session = None
        self.account = None
        self.kept = True
        self.serial = 0
        self.stop = threading.Event()
        self.operation = None
        self.restore_issue = None
        self.image = None
        self.label = tk.StringVar(parent, f"登录 {self.provider}")
        self.button = ttk.Button(parent, textvariable=self.label, command=self.open)
        self.page_key = f"{self.source}_account"
        self.page = app.pages[self.page_key]
        self.status = tk.StringVar(self.page)

        header = ttk.Frame(self.page, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        app.label(header, f"{self.provider}账号", large=True, background=app.background).grid(
            row=0, column=0, sticky="w"
        )
        self.back_button = app.action(header, f"返回 {self.provider}", lambda: app.show(self.source))
        self.back_button.grid(row=0, column=1, sticky="e", padx=(8, 0))

        card = app.panel(self.page, padding=(round(24 * app.scale), round(19 * app.scale)))
        card.grid(row=1, column=0, pady=(round(30 * app.scale), 0))
        card.columnconfigure(0, weight=1, minsize=round(286 * app.scale))
        self.code_label = ttk.Label(card, text="扫码登录", style="MusicTitle.TLabel", anchor="center")
        self.code_label.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        status = ttk.Label(card, textvariable=self.status, style="MusicStatus.TLabel",
                           wraplength=round(286 * app.scale), anchor="center", justify="center")
        status.grid(row=1, column=0, sticky="ew", pady=(2, 10))
        self.action = app.action(card, "获取二维码", self.begin, primary=True)
        self.action.grid(row=2, column=0)
        self.retry_button = app.action(card, "重试验证", self.restore)
        self.retry_button.grid(row=3, column=0, pady=(8, 0))
        self.retry_button.grid_remove()
        self.restore()

    def restore(self):
        if self.operation or self.app.closing:
            return
        self.operation = "restore"
        self.status.set("正在验证本机登录状态…")
        self.action.configure(state="disabled")
        self.retry_button.grid_remove()
        serial = self.serial
        stop = self.stop

        def check():
            try:
                session = self.auth.load()
                account = self.auth.check(session) if session is not None else None
                if not stop.is_set():
                    self.app.post(self.event, (serial, "restored", (session, account)))
            except (OSError, ValueError, RuntimeError):
                # A network error doesn't invalidate a saved session.
                if not stop.is_set():
                    self.app.post(self.event, (serial, "restore_error", None))

        threading.Thread(target=check, daemon=True).start()

    def open(self):
        if not self.app.closing:
            self.app.show(self.page_key)
            self._start_if_needed()

    def _start_if_needed(self):
        if self.app.active_page == self.page_key and self.session is None and not (self.operation or self.restore_issue):
            self.begin()

    def begin(self):
        if self.app.closing or self.app.active_page != self.page_key or self.session is not None:
            return
        self.close_worker()
        self.operation = "scan"
        serial, stop = self.serial, self.stop
        self.clear_code()
        self.status.set(f"正在向 {self.provider} 获取二维码…")
        self.action.configure(text="重新获取二维码", command=self.begin, state="disabled")
        self.retry_button.grid_remove()

        def login():
            def post(event, value=None):
                if not stop.is_set():
                    self.app.post(self.event, (serial, event, value))

            try:
                qr = self.auth.begin()
                post("qr", self.qr_payload(qr))
                limit = time.monotonic() + 180
                state = "waiting"
                while time.monotonic() < limit and not stop.wait(2):
                    poll = self.auth.poll(qr)
                    if poll.state == "success":
                        try:
                            account = self.auth.check(poll.session)
                        except (OSError, ValueError, RuntimeError):
                            account = None
                        post("signed_in", (poll.session, account))
                        return
                    if poll.state == "expired":
                        break
                    if poll.state != state:
                        post(poll.state)
                        state = poll.state
                post("expired")
            except (OSError, ValueError, RuntimeError) as error:
                post("error", str(error))

        threading.Thread(target=login, daemon=True).start()

    def receive(self, serial, event, value):
        if serial != self.serial or self.app.closing:
            return
        if event == "restored":
            self.operation = None
            session, account = value
            if account is not None:
                self.session, self.account = session, account
                self.kept = True
                self.restore_issue = None
                self.sync_button()
                self.show_account()
            else:
                self.restore_issue = "expired" if session is not None else None
                self.show_signin()
                self._start_if_needed()
            return
        if event == "restore_error":
            self.operation = None
            self.restore_issue = "error"
            self.show_signin()
            return
        if self.operation != "scan" or self.app.active_page != self.page_key:
            return
        if event == "qr":
            try:
                self.image = self.code_image(value)
            except (tk.TclError, ValueError):
                self.close_worker()
                self.show_signin("无法显示二维码，请重新获取。")
                return
            self.code_label.configure(image=self.image, text="")
            self.status.set(self.scan_instruction)
        elif event == "scanned":
            self.status.set("已扫码，请在手机上确认。")
        elif event == "signed_in":
            self.operation = None
            self.session, self.account = value
            self.kept = True
            self.restore_issue = None
            try:
                self.auth.save(self.session)
            except (OSError, ValueError, RuntimeError):
                self.kept = False
            self.sync_button()
            self.show_account()
        elif event in ("expired", "error"):
            self.operation = None
            self.show_signin("二维码已过期，请重新获取。" if event == "expired" else value)

    def sync_button(self):
        self.label.set(f"{self.provider}已登录" if self.session else f"登录 {self.provider}")
        if self.changed is not None:
            self.changed(self.session)

    def clear_code(self, text="扫码登录"):
        self.code_label.configure(image="", text=text)
        self.image = None

    def show_signin(self, message=""):
        self.clear_code()
        instruction = {"error": "暂时无法验证本机登录状态。", "expired": "登录已过期，请重新扫码。"}.get(self.restore_issue, "")
        self.status.set(message or instruction)
        self.action.configure(text="重新获取二维码" if message else "获取二维码",
                              command=self.begin, state="normal")
        if self.restore_issue == "error":
            self.retry_button.grid()
        else:
            self.retry_button.grid_remove()

    def show_account(self):
        name = self.account.name if self.account is not None else f"{self.provider}账号"
        self.clear_code(name)
        self.status.set("已登录" if self.kept else "已登录 · 此次会话有效，重开后请重新扫码")
        self.action.configure(text="退出登录", command=self.logout, state="normal")
        self.retry_button.grid_remove()

    def logout(self):
        try:
            self.auth.clear()
        except (OSError, RuntimeError, ValueError):
            self.status.set("无法清除本机登录信息，请重试。")
            return
        self.session = self.account = None
        self.sync_button()
        self.show_signin("已退出登录。")

    def expire(self):
        if self.session is None:
            return
        self.close_worker()
        self.session = self.account = None
        self.restore_issue = "expired"
        self.sync_button()
        self.show_signin()

    def close_worker(self):
        self.stop.set()
        self.stop = threading.Event()
        self.serial += 1
        self.operation = None

    def leave(self):
        if self.operation == "scan":
            self.close_worker()
            self.show_signin()

    def close(self):
        self.close_worker()

    @staticmethod
    def qr_payload(qr):
        return qr.url

    def code_image(self, payload):
        return qr_image(self.page, payload, self.app.scale)


class BiliAccountPanel(QrAccountPanel):
    source = "bilibili"
    provider = "B 站"
    event = "bili_auth"
    scan_instruction = "用哔哩哔哩 App 扫码，在手机上确认登录。"


class NeteaseAccountPanel(QrAccountPanel):
    source = "netease"
    provider = "网易云音乐"
    event = "netease_auth"
    scan_instruction = "用网易云音乐 App 扫码，在手机上确认登录。"


class QqAccountPanel(QrAccountPanel):
    source = "qq"
    provider = "QQ 音乐"
    event = "qq_auth"
    scan_instruction = "用手机 QQ 扫码，在手机上确认登录。"

    @staticmethod
    def qr_payload(qr):
        return qr.image

    def code_image(self, payload):
        image = tk.PhotoImage(master=self.page, data=base64.b64encode(payload), format="PNG")
        factor = max(1, math.ceil(round(136 * self.app.scale) / image.width()))
        return image.zoom(factor) if factor > 1 else image
