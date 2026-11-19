"""Qt shell main window: navigation rail, stacked pages and status bar."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QSizePolicy, QStackedWidget, QStatusBar,
                               QVBoxLayout, QWidget)

from qtui.pages import build_pages


class MainWindow(QMainWindow):
    """Top-level window of the migrating Qt interface."""

    def __init__(self, qt_app, core, version):
        super().__init__()
        self.app = qt_app
        self.core = core
        self.version = version
        self.setWindowTitle("影音下载台")
        self.setMinimumSize(860, 560)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        rail = QFrame()
        rail.setProperty("brand", True)
        rail.setFixedWidth(168)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(10, 14, 10, 12)
        brand = QLabel("影音下载台")
        brand.setProperty("brand", True)
        rail_layout.addWidget(brand)
        rail_layout.addSpacing(14)
        built = build_pages(qt_app, core)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, title, _widget in built:
            button = QPushButton(title)
            button.setProperty("nav", True)
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda _checked=False, page=key: self.show_page(page))
            self.group.addButton(button)
            self.buttons[key] = button
            rail_layout.addWidget(button)
        rail_layout.addStretch(1)
        self.theme_button = QPushButton("切换深浅主题")
        self.theme_button.clicked.connect(self.toggle_theme)
        rail_layout.addWidget(self.theme_button)
        layout.addWidget(rail)

        self.stack = QStackedWidget()
        self.pages = {}
        for key, _title, widget in built:
            self.pages[key] = widget
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, 1)

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel(f"影音下载台 · {self.version} · 新界面迁移中")
        status.addWidget(self.status_label)

        self.current = None
        self.show_page("queue")

    def show_page(self, key):
        if key not in self.pages:
            return
        if self.current == key:
            return
        self.current = key
        self.stack.setCurrentWidget(self.pages[key])
        if key in self.buttons:
            self.buttons[key].setChecked(True)
        if key == "queue":
            self.pages[key].refresh()

    def toggle_theme(self):
        settings_page = self.pages.get("settings")
        if settings_page is not None and hasattr(settings_page, "toggle_theme"):
            settings_page.toggle_theme()
