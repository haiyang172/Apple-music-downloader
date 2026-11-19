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

    # YouTube page resolve + enqueue via fixture.
    window.core.preferences["folder"] = scratch
    window.core.preferences["category"] = ""
    import qtui.youtube
    from unittest.mock import patch
    fixture = {"source": "youtube", "title": "测试频道", "rows": [
        {"source": "youtube", "id": "abcdefghijk", "url": "https://www.youtube.com/watch?v=abcdefghijk",
         "title": "测试视频", "artist": "测试频道", "position": 1, "total": 1, "duration": 30,
         "batch_key": "youtube:abcdefghijk", "batch_title": "测试频道"}]}
    with patch.object(qtui.youtube, "resolve_youtube", return_value=fixture):
        window.show_page("youtube")
        page = window.pages["youtube"]
        page.entry.setText("https://www.youtube.com/watch?v=abcdefghijk")
        page.resolve()
        deadline = time.monotonic() + 15
        while page.busy and time.monotonic() < deadline:
            page.poll()
            time.sleep(0.02)
        assert len(page.rows) == 1 and page.caption.text() == "测试频道"
        page.table.selectRow(0)
        page.enqueue(False)
    yt_task = [t for t in window.core.list_tasks() if t.url == "https://www.youtube.com/watch?v=abcdefghijk"]
    assert len(yt_task) == 1 and yt_task[0].source == "youtube" and yt_task[0].state == "pending"

    # QQ music page search + enqueue via fixture.
    import qq_catalog
    song_fixture = {"source": "qq", "title": "测试歌手", "total": 1, "rows": [
        {"position": 1, "title": "测试歌曲", "artist": "测试歌手", "album": "测试专辑",
         "duration": 90, "url": "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qxYhV"}]}
    with patch.object(qq_catalog, "search_songs", return_value=song_fixture):
        window.show_page("qq")
        page = window.pages["qq"]
        page.entry.setText("测试歌曲")
        page.resolve()
        deadline = time.monotonic() + 15
        while page.busy and time.monotonic() < deadline:
            page.poll()
            time.sleep(0.02)
        assert len(page.rows) == 1 and page.rows[0]["title"] == "测试歌曲"
        page.table.selectRow(0)
        page.enqueue(False)
    qq_task = [t for t in window.core.list_tasks() if t.url == "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qxYhV"]
    assert len(qq_task) == 1 and qq_task[0].source == "qq" and qq_task[0].state == "pending"

    # Bilibili page resolve + enqueue via fixture.
    import bili_catalog
    bili_fixture = {"id": "BV1xx411c7mD", "title": "测试视频", "uploader": "测试UP",
                    "source_category": "音乐", "media_category": "music",
                    "rows": [{"source": "bilibili", "url": "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                              "cid": 101, "id": "BV1xx411c7mD", "position": 1, "total": 1,
                              "title": "测试歌曲", "audio_title": "测试歌曲", "artist": "测试歌手",
                              "uploader": "测试UP", "source_category": "音乐", "media_category": "music",
                              "duration": 90, "batch_key": "bilibili:BV1xx411c7mD", "batch_title": "测试视频"}]}
    with patch.object(bili_catalog, "resolve_video", return_value=bili_fixture):
        window.show_page("bilibili")
        page = window.pages["bilibili"]
        page.entry.setText("https://www.bilibili.com/video/BV1xx411c7mD")
        page.resolve()
        deadline = time.monotonic() + 15
        while page.busy and time.monotonic() < deadline:
            page.poll()
            time.sleep(0.02)
        assert len(page.rows) == 1 and page.rows[0]["audio_title"] == "测试歌曲"
        page.table.selectRow(0)
        page.enqueue(False)
    bili_task = [t for t in window.core.list_tasks() if t.url == "https://www.bilibili.com/video/BV1xx411c7mD?p=1"]
    assert len(bili_task) == 1 and bili_task[0].source == "bilibili" and bili_task[0].state == "pending"

    print("Qt shell smoke OK: nav, queue page, theme toggle, local conversion, YouTube, QQ music and Bilibili pages passed.")
    window.core.close()
    window.close()
