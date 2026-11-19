import os, sys, tempfile, time, subprocess
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
with tempfile.TemporaryDirectory(prefix="amdl-qttag-") as scratch:
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        os.environ[key] = scratch
    sys.path.insert(0, r"D:\AMDL")
    sys.path.insert(0, r"D:\AMDL\lib")
    import settings
    settings.DEFAULTS["auto_service"] = False
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from qtui.tag_editor import TagEditorDialog

    ffmpeg = Path(r"D:\AMDL\backend\tools\ffmpeg.exe")
    music_dir = Path(scratch) / "音乐"
    music_dir.mkdir()
    path = music_dir / "样例.m4a"
    subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.1", "-c:a", "aac", str(path)],
                   check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    import music_tagging
    music_tagging.write_music_tags(path, "原歌名", "原歌手")
    original = path.read_bytes()

    dialog = TagEditorDialog(None, [str(path)])
    deadline = time.monotonic() + 15
    while dialog.scanning and time.monotonic() < deadline:
        dialog.poll()
        time.sleep(0.02)
    assert not dialog.scanning
    assert dialog.files[0]["title"] == "原歌名", dialog.files[0]
    # apply artist change via checkbox + entry
    dialog.checks["artist"].setChecked(True)
    dialog.entries["artist"].setText("界面歌手")
    dialog._refresh_controls()
    print("checked:", dialog.checks["artist"].isChecked(), "text:", repr(dialog.entries["artist"].text()))
    dialog.apply()
    deadline = time.monotonic() + 30
    while (dialog.busy or dialog.messages) and time.monotonic() < deadline:
        dialog.poll()
        time.sleep(0.02)
    print("status:", repr(dialog.status.text()))
    assert not dialog.busy
    after = dialog.store.read_audio(path)
    print("after apply artist repr:", repr(after.get("artist")), "title:", repr(after.get("title")))
    assert after["artist"] == "界面歌手"
    assert dialog.history and any(r["state"] == "applied" for r in dialog.history.values())
    # undo via history
    dialog.history_tree.selectRow(0)
    dialog._refresh_controls()
    dialog.undo()
    deadline = time.monotonic() + 30
    while dialog.busy and time.monotonic() < deadline:
        dialog.poll()
        time.sleep(0.02)
    assert not dialog.busy
    assert path.read_bytes() == original
    print("Qt tag editor OK: read, apply and undo restored exact original bytes.")
    dialog.close()
