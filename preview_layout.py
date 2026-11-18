"""Shared scrolling card and fixed actions for download previews."""

import tkinter as tk
from tkinter import ttk


class PreviewLayout:
    def __init__(self, app, title, on_close, *, width=550):
        self.app = app
        self.width = width
        self._scroll_bound = False
        window = self.window = tk.Toplevel(app.window, background=app.background)
        window.withdraw()
        window.title(title)
        window.transient(app.window)
        window.resizable(True, False)
        window.protocol("WM_DELETE_WINDOW", on_close)
        window.bind("<Escape>", on_close)
        try:
            icon = app.window.iconbitmap()
            if icon:
                window.iconbitmap(icon)
        except tk.TclError:
            pass

        # Pack the footer first so a short viewport only reduces the scroll area.
        self.actions = app.panel(window, padding=(20, 12))
        self.actions.pack(side="bottom", fill="x", padx=12, pady=(0, 12))
        self.body = tk.Frame(window, background=app.background)
        self.body.pack(side="top", fill="both", expand=True, padx=12, pady=(12, 8))
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.body, background=app.background, borderwidth=0, highlightthickness=0,
                                yscrollincrement=max(12, round(16 * app.scale)))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.body, command=self.canvas.yview, style="Music.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.card = app.panel(self.canvas, padding=(20, 17))
        self.body_item = self.canvas.create_window(0, 0, window=self.card, anchor="nw")
        self.canvas.bind("<Configure>", self.fit_body)
        self.card.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def fit_body(self, event):
        self.canvas.itemconfigure(self.body_item, width=event.width)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self.scroll, add="+")
        for child in widget.winfo_children():
            self.bind_scroll(child)

    def scroll(self, event):
        if event.delta:
            units = max(1, abs(event.delta) // 120)
            self.canvas.yview_scroll(-units if event.delta > 0 else units, "units")
        return "break"

    def show(self):
        window, app = self.window, self.app
        window.update_idletasks()
        self.canvas.configure(height=min(self.card.winfo_reqheight(), round(430 * app.scale)))
        if not self._scroll_bound:
            self.bind_scroll(self.body)
            self._scroll_bound = True
        window.update_idletasks()
        width = max(1, min(round(self.width * app.scale), app.window.winfo_screenwidth() - 60))
        height = max(1, min(window.winfo_reqheight(), app.window.winfo_screenheight() - 80))
        window.minsize(min(round(500 * app.scale), width), min(round(250 * app.scale), height))
        left = app.window.winfo_rootx() + (app.window.winfo_width() - width) // 2
        top = app.window.winfo_rooty() + (app.window.winfo_height() - height) // 2
        # The leading '+' keeps negative desktop coordinates absolute in Tk.
        window.geometry(f"{width}x{height}+{left}+{top}")
        window.deiconify()
