"""Placeholder pages and the read-only download queue page for the Qt shell."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import settings
from download_queue import QueueStore


TASK_LABELS = {
    "pending": "待下载", "running": "下载中", "complete": "已完成",
    "failed": "失败", "interrupted": "已中断", "cancelled": "已取消",
}

PAGES = (
    ("music", "Apple Music"),
    ("bilibili", "B 站"),
    ("qq", "QQ 音乐"),
    ("netease", "网易云音乐"),
    ("youtube", "YouTube"),
    ("douyin", "抖音"),
    ("queue", "下载队列"),
    ("local", "本地转码"),
    ("settings", "设置"),
)


class PlaceholderPage(QWidget):
    """A page that is not migrated yet; the Tkinter Studio still covers it."""

    def __init__(self, title, note):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        heading = QLabel(f"{title} · 迁移中")
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(heading)
        hint = QLabel(note)
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setMaximumWidth(420)
        layout.addWidget(hint)
        hint.setProperty("muted", True)


class QueuePage(QWidget):
    """Read-only table of persisted download tasks."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.summary = QLabel("下载队列")
        toolbar.addWidget(self.summary)
        toolbar.addStretch(1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("歌名", "歌手", "批次", "位置", "状态", "文件"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)
        self.store = None
        self.refresh()

    def refresh(self):
        try:
            if self.store is None:
                self.store = QueueStore(settings.DATA / "downloads.sqlite3")
            tasks = self.store.list_tasks()
        except (OSError, RuntimeError, ValueError) as error:
            tasks = []
            self.summary.setText(f"无法读取下载队列 · {error}")
        else:
            pending = sum(task.state in ("pending", "running", "interrupted") for task in tasks)
            self.summary.setText(f"下载队列 · 共 {len(tasks)} 项，其中待处理 {pending} 项")
        self.table.setRowCount(0)
        for task in sorted(tasks, key=lambda item: item.id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            position = f"{task.position}/{task.total}" if task.position and task.total else ""
            values = (task.title, task.artist or "", task.batch_title or "", position,
                      TASK_LABELS.get(task.state, task.state), str(len(task.files)))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 4:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)


class SettingsPage(QWidget):
    """Skeleton settings: theme toggle, with the remaining options migrating later."""

    def __init__(self, app, dark):
        super().__init__()
        self.app = app
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        heading = QLabel("设置")
        heading.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(heading)
        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setMaximumWidth(520)
        form = QVBoxLayout(frame)
        row = QHBoxLayout()
        row.addWidget(QLabel("界面主题"))
        row.addStretch(1)
        self.theme_button = QPushButton()
        self.theme_button.clicked.connect(self.toggle_theme)
        row.addWidget(self.theme_button)
        form.addLayout(row)
        note = QLabel("其余设置与功能仍在迁移；请继续使用原界面（Run.cmd）完成下载和账户操作。")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        form.addWidget(note)
        layout.addWidget(frame)
        self.dark = bool(dark)
        self._theme_text()

    def _theme_text(self):
        self.theme_button.setText("深色模式" if not self.dark else "浅色模式")

    def toggle_theme(self):
        self.dark = not self.dark
        self._theme_text()
        from qtui.theme import apply_theme
        apply_theme(self.app, self.dark)
        values = settings.load()
        values["dark_mode"] = self.dark
        settings.save(values)


def build_pages(app):
    """Return an ordered list of (key, title, widget) pages for the shell."""
    pages = []
    for key, title in PAGES:
        if key == "queue":
            widget = QueuePage()
        elif key == "settings":
            widget = SettingsPage(app, settings.load().get("dark_mode", False))
        else:
            widget = PlaceholderPage(title, "此页面正在迁移到新界面，当前版本仍可使用原界面完成该平台操作。")
        pages.append((key, title, widget))
    return pages
