"""Light and dark palettes for the Qt shell."""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


ACCENT = "#5B8DEF"

_LIGHT = {
    QPalette.Window: "#F4F6FA",
    QPalette.WindowText: "#1F2430",
    QPalette.Base: "#FFFFFF",
    QPalette.AlternateBase: "#F7F9FC",
    QPalette.Text: "#1F2430",
    QPalette.Button: "#FFFFFF",
    QPalette.ButtonText: "#1F2430",
    QPalette.Highlight: ACCENT,
    QPalette.HighlightedText: "#FFFFFF",
    QPalette.ToolTipBase: "#1F2430",
    QPalette.ToolTipText: "#FFFFFF",
    QPalette.PlaceholderText: "#8A93A5",
    QPalette.Link: ACCENT,
}

_DARK = {
    QPalette.Window: "#171A21",
    QPalette.WindowText: "#E6EAF2",
    QPalette.Base: "#20242E",
    QPalette.AlternateBase: "#262B36",
    QPalette.Text: "#E6EAF2",
    QPalette.Button: "#262B36",
    QPalette.ButtonText: "#E6EAF2",
    QPalette.Highlight: ACCENT,
    QPalette.HighlightedText: "#FFFFFF",
    QPalette.ToolTipBase: "#E6EAF2",
    QPalette.ToolTipText: "#171A21",
    QPalette.PlaceholderText: "#7A8499",
    QPalette.Link: "#7FA7FF",
}

_QSS = """
QPushButton[nav="true"] {
    border: none; border-radius: 8px; padding: 8px 14px; text-align: left;
}
QPushButton[nav="true"]:checked { background: {accent}; color: white; }
QPushButton[nav="true"]:hover:!checked { background: {hover}; }
QFrame[brand="true"] { font-size: 15px; font-weight: bold; }
QHeaderView::section { border: none; padding: 6px 8px; }
"""


def apply_theme(app: QApplication, dark: bool):
    """Replace the application palette and navigation stylesheet."""
    if not isinstance(app, QApplication):
        return
    palette = QPalette()
    colors = _DARK if dark else _LIGHT
    for role, value in colors.items():
        palette.setColor(QPalette.All, role, QColor(value))
        palette.setColor(QPalette.Disabled, role, QColor(value).darker(140) if dark else QColor(value).lighter(115))
    app.setPalette(palette)
    app.setStyle("Fusion")
    hover = "#343B49" if dark else "#E8EDF6"
    app.setStyleSheet(_QSS.replace("{accent}", ACCENT).replace("{hover}", hover))
