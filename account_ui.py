"""Apple Music download login and library connection dialogs."""

from queue import Empty, SimpleQueue
import re
import threading
import tkinter as tk
from tkinter import ttk
import webbrowser

from music_library import LibraryClient, LibrarySessionRequired
import settings


def build_account_page(app):
    page = app.pages["account"]
    header = ttk.Frame(page, style="Surface.TFrame")
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(0, weight=1)
    app.label(header, "Apple Music 账户", large=True, background=app.background).grid(row=0, column=0, sticky="w")
    app.apple_account_back = app.action(header, "返回", lambda: app.show(app.apple_account_return))
    app.apple_account_back.grid(row=0, column=1, sticky="e", padx=(8, 0))

    page.rowconfigure(1, weight=1)
    content = ttk.Frame(page, style="Surface.TFrame")
    content.grid(row=1, column=0, pady=(10, 0))
    content.columnconfigure(0, weight=1, minsize=round(368 * app.scale))

    form = app.panel(content, padding=(24, 22))
    form.grid(row=0, column=0, sticky="ew")
    form.columnconfigure(0, weight=1)
    ttk.Label(form, text="下载账户", style="MusicTitle.TLabel").grid(row=0, column=0, sticky="w")
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

    library = app.panel(content, padding=(15, 12))
    library.grid(row=2, column=0, sticky="ew", pady=(10, 0))
    library.columnconfigure(0, weight=1)
    app.label(library, "Apple Music 资料库").grid(row=0, column=0, sticky="w")
    library_status = tk.StringVar(library)
    app.label(library, variable=library_status, muted=True).grid(row=1, column=0, sticky="w", pady=(3, 0))
    library_action = app.action(library, "连接资料库", app.connect_library)
    library_action.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))
    reconnect = app.action(library, "重新连接", app.connect_library)
    reconnect.grid(row=0, column=2, rowspan=2, sticky="e", padx=(5, 0))

    def update_library_access(*_):
        saved = bool(app.preferences.get("session"))
        library_status.set("网页会话已保存" if saved else "等待连接")
        if saved:
            library_action.configure(text="查看资料库", command=lambda: app.show("library"))
            reconnect.grid()
        else:
            library_action.configure(text="连接资料库", command=app.connect_library)
            reconnect.grid_remove()

    update_library_access()
    app.account_status.trace_add("write", update_library_access)

    app.legacy_card = app.panel(content, padding=(15, 12))
    app.legacy_card.grid(row=3, column=0, sticky="ew", pady=(10, 0))
    app.legacy_card.columnconfigure(0, weight=1)
    app.label(app.legacy_card, "兼容会话令牌").grid(row=0, column=0, sticky="w", pady=(0, 4))
    ttk.Entry(app.legacy_card, textvariable=app.token, show="•").grid(
        row=1, column=0, sticky="ew"
    )
    app.action(app.legacy_card, "保存会话", app.save_account).grid(
        row=2, column=0, sticky="w", pady=(8, 0)
    )
    app.legacy_card.grid_remove()


def show_library_connection(app):
    """Open the existing-token connection for the Apple Music library."""
    dialog = getattr(app, "library_connection_dialog", None)
    if dialog is None or not dialog.window.winfo_exists():
        app.library_connection_dialog = LibraryConnectionDialog(app)
    else:
        dialog.window.lift()


class LibraryConnectionDialog:
    def __init__(self, app):
        self.app = app
        self._result = SimpleQueue()
        self._poll = None
        self._closing = False

        self.window = window = tk.Toplevel(app.window, background=app.background)
        window.title("连接 Apple Music 资料库")
        window.transient(app.window)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self._dismiss)
        window.bind("<Escape>", self._dismiss)
        window.bind("<Return>", self._connect)

        self.token = tk.StringVar(window)
        self.status = tk.StringVar(window)
        card = app.panel(window, padding=(20, 16))
        card.pack(fill="both", expand=True, padx=12, pady=12)
        card.columnconfigure(0, weight=1, minsize=round(333 * app.scale))
        app.label(card, "连接资料库", large=True).grid(row=0, column=0, sticky="w")
        app.label(card, "输入你要连接的 Apple Music 账户的 Music User Token。", muted=True, wrap=330).grid(
            row=1, column=0, sticky="w", pady=(6, 10)
        )
        self.entry = ttk.Entry(card, textvariable=self.token, show="•", width=42)
        self.entry.grid(row=2, column=0, sticky="ew")
        app.label(card, variable=self.status, muted=True, wrap=330).grid(row=3, column=0, sticky="w", pady=(7, 0))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        app.action(actions, "打开 Apple Music", lambda: webbrowser.open("https://music.apple.com/")).pack(side="left")
        self.submit = app.action(actions, "连接", self._connect, primary=True)
        self.submit.pack(side="right")
        app.action(actions, "取消", self._dismiss).pack(side="right", padx=(5, 7))

        window.grab_set()
        self.entry.focus_set()

    def _connect(self, _event=None):
        if self._closing or self._poll is not None or self.app.closing:
            return "break"
        token = self.token.get().strip()
        if not re.fullmatch(r"[\x21-\x7e]{50,8192}", token):
            self.status.set("请输入完整的 Music User Token。")
            self.entry.focus_set()
            return "break"
        preferences = self.app.preferences
        previous = (preferences.get("session"), preferences.get("native_account"))
        region = self.app.catalog_region()
        catalog = self.app.catalog
        self.entry.configure(state="disabled")
        self.submit.configure(state="disabled", text="验证中…")
        self.status.set("正在验证 Apple Music 资料库权限…")
        self._poll = self.window.after(70, self._receive)
        threading.Thread(target=self._verify, args=(token, region, catalog, previous), daemon=True).start()
        return "break"

    def _verify(self, token, region, catalog, previous):
        try:
            LibraryClient(token, region, catalog=catalog).items(limit=1)
            protected = settings.protect_session(token)
        except LibrarySessionRequired:
            self._result.put((None, "Apple Music 未接受此资料库会话，请确认令牌仍有效。", previous))
        except (OSError, RuntimeError, ValueError) as error:
            self._result.put((None, str(error), previous))
        else:
            self._result.put((protected, "", previous))

    def _receive(self):
        self._poll = None
        if self._closing or self.app.closing:
            return
        try:
            protected, error, previous = self._result.get_nowait()
        except Empty:
            self._poll = self.window.after(70, self._receive)
            return
        if protected is not None:
            preferences = self.app.preferences
            if previous != (preferences.get("session"), preferences.get("native_account")):
                error = "Apple Music 连接状态已变化，请重新验证。"
            else:
                try:
                    self.app.store({**preferences, "session": protected})
                except (OSError, RuntimeError, ValueError) as failure:
                    error = str(failure)
                else:
                    self._dismiss()
                    self.app.show("library")
                    return
        self.status.set(error or "暂时无法验证资料库会话，请重试。")
        self.entry.configure(state="normal")
        self.submit.configure(state="normal", text="连接")
        self.entry.focus_set()

    def _dismiss(self, _event=None):
        self._closing = True
        if self._poll is not None:
            self.window.after_cancel(self._poll)
            self._poll = None
        self.token.set("")
        self.window.destroy()
        self.app.library_connection_dialog = None
        return "break"


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
