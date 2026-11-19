"""Pages for the Qt shell: placeholders, the actionable queue and local conversion."""

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import settings
import audio
from local_convert import formats_for

TASK_LABELS = {
    "pending": "待下载", "running": "下载中", "complete": "已完成",
    "failed": "失败", "interrupted": "已中断", "cancelled": "已取消",
}

LOCAL_FORMATS = {
    "mp3": "MP3", "m4a": "M4A（AAC）", "flac": "FLAC", "wav": "WAV", "opus": "Opus",
    "mp4": "MP4", "mkv": "MKV", "webm": "WebM",
}

QUALITY = {"AAC": "AAC-LC · 兼容", "ALAC": "ALAC · 无损", "ATMOS": "Dolby Atmos"}
BILI_FORMATS = {"video": "视频 · MP4", "audio": "音频 · M4A", "flac": "音频 · 原始 FLAC", "mp3": "音频 · MP3"}
YOUTUBE_FORMATS = {"video": "视频 · MP4", "audio": "音频 · M4A", "mp3": "音频 · MP3"}
MUSIC_QUALITIES = {
    "qq": {"auto": "自动 · 最佳可用", "128": "MP3 · 128 kbps", "320": "MP3 · 320 kbps", "flac": "FLAC · 无损"},
    "netease": {"auto": "自动 · 最佳可用", "128": "标准", "192": "较高", "320": "极高",
                "flac": "无损", "hires": "Hi-Res · 无损"},
}
UI_MODES = {"tkinter": "Tkinter（经典界面）", "qt": "Qt（新界面）"}

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
        hint.setProperty("muted", True)
        layout.addWidget(hint)


class QueuePage(QWidget):
    """Actionable download queue backed by the shared queue controller."""

    def __init__(self, core):
        super().__init__()
        self.core = core
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.summary = QLabel("下载队列")
        toolbar.addWidget(self.summary)
        toolbar.addStretch(1)
        self.start_button = QPushButton("开始 / 继续")
        self.start_button.clicked.connect(self.start)
        toolbar.addWidget(self.start_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.core.cancel)
        toolbar.addWidget(self.cancel_button)
        self.remove_button = QPushButton("移除所选")
        self.remove_button.clicked.connect(self.remove_selected)
        toolbar.addWidget(self.remove_button)
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
        for column in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.ids = {}
        self.refresh()

    def selected_ids(self):
        return [self.ids[row] for row in {index.row() for index in self.table.selectedIndexes()}
                if row in self.ids]

    def refresh(self):
        try:
            tasks = self.core.list_tasks()
        except (OSError, RuntimeError, ValueError) as error:
            tasks = []
            self.summary.setText(f"无法读取下载队列 · {error}")
        else:
            pending = sum(task.state in ("pending", "running", "interrupted") for task in tasks)
            self.summary.setText(f"下载队列 · 共 {len(tasks)} 项，其中待处理 {pending} 项")
        self.table.setRowCount(0)
        self.ids = {}
        for task in sorted(tasks, key=lambda item: item.id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.ids[row] = task.id
            position = f"{task.position}/{task.total}" if task.position and task.total else ""
            values = (task.title, task.artist or "", task.batch_title or "", position,
                      TASK_LABELS.get(task.state, task.state), str(len(task.files)))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 4:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)

    def start(self):
        self.core.start()
        self.refresh()

    def remove_selected(self):
        ids = self.selected_ids()
        if ids:
            self.core.remove(ids)
            self.refresh()

    def poll(self):
        while True:
            try:
                kind, value = self.core.messages.get_nowait()
            except Exception:
                break
            if kind == "status":
                self.status.setText(value)
            elif kind == "log":
                self.status.setText(value)
            elif kind in ("done", "failed"):
                self.core.busy = False
                if kind == "failed":
                    self.status.setText(str(value))
                elif isinstance(value, tuple):
                    saved, existing, errors, waiting = value
                    self.status.setText(f"处理结束 · 新增 {saved} · 已存在 {existing} · 失败 {errors}"
                                        + (f" · 等待磁盘 {waiting}" if waiting else ""))
                self.refresh()
        self.start_button.setEnabled(not self.core.busy)


class LocalPage(QWidget):
    """Add local files, pick an output format and hand them to the queue."""

    def __init__(self, core):
        super().__init__()
        self.core = core
        self.files = []
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        heading = QLabel("本地转码")
        heading.setStyleSheet("font-size: 20px; font-weight: bold;")
        toolbar.addWidget(heading)
        toolbar.addStretch(1)
        self.add_button = QPushButton("添加文件")
        self.add_button.clicked.connect(self.choose_files)
        toolbar.addWidget(self.add_button)
        self.tag_button = QPushButton("编辑标签")
        self.tag_button.clicked.connect(self.open_tag_editor)
        toolbar.addWidget(self.tag_button)
        self.remove_button = QPushButton("移除所选")
        self.remove_button.clicked.connect(self.remove_selected)
        toolbar.addWidget(self.remove_button)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(("文件名", "类型"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        layout.addWidget(self.table, 1)

        options = QHBoxLayout()
        options.addWidget(QLabel("输出格式"))
        self.format_box = QComboBox()
        options.addWidget(self.format_box)
        options.addWidget(QLabel("保存位置"))
        self.folder_button = QPushButton("原文件目录")
        self.folder_button.clicked.connect(self.choose_folder)
        options.addWidget(self.folder_button)
        options.addStretch(1)
        self.start_button = QPushButton("转码并加入队列")
        self.start_button.clicked.connect(self.start)
        options.addWidget(self.start_button)
        layout.addLayout(options)

        self.status = QLabel("添加音频或视频文件后选择输出格式。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.folder = ""
        self._update_formats()

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加本地影音文件")
        if paths:
            self.add_files(paths)

    def open_tag_editor(self):
        from qtui.tag_editor import TagEditorDialog
        dialog = TagEditorDialog(self)
        dialog.exec()

    def add_files(self, paths):
        known = {os.path.normcase(path) for path in self.files}
        added = 0
        for value in paths:
            path = Path(value)
            key = os.path.normcase(str(path))
            if key in known or not path.is_file() or not formats_for(path):
                continue
            self.files.append(str(path))
            known.add(key)
            added += 1
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(path.name))
            self.table.setItem(row, 1, QTableWidgetItem(path.suffix.upper().lstrip(".")))
        self.status.setText(f"{len(self.files)} 个文件")
        self._update_formats()

    def remove_selected(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)
            del self.files[row]
        self.status.setText(f"{len(self.files)} 个文件")
        self._update_formats()

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择转码保存位置",
                                                  self.folder or str(Path.home()))
        if folder:
            self.folder = folder
            self.folder_button.setText(folder)

    def _common_formats(self):
        common = set(LOCAL_FORMATS)
        for path in self.files:
            common.intersection_update(formats_for(Path(path)))
        return [key for key in LOCAL_FORMATS if key in common]

    def _update_formats(self):
        keys = self._common_formats()
        self.format_box.clear()
        self.format_box.addItems([LOCAL_FORMATS[key] for key in keys])
        self.start_button.setEnabled(bool(keys and self.files))

    def start(self):
        if not self.files:
            return
        label = self.format_box.currentText()
        key = next((k for k, v in LOCAL_FORMATS.items() if v == label), None)
        if key is None:
            return
        ids = self.core.queue_local(self.files, key, self.folder)
        if ids:
            self.status.setText(f"已加入 {len(ids)} 个转码任务")
            self.core.start(ids)


class SettingsPage(QWidget):
    def __init__(self, app, dark):
        super().__init__()
        self.app = app
        self.dark = bool(dark)
        self.preferences = settings.load()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        heading = QLabel("设置")
        heading.setStyleSheet("font-size: 20px; font-weight: bold;")
        layout.addWidget(heading)

        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setMaximumWidth(620)
        form = QVBoxLayout(frame)
        form.setSpacing(8)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("保存目录"))
        self.folder = QLineEdit(str(self.preferences.get("folder", "")))
        folder_row.addWidget(self.folder, 1)
        browse = QPushButton("浏览")
        browse.clicked.connect(self.choose_folder)
        folder_row.addWidget(browse)
        form.addLayout(folder_row)

        category_row = QHBoxLayout()
        category_row.addWidget(QLabel("分类文件夹"))
        self.category = QLineEdit(str(self.preferences.get("category", "")))
        category_row.addWidget(self.category, 1)
        form.addLayout(category_row)

        self.output = QComboBox()
        self.output.addItems(list(audio.OUTPUTS.values()))
        current_output = audio.OUTPUTS.get(self.preferences.get("output", "original"), "保留原音轨")
        self.output.setCurrentText(current_output)
        form.addLayout(self._row("输出格式", self.output))

        self.mp3_bitrate = QComboBox()
        self.mp3_bitrate.addItems([str(value) for value in audio.MP3_BITRATES])
        self.mp3_bitrate.setCurrentText(str(self.preferences.get("mp3_bitrate", audio.DEFAULT_MP3_BITRATE)))
        form.addLayout(self._row("MP3 码率", self.mp3_bitrate))

        self.apple_quality = QComboBox()
        self.apple_quality.addItems(list(QUALITY.values()))
        self.apple_quality.setCurrentText(QUALITY.get(self.preferences.get("quality", "AAC"), QUALITY["AAC"]))
        form.addLayout(self._row("Apple 音质", self.apple_quality))

        for source, label in (("qq", "QQ 来源音质"), ("netease", "网易云来源音质")):
            box = QComboBox()
            box.addItems(list(MUSIC_QUALITIES[source].values()))
            box.setCurrentText(MUSIC_QUALITIES[source].get(self.preferences.get(f"{source}_quality", "auto"),
                                                          MUSIC_QUALITIES[source]["auto"]))
            setattr(self, f"{source}_quality", box)
            form.addLayout(self._row(label, box))

        self.bili_format = QComboBox()
        self.bili_format.addItems(list(BILI_FORMATS.values()))
        self.bili_format.setCurrentText(BILI_FORMATS.get(self.preferences.get("bili_mode", "video"), BILI_FORMATS["video"]))
        form.addLayout(self._row("B 站格式", self.bili_format))

        self.youtube_format = QComboBox()
        self.youtube_format.addItems(list(YOUTUBE_FORMATS.values()))
        self.youtube_format.setCurrentText(YOUTUBE_FORMATS.get(self.preferences.get("youtube_mode", "video"),
                                                               YOUTUBE_FORMATS["video"]))
        form.addLayout(self._row("YouTube 格式", self.youtube_format))

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("界面主题"))
        self.theme_button = QPushButton()
        self.theme_button.clicked.connect(self.toggle_theme)
        theme_row.addWidget(self.theme_button)
        theme_row.addStretch(1)
        form.addLayout(theme_row)

        self.ui_mode = QComboBox()
        self.ui_mode.addItems(list(UI_MODES.values()))
        self.ui_mode.setCurrentText(UI_MODES.get(self.preferences.get("ui_mode", "qt"), UI_MODES["qt"]))
        form.addLayout(self._row("启动界面", self.ui_mode))

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.save_button = QPushButton("保存设置")
        self.save_button.clicked.connect(self.save)
        save_row.addWidget(self.save_button)
        form.addLayout(save_row)

        note = QLabel("其余设置与功能仍在迁移；请继续使用原界面（Run.cmd）完成平台操作。")
        note.setWordWrap(True)
        note.setProperty("muted", True)
        form.addWidget(note)
        layout.addWidget(frame)
        self._theme_text()

    @staticmethod
    def _row(label, widget):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        row.addWidget(widget, 1)
        return row

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择下载保存目录",
                                                  self.folder.text() or str(Path.home()))
        if folder:
            self.folder.setText(folder)

    def save(self):
        values = dict(settings.load())
        values["folder"] = self.folder.text().strip()
        values["category"] = self.category.text().strip()
        values["output"] = next((key for key, label in audio.OUTPUTS.items()
                                 if label == self.output.currentText()), "original")
        try:
            values["mp3_bitrate"] = int(self.mp3_bitrate.currentText())
        except ValueError:
            values["mp3_bitrate"] = audio.DEFAULT_MP3_BITRATE
        values["quality"] = next((key for key, label in QUALITY.items()
                                  if label == self.apple_quality.currentText()), "AAC")
        for source in ("qq", "netease"):
            box = getattr(self, f"{source}_quality")
            values[f"{source}_quality"] = next((key for key, label in MUSIC_QUALITIES[source].items()
                                                if label == box.currentText()), "auto")
        values["bili_mode"] = next((key for key, label in BILI_FORMATS.items()
                                    if label == self.bili_format.currentText()), "video")
        values["youtube_mode"] = next((key for key, label in YOUTUBE_FORMATS.items()
                                       if label == self.youtube_format.currentText()), "video")
        values["ui_mode"] = next((key for key, label in UI_MODES.items()
                                  if label == self.ui_mode.currentText()), "qt")
        settings.save(values)
        self.status_message = "设置已保存"

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


def build_pages(qt_app, core):
    """Return an ordered list of (key, title, widget) pages for the shell."""
    pages = []
    for key, title in PAGES:
        if key == "queue":
            widget = QueuePage(core)
        elif key == "local":
            widget = LocalPage(core)
        elif key == "music":
            from qtui.apple import ApplePage
            widget = ApplePage(core)
        elif key == "youtube":
            from qtui.youtube import YoutubePage
            widget = YoutubePage(core)
        elif key == "bilibili":
            from qtui.bilibili import BilibiliPage
            widget = BilibiliPage(core)
        elif key in ("qq", "netease"):
            from qtui.music import MusicPage
            widget = MusicPage(core, key)
        elif key == "settings":
            widget = SettingsPage(qt_app, settings.load().get("dark_mode", False))
        else:
            widget = PlaceholderPage(title, "此页面正在迁移到新界面，当前版本仍可使用原界面完成该平台操作。")
        pages.append((key, title, widget))
    return pages
