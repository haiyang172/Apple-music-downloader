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
        self.serial = 0
        self.dialog = None
        self.stop = threading.Event()
        self.label = tk.StringVar(parent, f"登录 {self.provider}")
        self.status = tk.StringVar(parent)
        self.button = ttk.Button(parent, textvariable=self.label, command=self.open)
        self.restore()

    def restore(self):
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
        if self.dialog is not None:
            self.dialog.lift()
            return
        dialog = tk.Toplevel(self.app.window)
        self.dialog = dialog
        dialog.title(f"{self.provider}账号")
        dialog.transient(self.app.window)
        dialog.configure(background=self.app.background, padx=18, pady=14)
        dialog.columnconfigure(0, weight=1)
        dialog.protocol("WM_DELETE_WINDOW", self.close)
        dialog.bind("<Escape>", lambda _: self.close())
        card = self.app.panel(dialog)
        card.grid(sticky="nsew")
        card.columnconfigure(0, weight=1)
        self.code_label = ttk.Label(card, style="MusicTitle.TLabel", anchor="center")
        self.code_label.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        status = ttk.Label(card, textvariable=self.status, style="MusicStatus.TLabel", wraplength=320, justify="center")
        status.grid(row=1, column=0, sticky="ew", pady=(2, 10))
        self.action = self.app.action(card, "重新获取二维码", self.begin)
        self.action.grid(row=2, column=0)
        if self.session is None:
            self.begin()
        else:
            self.show_account()

    def begin(self):
        self.close_worker()
        serial, stop = self.serial, self.stop
        self.code_label.configure(image="", text="扫码登录")
        self.status.set(f"正在向 {self.provider} 获取二维码…")
        self.action.configure(text="重新获取二维码", command=self.begin, state="disabled")

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
            session, account = value
            if account is not None:
                self.session, self.account = session, account
                self.sync_button()
            return
        if event == "restore_error":
            return
        if self.dialog is None:
            return
        if event == "qr":
            try:
                self.image = self.code_image(value)
            except (tk.TclError, ValueError):
                self.close_worker()
                self.status.set("无法显示二维码，请重新获取。")
                self.action.configure(state="normal")
                return
            self.code_label.configure(image=self.image, text="")
            self.status.set(self.scan_instruction)
        elif event == "scanned":
            self.status.set("已扫码，请在手机上确认。")
        elif event == "signed_in":
            self.session, self.account = value
            kept = True
            try:
                self.auth.save(self.session)
            except (OSError, ValueError, RuntimeError):
                kept = False
            self.sync_button()
            self.show_account(kept)
        elif event in ("expired", "error"):
            self.code_label.configure(image="", text="扫码登录")
            self.status.set("二维码已过期，请重新获取。" if event == "expired" else value)
            self.action.configure(state="normal")

    def sync_button(self):
        self.label.set(f"{self.provider}已登录" if self.session else f"登录 {self.provider}")
        if self.changed is not None:
            self.changed(self.session)

    def show_account(self, kept=True):
        name = self.account.name if self.account is not None else f"{self.provider}账号"
        self.code_label.configure(image="", text=name)
        self.status.set("已登录" if kept else "已登录 · 当前会话有效")
        self.action.configure(text="退出登录", command=self.logout, state="normal")

    def logout(self):
        try:
            self.auth.clear()
        except (OSError, RuntimeError, ValueError):
            self.status.set("无法清除本机登录信息，请重试。")
            return
        self.session = self.account = None
        self.sync_button()
        self.close()

    def close_worker(self):
        self.stop.set()
        self.stop = threading.Event()
        self.serial += 1

    def close(self):
        self.close_worker()
        if self.dialog is not None:
            self.dialog.destroy()
            self.dialog = None

    @staticmethod
    def qr_payload(qr):
        return qr.url

    def code_image(self, payload):
        return qr_image(self.dialog, payload, self.app.scale)


class BiliAccountPanel(QrAccountPanel):
    provider = "B 站"
    event = "bili_auth"
    scan_instruction = "用哔哩哔哩 App 扫码，在手机上确认登录。"


class NeteaseAccountPanel(QrAccountPanel):
    provider = "网易云音乐"
    event = "netease_auth"
    scan_instruction = "用网易云音乐 App 扫码，在手机上确认登录。"


class QqAccountPanel(QrAccountPanel):
    provider = "QQ 音乐"
    event = "qq_auth"
    scan_instruction = "用手机 QQ 扫码，在手机上确认登录。"

    @staticmethod
    def qr_payload(qr):
        return qr.image

    def code_image(self, payload):
        image = tk.PhotoImage(master=self.dialog, data=base64.b64encode(payload), format="PNG")
        factor = max(1, math.ceil(round(136 * self.app.scale) / image.width()))
        return image.zoom(factor) if factor > 1 else image
