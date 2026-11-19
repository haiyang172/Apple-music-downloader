import os, sys, tempfile, time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
with tempfile.TemporaryDirectory(prefix="amdl-qt-smoke-") as scratch:
    for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        os.environ[key] = scratch
    sys.path.insert(0, r"D:\AMDL")
    sys.path.insert(0, r"D:\AMDL\lib")
    import StudioQt
    import settings
    settings.DEFAULTS["auto_service"] = False

    window = StudioQt.build_window(dark=False)
    assert window.windowTitle() == "影音下载台"
    assert list(window.buttons) == ["music", "bilibili", "qq", "netease", "youtube", "douyin",
                                    "queue", "local", "settings"]
    window.show_page("queue")
    window.show_page("settings")
    window.pages["settings"].toggle_theme()
    assert window.pages["settings"].dark is True
    window.pages["settings"].toggle_theme()
    assert window.pages["settings"].dark is False
    for key in ("music", "bilibili", "qq", "netease", "youtube", "douyin", "local"):
        window.show_page(key)
        assert window.stack.currentWidget() is window.pages[key]

    # Functional local conversion through the Qt controller.
    backend = Path(r"D:\AMDL\backend")
    ffmpeg = backend / "tools" / "ffmpeg.exe"
    source = Path(scratch) / "source.m4a"
    import subprocess
    subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.1", "-c:a", "aac", str(source)],
                   check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    out = Path(scratch) / "out"
    out.mkdir()
    ids = window.core.queue_local([str(source)], "mp3", str(out))
    assert len(ids) == 1
    window.core.start(ids)
    deadline = time.monotonic() + 90
    while window.core.busy and time.monotonic() < deadline:
        window.pages["queue"].poll()
        time.sleep(0.05)
    task = window.core.queue_store.get(ids[0])
    assert task.state == "complete", task.error
    assert task.files and Path(task.files[0]).suffix == ".mp3" and Path(task.files[0]).is_file()
    print("Qt shell smoke OK: nav, queue page, theme toggle, local conversion passed.")
    window.core.close()
    window.close()
