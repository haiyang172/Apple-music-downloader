"""The account page and the modal Apple Music verification prompt."""

import re
import tkinter as tk
from tkinter import ttk


def build_account_page(app):
    page = app.pages["account"]
    page.rowconfigure(0, weight=1)
    content = ttk.Frame(page, style="Surface.TFrame")
    content.grid(row=0, column=0)
    content.columnconfigure(0, weight=1, minsize=round(368 * app.scale))

    form = app.panel(content, padding=(24, 22))
    form.grid(row=0, column=0, sticky="ew")
    form.columnconfigure(0, weight=1)
    app.label(form, "登录 Apple Music", large=True).grid(row=0, column=0, sticky="w")
    app.label(form, variable=app.account_status, muted=True, wrap=330).grid(
        row=1, column=0, sticky="w", pady=(4, 0)
    )

    app.label(form, "Apple 账号").grid(row=2, column=0, sticky="w", pady=(16, 4))
    app.user_entry = ttk.Entry(form, textvariable=app.username, width=27)
    app.user_entry.grid(row=3, column=0, sticky="ew")

    app.label(form, "密码").grid(row=4, column=0, sticky="w", pady=(10, 4))
    app.password_entry = ttk.Entry(form, textvariable=app.password, show="•", width=27)
    app.password_entry.grid(row=5, column=0, sticky="ew")

    region = tk.Frame(form, background=ttk.Style(form).lookup("Card.TFrame", "background") or "#ffffff")
    region.grid(row=6, column=0, sticky="w", pady=(10, 0))
    app.label(region, "账号地区").pack(side="left", padx=(0, 8))
    app.region_entry = ttk.Entry(region, textvariable=app.fields["region"], width=5, justify="center")
    app.region_entry.pack(side="left")

    app.login_button = app.action(form, "登录", app.login_account, primary=True)
    app.login_button.grid(row=7, column=0, sticky="ew", pady=(14, 0))
    app.label(form, variable=app.auth_status, muted=True, wrap=330).grid(
        row=8, column=0, sticky="w", pady=(7, 0)
    )
    for field in (app.user_entry, app.password_entry, app.region_entry):
        field.bind("<Return>", lambda _: app.login_button.invoke())

    actions = ttk.Frame(content, style="Surface.TFrame")
    actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
    app.legacy_toggle = app.action(actions, "高级设置", app.toggle_legacy)
    app.legacy_toggle.pack(side="left")
    app.logout_button = app.action(actions, "退出登录", app.logout_account)
    app.logout_button.pack(side="right")

    app.legacy_card = app.panel(content, padding=(15, 12))
    app.legacy_card.grid(row=2, column=0, sticky="ew", pady=(10, 0))
    app.legacy_card.columnconfigure(0, weight=1)
    app.label(app.legacy_card, "兼容会话令牌").grid(row=0, column=0, sticky="w", pady=(0, 4))
    ttk.Entry(app.legacy_card, textvariable=app.token, show="•").grid(
        row=1, column=0, sticky="ew"
    )
    app.action(app.legacy_card, "保存会话", app.save_account).grid(
        row=2, column=0, sticky="w", pady=(8, 0)
    )
    app.legacy_card.grid_remove()


class VerificationDialog:
    """Prompt for an active Apple login challenge without modifying the form."""

    def __init__(self, app, submit, cancel):
        self._submit = submit
        self._cancel = cancel
        self._busy = False
        self._dismissed = False
        parent = app.window
        self.window = window = tk.Toplevel(parent, background=app.background)
        window.withdraw()
        window.title("Apple Music 安全验证")
        window.transient(parent)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self._cancel_request)
        window.bind("<Escape>", self._cancel_request)
        window.bind("<Return>", self._submit_request)

        self._code = tk.StringVar(window)
        self._message = tk.StringVar(window)
        card = app.panel(window, padding=(21, 18))
        card.pack(fill="both", expand=True, padx=12, pady=12)
        card.columnconfigure(0, weight=1, minsize=round(285 * app.scale))
        app.label(card, "输入验证码", large=True).grid(row=0, column=0, sticky="w")
        app.label(card, "输入 Apple 发送的 6 位验证码。", muted=True, wrap=280).grid(
            row=1, column=0, sticky="w", pady=(6, 12)
        )
        digits = window.register(lambda proposed: re.fullmatch(r"[0-9]{0,6}", proposed) is not None)
        self.entry = ttk.Entry(
            card, textvariable=self._code, width=12, justify="center",
            font=(app.font, 19, "bold"), validate="key", validatecommand=(digits, "%P"),
        )
        self.entry.grid(row=2, column=0, sticky="ew")
        app.label(card, variable=self._message, muted=True, wrap=280).grid(
            row=3, column=0, sticky="w", pady=(7, 0)
        )
        self.cancel_button = app.action(card, "取消", self._cancel_request)
        self.cancel_button.grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.verify_button = app.action(card, "验证", self._submit_request, primary=True)
        self.verify_button.grid(row=4, column=0, sticky="e", pady=(10, 0))

        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
        window.deiconify()
        window.grab_set()
        self.focus()

    def focus(self):
        if not self._dismissed and not self._busy:
            self.window.lift()
            self.entry.focus_set()

    def set_busy(self, busy):
        if self._dismissed:
            return
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.entry.configure(state=state)
        self.cancel_button.configure(state=state)
        self.verify_button.configure(state=state, text="验证中…" if busy else "验证")

    def set_error(self, message):
        if not self._dismissed:
            self._message.set(message)
            self.entry.selection_range(0, "end")

    def dismiss(self):
        if not self._dismissed:
            self._dismissed = True
            self.window.grab_release()
            self.window.destroy()

    def _submit_request(self, _event=None):
        if not self._dismissed and not self._busy:
            code = self._code.get()
            if re.fullmatch(r"[0-9]{6}", code):
                self._message.set("")
                self._submit(code)
            else:
                self.set_error("请输入完整的 6 位验证码。")
                self.focus()
        return "break"

    def _cancel_request(self, _event=None):
        if not self._dismissed and not self._busy:
            self.dismiss()
            self._cancel()
        return "break"
