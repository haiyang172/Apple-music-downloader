"""Qt Widgets shell for 影音下载台 — the migration target UI.

The Tkinter Studio.py remains the default interface (Run.cmd) until these
pages reach parity. Launch this shell with RunQt.cmd or
`runtime\\python.exe StudioQt.py`.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_TITLE = "影音下载台"
try:
    APP_VERSION = (ROOT / "app-version.txt").read_text(encoding="ascii").strip()
except (OSError, UnicodeError):
    APP_VERSION = ""
for library in (ROOT.parent / "playlist-download" / "python-deps", ROOT / "lib"):
    if library.is_dir():
        sys.path.insert(0, str(library))

import settings  # noqa: E402
from qtui.core import QtApp  # noqa: E402
from qtui.main_window import MainWindow  # noqa: E402
from qtui.theme import apply_theme  # noqa: E402


def build_window(dark=None):
    """Create the shell window (used by tests as well as main())."""
    from PySide6.QtWidgets import QApplication

    qt_app = QApplication.instance() or QApplication(sys.argv[:1])
    dark = bool(settings.load().get("dark_mode", False)) if dark is None else bool(dark)
    apply_theme(qt_app, dark)
    core = QtApp(ROOT)
    window = MainWindow(qt_app, core, APP_VERSION)
    window.resize(1040, 700)
    return window


def main():
    from PySide6.QtWidgets import QApplication

    window = build_window()
    window.show()
    sys.exit(QApplication.instance().exec())


if __name__ == "__main__":
    main()
