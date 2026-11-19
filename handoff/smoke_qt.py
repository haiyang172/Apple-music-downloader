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

    # Apple Music page search + enqueue via fixture.
    from music_catalog import CatalogClient
    apple_fixture = [{"id": "1", "type": "songs", "title": "测试歌曲", "artist": "测试歌手",
                      "album": "测试专辑", "duration": 90, "url": "https://music.apple.com/au/song/1"}]
    with patch.object(CatalogClient, "search", return_value=apple_fixture):
        window.show_page("music")
        page = window.pages["music"]
        page.entry.setText("测试歌曲")
        page.resolve()
        deadline = time.monotonic() + 15
        while page.busy and time.monotonic() < deadline:
            page.poll()
            time.sleep(0.02)
        assert len(page.rows) == 1 and page.rows[0]["title"] == "测试歌曲"
        page.table.selectRow(0)
        page.enqueue(False)
    apple_task = [t for t in window.core.list_tasks() if t.url == "https://music.apple.com/au/song/1"]
    assert len(apple_task) == 1 and apple_task[0].source == "apple_music" and apple_task[0].state == "pending"

    # Qt account dialog: NetEase QR login success saves the session.
    from types import SimpleNamespace
    from netease_auth import NeteaseAuth, NeteaseSession, NeteaseAccount, NeteasePoll
    from qtui.account import MusicAccountDialog
    acct_saved = {}
    acct_qr = SimpleNamespace(url="https://music.163.com/login?codekey=fixture")
    acct_session = NeteaseSession(12345, (("MUSIC_U", "fixture-cookie"),))
    with patch.object(NeteaseAuth, "load", return_value=None), \
         patch.object(NeteaseAuth, "begin", return_value=acct_qr), \
         patch.object(NeteaseAuth, "poll", return_value=NeteasePoll("success", acct_session)), \
         patch.object(NeteaseAuth, "save", side_effect=lambda s: acct_saved.update(session=s)), \
         patch.object(NeteaseAuth, "check", return_value=NeteaseAccount(12345, "测试用户")):
        acct_dialog = MusicAccountDialog(window, "netease")
        assert acct_dialog.polling
        acct_dialog.poll()
        assert acct_dialog.session == acct_session and acct_saved.get("session") == acct_session
        acct_dialog.close()

    # Bilibili account dialog (QR login) saves the session.
    from bili_auth import BiliAuth, BiliSession, BiliAccount, LoginPoll
    bili_saved = {}
    bili_qr = SimpleNamespace(url="https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1")
    bili_session = BiliSession((("SESSDATA", "fixture"),))
    with patch.object(BiliAuth, "load", return_value=None), \
         patch.object(BiliAuth, "begin", return_value=bili_qr), \
         patch.object(BiliAuth, "poll", return_value=LoginPoll("success", bili_session)), \
         patch.object(BiliAuth, "save", side_effect=lambda s: bili_saved.update(session=s)), \
         patch.object(BiliAuth, "check", return_value=BiliAccount(12345, "测试UP")):
        bili_dialog = MusicAccountDialog(window, "bilibili")
        assert bili_dialog.polling
        bili_dialog.poll()
        assert bili_dialog.session == bili_session and bili_saved.get("session") == bili_session
        bili_dialog.close()

    # Qt music preview dialog: probe declaration shown and enqueue works.
    import qq_download
    from qtui.preview import MusicPreviewDialog
    preview_row = {"url": "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qyYhW",
                   "title": "预览歌曲", "artist": "预览歌手", "duration": 90}
    declaration = {"source": "qq", "requested": "auto", "selected": None,
                   "label": "平台提供 · MP3 · 320 kb/s", "codec": "MP3", "bitrate": 320000,
                   "size": 1000000, "duration": 90}
    with patch.object(qq_download, "probe_qq", return_value=declaration):
        pv_dialog = MusicPreviewDialog(window, window.core, "qq", preview_row)
        deadline = time.monotonic() + 15
        while pv_dialog.info is None and time.monotonic() < deadline:
            pv_dialog.poll()
            time.sleep(0.02)
        assert pv_dialog.info == declaration
        assert "MP3" in pv_dialog.source_info.text()
        pv_dialog.enqueue()
    pv_task = [t for t in window.core.list_tasks() if t.url == "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qyYhW"]
    assert len(pv_task) == 1 and pv_task[0].source == "qq" and pv_task[0].state == "pending"
    pv_dialog.close()

    # QQ personal playlists: list, open and enqueue a private track.
    from qq_auth import QqAuth, QqSession
    qq_session = QqSession("fixture1234", "fixture-key", "Fixture")
    playlist = {"id": "42", "title": "QQ 测试歌单", "kind": "created", "total": 1,
                "url": "", "private": True, "_account_id": "fixture1234"}
    tracks_result = {"source": "qq", "title": playlist["title"], "total": 1, "rows": [
        {"position": 1, "title": "私密歌曲", "artist": "歌手", "duration": 90,
         "url": "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qyYhZ"}]}
    with patch.object(QqAuth, "load", return_value=qq_session), \
         patch.object(qq_catalog, "list_playlists", return_value={"source": "qq", "rows": [playlist], "total": 1}), \
         patch.object(qq_catalog, "resolve_playlist", return_value=tracks_result):
        window.show_page("qq")
        page = window.pages["qq"]
        page.show_playlists()
        deadline = time.monotonic() + 15
        while page.busy and time.monotonic() < deadline:
            page.poll()
            time.sleep(0.02)
        assert page.playlist_view and len(page.rows) == 1
        page.table.selectRow(0)
        page.open_playlist()
        deadline = time.monotonic() + 15
        while page.busy and time.monotonic() < deadline:
            page.poll()
            time.sleep(0.02)
        assert not page.playlist_view and len(page.rows) == 1 and page.rows[0]["title"] == "私密歌曲"
        page.table.selectRow(0)
        page.enqueue(False)
    qq_priv_task = [t for t in window.core.list_tasks() if t.url == "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qyYhZ"]
    assert len(qq_priv_task) == 1 and qq_priv_task[0].source == "qq" and qq_priv_task[0].state == "pending"

    # Bilibili personal favorites: list, open a folder and enqueue a video.
    import bili_collections
    from bili_auth import BiliAuth, BiliSession, BiliAccount
    bili_session2 = BiliSession((("SESSDATA", "fixture"), ("DedeUserID", "12345")))
    bili_account = BiliAccount(12345, "测试UP")
    folder = {"id": "favorite:4242", "collection_id": "4242", "kind": "favorite", "owner_id": "12345",
              "title": "驾驶音乐", "total": 1, "url": "https://space.bilibili.com/12345/favlist?fid=4242",
              "_account_id": "12345"}
    favorites_result = {"source": "bilibili", "view": "favorites", "title": "我的收藏夹", "_account_id": "12345",
                        "rows": [folder], "total": 1, "complete": True, "warning": ""}
    folder_result = {"source": "bilibili", "title": "驾驶音乐", "total": 1, "rows": [
        {"source": "bilibili", "url": "https://www.bilibili.com/video/BV1yy411c7mE?p=1", "cid": 102,
         "id": "BV1yy411c7mE", "position": 1, "total": 1, "title": "收藏视频", "audio_title": "收藏视频",
         "artist": "收藏UP", "uploader": "收藏UP", "source_category": "音乐", "media_category": "music",
         "duration": 90, "batch_key": "bilibili:BV1yy411c7mE", "batch_title": "驾驶音乐"}]}
    with patch.object(BiliAuth, "load", return_value=bili_session2), \
         patch.object(BiliAuth, "check", return_value=bili_account), \
         patch.object(bili_collections, "list_my_favorites", return_value=favorites_result), \
         patch.object(bili_collections, "resolve_my_favorite", return_value=folder_result):
        window.show_page("bilibili")
        bpage = window.pages["bilibili"]
        bpage.show_favorites()
        deadline = time.monotonic() + 15
        while bpage.busy and time.monotonic() < deadline:
            bpage.poll()
            time.sleep(0.02)
        assert bpage.favorites_view and len(bpage.rows) == 1
        bpage.table.selectRow(0)
        bpage.open_folder()
        deadline = time.monotonic() + 15
        while bpage.busy and time.monotonic() < deadline:
            bpage.poll()
            time.sleep(0.02)
        assert not bpage.favorites_view and len(bpage.rows) == 1 and bpage.rows[0]["audio_title"] == "收藏视频"
        bpage.table.selectRow(0)
        bpage.enqueue(False)
    fav_task = [t for t in window.core.list_tasks() if t.url == "https://www.bilibili.com/video/BV1yy411c7mE?p=1"]
    assert len(fav_task) == 1 and fav_task[0].source == "bilibili" and fav_task[0].state == "pending"

    # Apple account dialog: login form drives the shared LoginService.
    from login_service import LoginService
    from qtui.apple_account import AppleAccountDialog
    with patch.object(LoginService, "status", return_value={"running": True, "logged_in": False}), \
         patch.object(LoginService, "login", return_value={"logged_in": True, "needs_code": False}):
        ap_dialog = AppleAccountDialog(window, window.core)
        ap_dialog.username.setText("test@example.com")
        ap_dialog.password.setText("secret")
        ap_dialog.login()
        deadline = time.monotonic() + 15
        while ap_dialog.busy and time.monotonic() < deadline:
            ap_dialog.poll()
            time.sleep(0.02)
        assert "已登录" in ap_dialog.status.text()
        ap_dialog.close()

    # Apple library: connected token lists personal songs and queues one.
    from music_library import LibraryClient
    from qtui.library import AppleLibraryDialog
    window.core.preferences["session"] = settings.protect_session("x" * 60)
    library_fixture = [{"id": "1", "type": "songs", "title": "资料库歌曲", "artist": "资料库歌手",
                        "album": "资料库专辑", "duration": 90,
                        "url": "https://music.apple.com/au/song/1"}]
    with patch.object(LibraryClient, "items", return_value=(library_fixture, None)):
        lib_dialog = AppleLibraryDialog(window, window.core)
        lib_dialog.load_songs()
        deadline = time.monotonic() + 15
        while lib_dialog.busy and time.monotonic() < deadline:
            lib_dialog.poll()
            time.sleep(0.02)
        assert len(lib_dialog.rows) == 1 and lib_dialog.rows[0]["title"] == "资料库歌曲"
        lib_dialog.table.selectRow(0)
        lib_dialog.enqueue()
    lib_task = [t for t in window.core.list_tasks() if t.url == "https://music.apple.com/au/song/1"]
    assert len(lib_task) == 1 and lib_task[0].source == "apple_music"
    lib_dialog.close()

    # Settings page saves the download configuration.
    import audio
    window.show_page("settings")
    settings_page = window.pages["settings"]
    settings_page.folder.setText(scratch)
    settings_page.category.setText("测试分类")
    settings_page.output.setCurrentText(audio.OUTPUTS["mp3"])
    settings_page.mp3_bitrate.setCurrentText("192")
    settings_page.save()
    saved = settings.load()
    assert (saved["folder"], saved["category"], saved["output"], saved["mp3_bitrate"]) == (
        scratch, "测试分类", "mp3", 192)
    assert saved["ui_mode"] == "qt"

    # Launch dispatch defaults to Qt and can switch to Tkinter.
    import Studio
    assert Studio._launch_ui() == "qt"
    settings.save({**settings.load(), "ui_mode": "tkinter"})
    assert Studio._launch_ui() == "tkinter"
    settings.save({**settings.load(), "ui_mode": "qt"})
    assert Studio._launch_ui() == "qt"

    print("Qt shell smoke OK: nav, queue, theme, local conversion, YouTube, QQ, Bilibili, Apple, account, preview, playlists, favorites, Apple login, library and settings passed.")
    window.core.close()
    window.close()
