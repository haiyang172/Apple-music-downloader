[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Setup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$installer = (Get-Item -LiteralPath $Setup).FullName
$temporaryDirectory = (Resolve-Path -LiteralPath ([IO.Path]::GetTempPath())).Path
$testName = 'amdl-setup-smoke-' + [Guid]::NewGuid().ToString('N')
$testRoot = Join-Path $temporaryDirectory $testName
$target = Join-Path $testRoot 'application'
$profile = Join-Path $testRoot 'profile'
$tempFiles = Join-Path $testRoot 'temp'
foreach ($directory in @($target, $profile, $tempFiles)) {
    [void][IO.Directory]::CreateDirectory($directory)
}

function Get-InstallState {
    $uninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{74870839-527C-4C3D-A869-AB8A60913829}_is1'
    foreach ($root in @('HKCU:', 'HKLM:')) {
        $path = Join-Path $root $uninstallKey
        if (Test-Path -LiteralPath $path) {
            Get-ItemProperty -LiteralPath $path |
                Select-Object DisplayName, DisplayVersion, InstallLocation, UninstallString |
                ConvertTo-Json -Compress
        } else {
            "$path absent"
        }
    }
    foreach ($directory in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
        foreach ($name in @('影音下载台.lnk', 'amdl.lnk', 'AMDL Studio.lnk')) {
            $path = Join-Path $directory $name
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
            } else {
                "$path absent"
            }
        }
    }
}

$environment = @{
    USERPROFILE = $profile
    LOCALAPPDATA = $profile
    APPDATA = $profile
    TEMP = $tempFiles
    TMP = $tempFiles
    PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    PYTHONHOME = $null
    PYTHONPATH = $null
    TCL_LIBRARY = $null
    TK_LIBRARY = $null
    SSL_CERT_FILE = $null
    SSL_CERT_DIR = $null
}
$savedEnvironment = @{}

try {
    $before = @(Get-InstallState)
    $arguments = @('/EXTRACTONLY=1', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOCLOSEAPPLICATIONS',
        '/NORESTARTAPPLICATIONS', '/LANG=english', ('/DIR="' + $target + '"'))
    $setupProcess = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($setupProcess.ExitCode -ne 0) {
        throw "Setup extraction failed with exit code $($setupProcess.ExitCode)."
    }
    if (Compare-Object $before @(Get-InstallState)) {
        throw 'Setup extraction unexpectedly changed an existing registration or shortcut.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $target 'amdl.exe') -PathType Leaf) -or
        (Get-ChildItem -LiteralPath $target -Filter 'unins*')) {
        throw 'Setup did not produce a standalone, unregistered application directory.'
    }
    $launcher = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $target 'amdl.exe'))
    if ($launcher.ProductName -cne '影音下载台') { throw 'Setup did not contain the branded launcher.' }

    foreach ($name in $environment.Keys) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
    }

    $smokeCode = @'
from pathlib import Path
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
from unittest.mock import patch

root = Path(sys.argv[1])
profile = Path(os.environ["LOCALAPPDATA"])
sys.path.insert(0, str(root))

import Studio
import login_service
import mutagen
import settings
import yaml

assert ssl.create_default_context().cert_store_stats()["x509"] > 0
assert settings.DATA.is_relative_to(profile) and not settings.FILE.exists()
backend = Studio.find_backend()
assert backend == root / "backend"

audio = profile / "local-test.m4a"
subprocess.run(
    [str(backend / "tools/ffmpeg.exe"), "-v", "error", "-f", "lavfi",
     "-i", "sine=frequency=440:duration=0.1", "-c:a", "aac", str(audio)],
    check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
)
assert mutagen.File(audio).info.length > 0
for suffix, codec in (("flac", "flac"), ("mp3", "libmp3lame")):
    converted = profile / f"local-test.{suffix}"
    subprocess.run(
        [str(backend / "tools/ffmpeg.exe"), "-v", "error", "-i", str(audio), "-c:a", codec, str(converted)],
        check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert mutagen.File(converted).info.length > 0
print("Bundled Python/Tk, TLS, backend, and AAC/FLAC/MP3 encoding passed.")

def unexpected_download(*args):
    raise AssertionError("Login service attempted to download a missing component.")

login_service._download = unexpected_download
service = login_service.LoginService(root, profile / "service-test")
try:
    state = service.start(threading.Event(), lambda _: None)
    assert state["running"] and not state["logged_in"]
finally:
    service.close()
print("Bundled login service started without fetching a component or accessing an account.")

class TestCatalog:
    def tracks(self, item, region):
        assert (item["type"], item["id"], region) == ("playlist", "pl.test", "au")
        return [{"url": f"https://music.apple.com/au/song/{index}", "title": f"Track {index}"}
                for index in (1, 2, 3)]


class TestLogin:
    def start(self, stop, post):
        pass

    def status(self):
        return {"logged_in": True}

    def engine_config(self):
        return {}

    def close(self):
        pass


class TestEngine:
    ffmpeg = backend / "tools/ffmpeg.exe"

    def __init__(self, pause_second=False):
        self.entered_second = threading.Event()
        self.pause_second = pause_second
        self.calls = []

    def download(self, config, url, quality, stage, stop, on_event):
        from engine import Cancelled

        index = int(url.rsplit("/", 1)[-1])
        self.calls.append(index)
        if index == 2 and self.pause_second:
            self.entered_second.set()
            assert stop.wait(10)
            raise Cancelled()
        source = stage / "test.m4a"
        shutil.copyfile(audio, source)
        return {"errors": 0, "artifacts": [{"path": str(source), "duration": 0.1,
                "title": f"Track {index}", "track_id": str(index)}]}

    def close_service(self):
        pass


def new_window():
    window = tk.Tk()
    window.withdraw()
    return window, Studio.Studio(window)


def wait_for(window, condition):
    deadline = time.monotonic() + 15
    while not condition() and time.monotonic() < deadline:
        window.update()
        time.sleep(0.02)
    assert condition(), "The application did not reach the expected state"


Studio.configure_windows()
window, app = new_window()
try:
    assert window.title() == Studio.APP_TITLE
    assert app.profile_text.get() == "登录" and app.brand_icon.width() >= 32
    assert app.profile_button.master == app.rail and app.navigation["components"].master == app.nav_content
    assert int(app.profile_button.grid_info()["row"]) > int(app.nav_canvas.master.grid_info()["row"])
    app.profile_button.invoke()
    assert app.active_page == "account"
    app.show("music")
    browser = app.music_browser
    deadline = time.monotonic() + 30
    while browser.busy and time.monotonic() < deadline:
        window.update()
        time.sleep(0.03)
    assert browser.rows, "Apple Music catalogue failed: " + browser.status.get()
    browser.query.set("Beyond")
    browser.search()
    deadline = time.monotonic() + 30
    while browser.busy and time.monotonic() < deadline:
        window.update()
        time.sleep(0.03)
    assert any("Beyond" in row["artist"] for row in browser.rows.values()), browser.status.get()
finally:
    app.close()
print("Isolated installed window loaded Apple Music catalogue and live search.")


class PagedCatalog:
    def __init__(self):
        self.calls = []
        self.second_started = threading.Event()
        self.release_second = threading.Event()
        self.fail_last = True

    def search_page(self, term, region, kind, cursor):
        self.calls.append((term, cursor))
        if term == "new":
            return self.rows(100, 101), None
        if cursor == "second":
            self.second_started.set()
            assert self.release_second.wait(10)
            return self.rows(30, 60), "last"
        if cursor == "last":
            if self.fail_last:
                self.fail_last = False
                raise RuntimeError("Fixture network error")
            return self.rows(60, 62), None
        return self.rows(0, 30), "second"

    @staticmethod
    def rows(start, end):
        return [{"type": "song", "title": f"Track {index}", "artist": "Fixture",
                 "url": f"https://music.apple.com/au/song/{index}", "artwork_url": f"art-{index}"}
                for index in range(start, end)]


import music_browser
art_requests = []
original_thumbnail = music_browser._thumbnail


def count_art(url, pixels):
    art_requests.append((url, pixels))
    return b""


music_browser._thumbnail = count_art
window, app = new_window()
try:
    window.deiconify()
    browser = app.music_browser
    catalogue = PagedCatalog()
    browser.catalog = catalogue
    browser.query.set("paged")
    browser.search()
    wait_for(window, lambda: len(browser.rows) == 30 and art_requests and not browser.art_busy)
    assert catalogue.calls == [("paged", None)]
    assert len(art_requests) < 30 and all(url != "art-29" for url, _ in art_requests)
    assert all(pixels == round(52 * app.scale) for _, pixels in art_requests)
    assert not browser.retry_page.winfo_manager()

    browser.sort.choose("#0")
    selected = browser.tree.get_children()[10]
    browser.tree.selection_set(selected)
    browser.tree.yview_moveto(1)
    wait_for(window, catalogue.second_started.is_set)
    assert browser.queue_button.instate(("!disabled",))
    first, _ = browser.tree.yview()
    anchor = browser.tree.get_children()[round(first * len(browser.rows))]
    catalogue.release_second.set()
    wait_for(window, lambda: len(browser.rows) == 60)
    assert browser.tree.selection() == (selected,)
    first, _ = browser.tree.yview()
    assert browser.tree.get_children()[round(first * len(browser.rows))] == anchor
    assert catalogue.calls == [("paged", None), ("paged", "second")]

    browser.tree.yview_moveto(1)
    wait_for(window, lambda: browser.page_error)
    assert len(browser.rows) == 60 and browser.retry_page.winfo_manager()
    browser.retry_page.invoke()
    wait_for(window, lambda: len(browser.rows) == 62)
    assert browser.cursor is None and not browser.retry_page.winfo_manager()
    assert catalogue.calls[-2:] == [("paged", "last"), ("paged", "last")]

    browser.tree.yview_moveto(0.3)
    window.update()
    first = browser.tree.yview()[0]
    event = tk.Event()
    event.widget = window
    scales = ((192, 104, 128), (96, 52, 64), (144, 78, 96)) if app.sidebar_scale == 1 else (
        (96, 52, 64), (192, 104, 128), (144, 78, 96))
    for dpi, pixels, height in scales:
        request_count = len(art_requests)
        old_serial = browser.art_serial
        with patch.object(Studio.ctypes.windll.user32, "GetDpiForWindow", return_value=dpi):
            app.refresh_sidebar_art(event)
            wait_for(window, lambda: not browser.art_busy and any(
                actual == pixels for _, actual in art_requests[request_count:]))
        assert browser.cover_size == app.library_browser.cover_size == pixels
        assert int(Studio.ttk.Style(window).lookup("Music.Treeview", "rowheight")) == height
        assert browser.tree.selection() == (selected,)
        assert abs(browser.tree.yview()[0] - first) <= 1 / len(browser.rows)
        assert browser.art_serial > old_serial
        browser.receive_art(old_serial, "old-size-fixture", None)
        assert "old-size-fixture" not in browser.thumbnails

    catalogue.release_second.clear()
    catalogue.second_started.clear()
    browser.search()
    wait_for(window, lambda: len(browser.rows) == 30)
    browser.tree.yview_moveto(1)
    wait_for(window, catalogue.second_started.is_set)
    browser.query.set("new")
    browser.search()
    wait_for(window, lambda: any(row["title"] == "Track 100" for row in browser.rows.values()))
    catalogue.release_second.set()
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        window.update()
        time.sleep(0.02)
    assert len(browser.rows) == 1
finally:
    music_browser._thumbnail = original_thumbnail
    app.close()
print("Scrolling, visible artwork, and selection retention across simulated 100/150/200 percent DPI passed.")


class PagedLibrary:
    def __init__(self, *args, **kwargs):
        pass

    def items(self, kind, cursor=None, *, limit=100):
        assert limit == 100
        library_calls.append((kind, cursor))
        if library_phase == "cold" and cursor == "page-2" and library_calls.count((kind, cursor)) == 1:
            raise RuntimeError("Fixture network error")
        if library_phase != "cold":
            assert release_library_sync.wait(10)
            if library_phase == "offline":
                raise RuntimeError("Fixture offline")
            return self.rows(1, 71), None
        if cursor == "page-2":
            return self.rows(0, 20), "page-3"
        if cursor == "page-3":
            return self.rows(20, 30), None
        return self.rows(30, 70), "page-2"

    @staticmethod
    def rows(start, end):
        return [dict(row, duration=index + 60, artwork_url="")
                for index, row in enumerate(PagedCatalog.rows(start, end), start)]


original_library = music_browser.LibraryClient
library_calls = []
library_phase = "cold"
release_library_sync = threading.Event()
music_browser.LibraryClient = PagedLibrary
window, app = new_window()
try:
    window.deiconify()
    window.geometry(f"{window.minsize()[0]}x{window.minsize()[1]}")
    app.library_token = lambda: "test-session"
    app.show("library")
    browser = app.library_browser
    wait_for(window, lambda: len(browser.rows) == 40 and browser.page_error)
    assert library_calls == [("song", None), ("song", "page-2")]
    row = browser.tree.get_children()[0]
    browser.tree.selection_set(row)
    x, y, width, height = browser.tree.bbox(row, "duration")
    assert width >= browser.row_font.measure("59:59") and x + width <= browser.tree.winfo_width()
    browser.sort.choose("#0")
    assert "同步失败" in browser.status.get()
    assert app.library_cache.load(browser.library_scope) is None
    browser.retry_page.invoke()
    wait_for(window, lambda: len(browser.rows) == 70 and not browser.busy)
    assert library_calls == [("song", None), ("song", "page-2"), ("song", "page-2"), ("song", "page-3")]
    order = browser.tree.get_children()
    assert [browser.rows[item]["title"] for item in order] == [f"Track {index}" for index in range(70)]
    assert browser.tree.selection() == (row,)
    browser.sort.choose("#0")
    assert browser.rows[browser.tree.get_children()[0]]["title"] == "Track 69"
    assert len(library_calls) == 4
    snapshot = app.library_cache.load(browser.library_scope)
    assert len(snapshot) == 70 and snapshot[0]["title"] == "Track 30"
finally:
    app.close()

library_phase = "offline"
window, app = new_window()
try:
    window.deiconify()
    app.library_token = lambda: "test-session"
    app.show("library")
    browser = app.library_browser
    wait_for(window, lambda: len(browser.rows) == 70 and browser.appending)
    assert browser.busy and len(library_calls) == 5
    browser.sort.choose("#0")
    assert browser.rows[browser.tree.get_children()[0]]["title"] == "Track 0"
    keep = next(row for row, item in browser.rows.items() if item["title"] == "Track 40")
    browser.tree.selection_set(keep)
    release_library_sync.set()
    wait_for(window, lambda: browser.page_error)
    assert len(browser.rows) == 70 and app.library_cache.load(browser.library_scope) == snapshot
    library_phase = "changed"
    browser.retry_page.invoke()
    wait_for(window, lambda: not browser.busy)
    assert browser.rows[browser.tree.get_children()[0]]["title"] == "Track 1"
    assert browser.rows[browser.tree.get_children()[-1]]["title"] == "Track 70"
    assert browser.tree.selection() == (keep,)
    assert app.library_cache.load(browser.library_scope)[0]["title"] == "Track 1"
finally:
    release_library_sync.set()
    music_browser.LibraryClient = original_library
    app.close()
print("Library saved complete pages, reused cache before network, and kept selection while adding and removing tracks.")

window, app = new_window()
try:
    app.catalog = TestCatalog()
    assert app.queue_music([{"url": "https://music.apple.com/au/song/1", "title": "Track 1",
                            "batch_key": "catalog:au:playlist:pl.test", "batch_title": "example",
                            "position": 1}]) == 1
    assert app.add_link_text("https://music.apple.com/au/playlist/example/pl.test")
    wait_for(window, lambda: not app.resolving_links)
    assert [task.position for task in app.tasks.values()] == [1, 2, 3]
    assert all(task.total == 3 for task in app.tasks.values())
    app.login_service = TestLogin()
    app.engine = TestEngine(pause_second=True)
    app.fields["folder"].set(str(profile))
    app.fields["category"].set("queue-test")
    app.fields["native_account"].set(True)
    app.fields["auto_service"].set(False)
    app.start_download()
    wait_for(window, app.engine.entered_second.is_set)
    app.pause()
    wait_for(window, lambda: not app.busy)
    assert [task.state for task in app.tasks.values()] == ["complete", "interrupted", "pending"]
    assert app.engine.calls == [1, 2]
    first = app.queue_store.list_tasks()[0]
    assert Path(first.files[0]).parent == profile / "queue-test"
    details, = first.audio_details
    assert details["source"]["codec"] == details["output"]["codec"] == "AAC-LC"
    assert details["source"]["sample_rate"] == 44100 and details["source"]["bitrate"] > 0
    assert details["source"]["bits_per_sample"] is None
    with patch.object(Studio.messagebox, "showinfo") as displayed:
        app.show_task_details(str(first.id))
        displayed.assert_called_once()
        assert "AAC-LC" in displayed.call_args.args[1] and "kb/s" in displayed.call_args.args[1]
    queue_store = app.queue_store
finally:
    app.close()

queue_store.set_state(queue_store.list_tasks()[1].id, "running", files=first.files)
window, app = new_window()
try:
    assert [task.state for task in app.tasks.values()] == ["complete", "interrupted", "pending"]
    assert app.queue_store.get(first.id).audio_details == first.audio_details
    app.login_service = TestLogin()
    app.engine = TestEngine()
    app.start_download()
    wait_for(window, lambda: not app.busy)
    assert app.engine.calls == [2, 3]
    assert all(task.state == "complete" and Path(task.files[0]).is_file()
               for task in app.queue_store.list_tasks())
    alternate = profile / "new-output"
    alternate.mkdir()
    app.fields["folder"].set(str(alternate))
    app.queue_tree.selection_set(str(first.id))
    app.requeue_selected()
    repeat = app.queue_store.list_tasks()[-1]
    assert repeat.id != first.id and repeat.state == "pending"
    assert repeat.audio_details == ()
    assert app.queue_store.get(first.id).state == "complete"
    app.start_download()
    wait_for(window, lambda: not app.busy)
    repeat = app.queue_store.get(repeat.id)
    assert repeat.state == "complete" and Path(repeat.files[0]).parent == alternate / "queue-test"
finally:
    app.close()
print("Playlist positions, restart recovery, completed-track skipping, and new output settings passed.")

import apple_download
from bili_auth import BiliAccount, BiliSession, QRLogin
import bili_catalog
import bili_collections
import bili_download
import bili_resume
import base64
import json
from dataclasses import replace
from io import BytesIO

window = tk.Tk()
window.withdraw()
try:
    window.update_idletasks()
    pixels = 104
    bands = b"\0\0\xff" * 20 + b"\xff\0\0" * 20 + b"\0\xff\0" * 20 + b"\xff\xff\xff" * 20
    source = b"P6\n80 40\n255\n" + bands * 40
    for codec in ("png", "mjpeg"):
        encoded = subprocess.run(
            [str(backend / "tools/ffmpeg.exe"), "-v", "error", "-f", "ppm_pipe", "-i", "pipe:0",
             "-frames:v", "1", "-c:v", codec, "-f", "image2pipe", "pipe:1"],
            input=source, capture_output=True, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        with patch.object(music_browser, "_apple_data", side_effect=[OSError("Requested rendition unavailable"), encoded]):
            data = music_browser._thumbnail("https://fixture.mzstatic.com/art/48x48bb.jpg", pixels)
        assert base64.b64decode(data).startswith(b"\x89PNG\r\n\x1a\n")
        image = tk.PhotoImage(master=window, data=data)
        assert (image.width(), image.height()) == (pixels, pixels)
        left, right = image.get(20, 50), image.get(82, 50)
        assert left[0] > 200 and left[1] < 50 and right[1] > 200 and right[0] < 50
finally:
    window.destroy()
print("High-DPI artwork fallback crops and scales mismatched PNG and JPEG without copying low-resolution pixels.")

window, app = new_window()
try:
    collection = {"type": "playlist", "library_id": "p.saved-fixture", "title": "已缓存歌单"}
    track = {"title": "已缓存曲目", "type": "song", "url": "https://music.apple.com/au/song/901"}
    token = "fixture-cache-scope"
    app.library_token = lambda: token
    scope = music_browser._library_cache_scope(token, app.catalog_region(), "playlist", collection)
    app.library_cache.save(scope, [track])
    client = TestCatalog()
    browser = app.library_browser

    def offline(*_args, **_kwargs):
        raise music_browser.CatalogError("暂时无法同步", 503)

    def forbidden(*_args, **_kwargs):
        raise music_browser.CatalogError("歌单已删除", 404)

    with patch.object(music_browser, "LibraryClient", return_value=client), \
         patch.object(client, "tracks", side_effect=offline), \
         patch.object(app, "queue_music", return_value=1) as enqueue:
        browser._resolve_batch([collection], False)
        wait_for(window, lambda: not browser.batch_busy)
        enqueue.assert_called_once()
        assert enqueue.call_args.args[0][0]["url"] == track["url"]
        assert "使用上次同步" in browser.status.get()
    with patch.object(music_browser, "LibraryClient", return_value=client), \
         patch.object(client, "tracks", side_effect=forbidden), \
         patch.object(app, "queue_music") as enqueue:
        browser._resolve_batch([collection], False)
        wait_for(window, lambda: not browser.batch_busy)
        enqueue.assert_not_called()
finally:
    app.close()
print("Apple Music batch queue reuses complete cached playlists on temporary failure only.")

window, app = new_window()
try:
    candidate = "fixture-library-token-" + "a" * 60
    app.connect_library()
    form = app.library_connection_dialog
    form.token.set(candidate)
    with patch.object(app.catalog, "request", return_value={"data": []}) as validated:
        form.submit.invoke()
        wait_for(window, lambda: app.active_page == "library" and app.library_token() == candidate)
        assert validated.call_args_list[0].args[0] == "/v1/me/library/songs"
        assert validated.call_args_list[0].kwargs["user_token"] == candidate
    assert candidate not in settings.FILE.read_text(encoding="utf-8")
    app.clear_account()
finally:
    app.close()
print("Apple Music library authorization validation and encrypted storage passed.")

original_parts = bili_catalog._fetch_parts
original_metadata = bili_catalog._fetch_video_metadata
original_bili_download = apple_download.download_video
original_danmaku = apple_download.save_danmaku
original_subtitle = apple_download.save_subtitle
original_cover = apple_download.save_cover
session = BiliSession((("SESSDATA", "fixture-account-session"),))
parts_calls = []


def fixture_parts(video_id, sess=None):
    parts_calls.append((video_id, sess))
    return [
    {"page": 1, "cid": 101, "part": "Part One", "duration": 35},
    {"page": 2, "cid": 102, "part": "演唱：周杰伦｜【纯享版】《夜曲》", "duration": 60},
    ]


bili_catalog._fetch_parts = fixture_parts
bili_catalog._fetch_video_metadata = lambda _: {"title": "公开演出合集", "uploader": "投稿者测试", "source_category": "音乐"}
bili_calls = []


def fixture_bili_download(url, mode, stage, ffmpeg, stop, on_status, *, session=None, checkpoint=None, check_volume=None):
    assert stage.parent == checkpoint and checkpoint.parent == profile / ".amdl-downloads"
    check_volume()
    bili_calls.append((url, mode, session))
    on_status("Fixture audio")
    return Path(shutil.copyfile(audio, stage / "fixture.m4a"))


def fixture_danmaku(url, stage, stop, on_status):
    target = stage / "fixture.ass"
    target.write_text("[Script Info]\nScriptType: v4.00+\n", encoding="utf-8")
    return target


def fixture_subtitle(url, stage, stop, on_status, *, session=None):
    assert url.endswith("?p=2") and session is not None
    target = stage / "fixture.srt"
    target.write_text("1\n00:00:00,100 --> 00:00:01,000\n测试字幕\n\n", encoding="utf-8")
    return target


def fixture_cover(url, stage, stop, on_status):
    assert url.endswith("?p=2")
    return Path(shutil.copyfile(root / "assets/amdl-32.png", stage / "fixture-cover.png"))


apple_download.download_video = fixture_bili_download
apple_download.save_danmaku = fixture_danmaku
apple_download.save_subtitle = fixture_subtitle
apple_download.save_cover = fixture_cover
window, app = new_window()
try:
    app.fields["native_account"].set(False)
    app.fields["auto_service"].set(False)
    app.fields["folder"].set(str(profile))
    app.fields["category"].set("")
    app.bili_format.set(Studio.BILI_FORMATS["audio"])
    app.fields["bili_danmaku"].set(True)
    app.fields["bili_subtitle"].set(True)
    app.fields["bili_archive"].set(True)
    app.show("bilibili")
    browser = app.bili_browser
    account = browser.account
    fixture_url = "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + "1" * 32
    with patch.object(app.bili_auth, "begin", return_value=QRLogin(fixture_url, "1" * 32)):
        account.open()
        wait_for(window, lambda: hasattr(account, "image"))
        assert account.image.width() >= 100
        account.receive(account.serial, "signed_in", (session, BiliAccount(1, "Fixture account")))
        assert app.bili_auth.load() == session
        assert b"fixture-account-session" not in app.bili_auth.path.read_bytes()
        account.close()
    browser.link.set("【哔哩哔哩】分享作品 https://www.bilibili.com/video/BV1GJ411x7h7）。")
    browser.resolve()
    wait_for(window, lambda: len(browser.rows) == 2 and not browser.busy)
    assert browser.caption.get() == "公开演出合集"
    assert browser.metadata.get() == "投稿者测试  ·  音乐"
    track_row = browser.tree.get_children()[1]
    assert browser.tree.set(track_row, "title") == "夜曲 · 周杰伦"
    app.bili_format.set(Studio.BILI_FORMATS["video"])
    assert "《夜曲》" in browser.tree.set(track_row, "title")
    app.bili_format.set(Studio.BILI_FORMATS["audio"])
    assert bili_catalog.clean_song_title("《三体》解说") == ""
    browser.tree.selection_set(track_row)
    browser.queue_selected()
    task, = [task for task in app.tasks.values() if task.source == "bilibili"]
    assert (task.position, task.total, task.audio_title, task.artist) == (2, 2, "夜曲", "周杰伦")
    assert (task.uploader, task.source_category, task.media_category) == ("投稿者测试", "音乐", "music")
    app.show_download_source("bilibili")
    assert app.active_page == "download" and app.queue_tree.get_children() == (str(task.id),)
    assert not app.quality_box.winfo_manager() and app.bili_format_box.winfo_manager()
    app.show_download_source(Studio.SOURCE)
    assert str(task.id) not in app.queue_tree.get_children() and app.quality_box.winfo_manager()
    app.show_download_source("all")
    assert str(task.id) in app.queue_tree.get_children()
    app.show_download_source("bilibili")
    app.start_download()
    wait_for(window, lambda: not app.busy)
    task = app.queue_store.get(task.id)
    assert task.state == "complete" and Path(task.files[0]).parent == profile / "音乐"
    persona = replace(task, title="第 2 段", batch_title="Persona 5 OST 原声合集")
    assert apple_download.task_category(persona) == "Persona 系列"
    assert apple_download.task_category(replace(persona, batch_key="bilibili:favorite:123")) == "音乐"
    assert apple_download.task_category(replace(persona, options={**persona.options, "category": "自选歌单"})) == "自选歌单"
    assert apple_download.task_category(replace(persona, options={**persona.options, "bili_category_rules": 1})) == "音乐"
    assert bili_catalog.suggest_folder("Phigros 音乐全收集", "游戏", "music") == "音游"
    assert bili_catalog.suggest_folder("粤语经典老歌合集", "音乐", "music") == "粤语"
    assert bili_catalog.suggest_folder("经典老歌回顾", "音乐", "music") == "老歌"
    assert bili_catalog.suggest_folder("Phigros All Perfect 手元", "游戏", "video") == ""
    assert Path(task.files[0]).name == "周杰伦 - 夜曲.m4a"
    tags = mutagen.File(task.files[0]).tags
    assert tags["\xa9nam"] == ["夜曲"] and tags["\xa9ART"] == ["周杰伦"]
    assert Path(task.files[1]) == Path(task.files[0]).with_suffix(".ass")
    assert Path(task.files[2]) == Path(task.files[0]).with_suffix(".srt")
    assert Path(task.files[3]) == Path(task.files[0]).with_suffix(".png")
    assert Path(task.files[3]).read_bytes() == (root / "assets/amdl-32.png").read_bytes()
    metadata_path = Path(task.files[4])
    assert metadata_path == Path(task.files[0]).with_suffix(".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert (metadata["artist"], metadata["song_title"], metadata["part"]) == ("周杰伦", "夜曲", 2)
    assert metadata["url"] == task.url and metadata["media_file"] == Path(task.files[0]).name
    assert "fixture-account-session" not in metadata_path.read_text(encoding="utf-8")
    assert not any((profile / ".amdl-downloads").iterdir())
    assert parts_calls == [("BV1GJ411x7h7", session)]
    assert bili_calls == [("https://www.bilibili.com/video/BV1GJ411x7h7?p=2", "audio", session)]
    playlist = profile / "驾车.m3u8"
    app.queue_tree.selection_set(str(task.id))
    with patch.object(Studio.filedialog, "asksaveasfilename", return_value=str(playlist)):
        app.export_queue_playlist()
    playlist_lines = playlist.read_text(encoding="utf-8").splitlines()
    assert playlist_lines[0] == "#EXTM3U" and playlist_lines[-1] == "音乐/周杰伦 - 夜曲.m4a"
    with patch.object(app.local_converter, "add_files") as convert:
        app.convert_saved_selection()
        convert.assert_called_once_with([task.files[0]])

    collection = "https://space.bilibili.com/517327498/lists/3993361?type=season"
    collection_result = {"title": "测试合集", "collection_kind": "season", "rows": [
        {"source": "bilibili", "title": "测试视频", "url": "https://www.bilibili.com/video/BV1GJ411x7h7?p=1",
         "position": 4, "total": 12, "duration": 35, "batch_key": "bilibili:collection:3993361", "batch_title": "测试合集"},
    ]}
    with patch.object(bili_collections, "resolve_collection", return_value=collection_result):
        assert app.add_link_text(collection)
        wait_for(window, lambda: len(browser.rows) == 1 and not browser.busy)
    row, = browser.tree.get_children()
    assert browser.tree.item(row, "values")[0] in (4, "4")
    browser.open_parts()
    wait_for(window, lambda: len(browser.rows) == 2 and not browser.busy)
    assert browser.back_button.winfo_manager() and "/video/" in browser.link.get()
    browser.back()
    assert browser.caption.get() == "测试合集" and browser.link.get() == collection
    browser.queue_all()
    collection_task, = [item for item in app.tasks.values() if item.batch_title == "测试合集"]
    assert (collection_task.position, collection_task.total) == (4, 12) and collection_task.url.endswith("?p=1")

    browser.open_parts()
    wait_for(window, lambda: len(browser.rows) == 2 and not browser.busy)
    edition_row = browser.tree.get_children()[0]
    browser.tree.selection_set(edition_row)
    source_row = dict(browser.rows[edition_row], audio_title="测试歌手", artist="测试歌曲")
    browser.rows[edition_row].update(source_row)
    original_title = source_row["title"]

    def all_children(parent):
        for child in parent.winfo_children():
            yield child
            yield from all_children(child)

    browser.edit_song()
    dialog = next(w for w in window.winfo_children() if isinstance(w, tk.Toplevel) and w.title() == "编辑歌名和歌手")
    buttons = {w.cget("text"): w for w in all_children(dialog) if w.winfo_class() == "TButton"}
    buttons["互换"].invoke()
    buttons["应用"].invoke()
    assert browser.tree.set(edition_row, "title") == "测试歌曲 · 测试歌手"
    assert browser.rows[edition_row]["title"] == original_title
    browser.queue_selected()
    edited, = [item for item in app.tasks.values() if item.source == "bilibili" and item.song_edited]
    assert (edited.audio_title, edited.artist) == ("测试歌曲", "测试歌手")
    persisted, = app.queue_store.add([source_row])
    assert (persisted.id, persisted.audio_title, persisted.artist) == (edited.id, "测试歌曲", "测试歌手")
finally:
    apple_download.download_video = original_bili_download
    apple_download.save_danmaku = original_danmaku
    apple_download.save_subtitle = original_subtitle
    apple_download.save_cover = original_cover
    bili_catalog._fetch_parts = original_parts
    bili_catalog._fetch_video_metadata = original_metadata
    app.bili_auth.clear()
    app.close()
print("Bilibili QR, encrypted session, song metadata, ASS/SRT/cover/JSON publishing, and collection navigation passed.")

ffmpeg = backend / "tools/ffmpeg.exe"
fragment = profile / "fixture-flac.m4s"
subprocess.run([str(ffmpeg), "-v", "error", "-i", str(profile / "local-test.flac"), "-c:a", "copy",
                "-strict", "experimental", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", str(fragment)],
               check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
flac_cdn = "https://cdn.bilivideo.com/track.m4s"
flac_manifest = {"flac": {"audio": {"codecs": "fLaC", "mimeType": "audio/mp4", "baseUrl": flac_cdn}}}

class MediaFixture(BytesIO):
    from email.message import Message
    status = 200
    headers = Message()
    headers["Content-Type"] = "audio/mp4"
    headers["Content-Length"] = str(fragment.stat().st_size)

flac_stage = profile / "flac-stage"
flac_stage.mkdir()
with patch.object(bili_download, "resolve_video", return_value={"rows": [{"cid": 101}]}), \
     patch.object(bili_download, "_playback", return_value=flac_manifest), \
     patch.object(bili_download, "_open", return_value=MediaFixture(fragment.read_bytes())) as cdn:
    flac_path = bili_download.download_video("https://www.bilibili.com/video/BV1GJ411x7h7?p=1", "flac", flac_stage,
                                             ffmpeg, threading.Event(), lambda _: None, session=session)
    assert cdn.call_args.args[0] == flac_cdn and "Cookie" not in cdn.call_args.kwargs
assert mutagen.File(flac_path).info.md5_signature == mutagen.File(profile / "local-test.flac").info.md5_signature
try:
    bili_download._select_flac({"flac": {"display": True, "audio": None}})
except RuntimeError as error:
    assert "原始 FLAC" in str(error)
else:
    raise AssertionError("Missing original FLAC must not fall back to AAC")
print("Bilibili original FLAC remux and unavailable-format handling passed.")

fixture_bytes = fragment.read_bytes()
half = len(fixture_bytes) // 2
pause = threading.Event()
requests = []

class ResumeFixture(MediaFixture):
    status = 206

    def __init__(self, offset):
        self.offset = offset
        super().__init__(fixture_bytes[offset:])
        self.headers = self.Message()
        self.headers["Content-Type"] = "audio/mp4"
        self.headers["Content-Length"] = str(len(fixture_bytes) - offset)
        self.headers["Content-Range"] = f"bytes {offset}-{len(fixture_bytes) - 1}/{len(fixture_bytes)}"
        self.headers["ETag"] = '"flac-fixture-v1"'

    def read(self, size=-1):
        if self.offset == 0:
            pause.set()
            return super().read(half)
        return super().read(size)

def resumable_cdn(url, referer, **headers):
    assert url == flac_cdn and "Cookie" not in headers
    requests.append(headers)
    return ResumeFixture(int(headers["Range"].split("=")[1].split("-")[0]))

checkpoint = profile / "resume-fixture"
stage1 = profile / "resume-1"
stage2 = profile / "resume-2"
stage1.mkdir()
stage2.mkdir()
with patch.object(bili_download, "resolve_video", return_value={"rows": [{"cid": 101}]}), \
     patch.object(bili_download, "_playback", return_value=flac_manifest), \
     patch.object(bili_download, "_open", side_effect=resumable_cdn):
    try:
        bili_download.download_video("https://www.bilibili.com/video/BV1GJ411x7h7?p=1", "flac", stage1,
                                     ffmpeg, pause, lambda _: None, checkpoint=checkpoint)
    except bili_download.Cancelled:
        assert not any(stage1.iterdir()) and (checkpoint / "audio.m4s.part").stat().st_size == half
    else:
        raise AssertionError("Pausing should persist a verified prefix")
    resumed = bili_download.download_video("https://www.bilibili.com/video/BV1GJ411x7h7?p=1", "flac", stage2,
                                            ffmpeg, threading.Event(), lambda _: None, checkpoint=checkpoint)
assert [headers["Range"] for headers in requests] == ["bytes=0-", f"bytes={half}-"]
assert requests[1]["If-Range"] == '"flac-fixture-v1"'
assert mutagen.File(resumed).info.md5_signature == mutagen.File(flac_path).info.md5_signature
bili_resume.forget_resume(checkpoint)
assert not checkpoint.exists()
print("Bilibili persistent range resume, checksummed media, and checkpoint cleanup passed.")

import shared_music
import shared_music_ui
import video_links_ui
import youtube_download
import qq_catalog
import netease_catalog
from netease_auth import NeteaseAccount, NeteaseSession
from qq_auth import QqAccount, QqQR, QqSession
from types import SimpleNamespace

window, app = new_window()
try:
    url = "https://y.qq.com/n/ryqq_v2/playlist/9505357778"
    result = {"source": "qq", "title": "Public Playlist", "url": url, "total": 5, "rows": [
        {"position": 1, "title": "=Fixture", "artist": "Artist", "album": "Album", "duration": 90,
         "url": "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qxYhV"},
    ]}
    with patch.object(qq_catalog, "resolve_public_qq", return_value=result):
        assert app.add_link_text(f"【QQ 音乐】给你分享一张歌单：{url}！")
        browser = app.shared_music_browsers["qq"]
        wait_for(window, lambda: len(browser.rows) == 1 and not browser.busy)
    assert app.active_page == "qq" and "1 / 5" in browser.count.get()
    assert "部分" in browser.status.get()
    output = profile / "shared-music.csv"
    with patch.object(shared_music_ui.filedialog, "asksaveasfilename", return_value=str(output)):
        browser.export_csv()
    assert "'=Fixture" in output.read_text(encoding="utf-8-sig")

    account = browser.account
    qq_session = QqSession("fixture1234", "isolated-fixture-qq-session", "Fixture account")
    login_image = QqQR((root / "assets/amdl-32.png").read_bytes(), "fixture-signature")
    collection = {"id": "42", "dir_id": 0, "kind": "created", "title": "QQ 测试歌单", "total": 1, "url": url}
    with patch.object(account.auth, "begin", return_value=login_image), \
         patch.object(qq_catalog, "list_playlists", return_value={"rows": [collection]}) as my_playlists:
        account.open()
        wait_for(window, lambda: hasattr(account, "image"))
        assert account.image.width() >= 100
        account.receive(account.serial, "signed_in", (qq_session, QqAccount(qq_session.musicid, "Fixture account")))
        wait_for(window, lambda: browser.view == "library" and not browser.busy)
        assert my_playlists.call_args.args == (qq_session,)
        assert account.auth.load() == qq_session
        assert b"isolated-fixture-qq-session" not in account.auth.path.read_bytes()
        account.close()

    qq_song = result["rows"][0]["url"]
    qq_playlist_result = dict(result, title=collection["title"], total=1, rows=[
        dict(result["rows"][0], title="QQ Fixture"),
    ])
    with patch.object(qq_catalog, "resolve_playlist", return_value=qq_playlist_result) as tracks:
        browser._open_selected()
        wait_for(window, lambda: browser.view == "tracks" and len(browser.rows) == 1 and not browser.busy)
        assert tracks.call_args.args[0] == qq_session
        assert tracks.call_args.args[1]["id"] == collection["id"]
    browser.library_button.invoke()
    assert browser.view == "library"
    with patch.object(qq_catalog, "resolve_playlist", return_value=qq_playlist_result):
        browser._open_selected()
        wait_for(window, lambda: browser.view == "tracks" and len(browser.rows) == 1 and not browser.busy)

    def qq_fixture(url, stage, ffmpeg, stop, on_status, *, session, expected_duration=None):
        assert url == qq_song and session == qq_session and expected_duration == 90
        return Path(shutil.copyfile(profile / "local-test.mp3", stage / "fixture.mp3"))

    def music_cover(url, stage, stop, on_status, *, session=None):
        return Path(shutil.copyfile(root / "assets/amdl-32.png", stage / "fixture-cover.png"))

    def music_lyrics(url, stage, stop, on_status, *, session=None):
        target = stage / "fixture.lrc"
        target.write_text("[00:02.00]测试歌词\n", encoding="utf-8")
        return target

    app.fields["folder"].set(str(profile))
    app.fields["category"].set("")
    app.fields["music_sidecars"].set(True)
    with patch.object(apple_download, "download_qq", side_effect=qq_fixture), \
         patch.object(apple_download.qq_sidecars, "save_cover", side_effect=music_cover), \
         patch.object(apple_download.qq_sidecars, "save_lyrics", side_effect=music_lyrics):
        browser._enqueue(True)
        wait_for(window, lambda: not app.busy)
    qq_task, = [task for task in app.queue_store.list_tasks() if task.source == "qq"]
    assert qq_task.state == "complete" and Path(qq_task.files[0]).name == "Artist - QQ Fixture.mp3"
    mp3, artwork, lyrics = map(Path, qq_task.files)
    assert artwork == mp3.with_suffix(".png") and lyrics == mp3.with_suffix(".lrc")
    assert lyrics.read_text(encoding="utf-8").startswith("[00:02.00]")
    tags = mutagen.File(mp3).tags
    assert tags["TIT2"].text == ["QQ Fixture"] and tags["TPE1"].text == ["Artist"]
    front_cover, = tags.getall("APIC")
    assert front_cover.mime == "image/png" and front_cover.data == artwork.read_bytes()
    app.fields["music_sidecars"].set(False)
    account.logout()
    assert not browser.rows and account.session is None and account.auth.load() is None

    netease_url = "https://music.163.com/playlist?id=3778678"
    netease_song = "https://music.163.com/song?id=1973665667"
    netease_result = dict(result, source="netease", url=netease_url, rows=[
        dict(result["rows"][0], title="NetEase Fixture", url=netease_song),
    ])
    with patch.object(netease_catalog, "resolve_public_netease", return_value=netease_result):
        assert app.add_link_text(f"这张歌单不错 https://music.163.com/#/playlist?id=3778678。")
        wait_for(window, lambda: len(app.shared_music_browsers["netease"].rows) == 1)
    assert app.active_page == "netease"

    app.fields["folder"].set(str(profile))
    app.fields["category"].set("")
    netease = app.shared_music_browsers["netease"]
    netease._enqueue(False, all_rows=True)
    song, = [task for task in app.tasks.values() if task.source == "netease"]
    assert (song.url, song.position, song.total, song.duration) == (netease_song, 1, 5, 90)

    def netease_fixture(url, stage, ffmpeg, stop, on_status, *, session=None, expected_duration=None):
        assert url == netease_song and expected_duration == 90 and session is None
        return Path(shutil.copyfile(profile / "local-test.mp3", stage / "fixture.mp3"))

    with patch.object(apple_download, "download_netease", side_effect=netease_fixture):
        netease._enqueue(True)
        wait_for(window, lambda: not app.busy)
    song = app.queue_store.get(song.id)
    assert song.state == "complete" and Path(song.files[0]).name == "Artist - NetEase Fixture.mp3"
    assert len(song.files) == 1 and Path(song.files[0]).read_bytes() == (profile / "local-test.mp3").read_bytes()

    netease_account = netease.account
    netease_session = NeteaseSession(12345, (("MUSIC_U", "isolated-fixture-netease-cookie"),))
    netease_qr = SimpleNamespace(url="https://music.163.com/login?codekey=fixture")
    my_netease_playlist = {"id": "1234", "title": "网易云测试歌单", "kind": "liked", "total": 1, "url": ""}
    with patch.object(netease_account.auth, "begin", return_value=netease_qr), \
         patch.object(netease_catalog, "list_playlists", return_value={"rows": [my_netease_playlist]}) as netease_list:
        netease_account.open()
        wait_for(window, lambda: hasattr(netease_account, "image"))
        netease_account.receive(netease_account.serial, "signed_in", (netease_session, NeteaseAccount(12345, "网易云测试")))
        wait_for(window, lambda: netease.view == "library" and not netease.busy)
        assert netease_list.call_args.args == (netease_session,)
        assert netease_account.auth.load() == netease_session
        assert b"isolated-fixture-netease-cookie" not in netease_account.auth.path.read_bytes()
        netease_account.close()

    private_netease = dict(netease_result, title=my_netease_playlist["title"], url="", total=1, rows=[
        dict(netease_result["rows"][0], title="NetEase Private Fixture"),
    ])
    with patch.object(netease_catalog, "resolve_playlist", return_value=private_netease) as netease_reads:
        netease._open_selected()
        wait_for(window, lambda: netease.view == "tracks" and len(netease.rows) == 1 and not netease.busy)
        assert netease_reads.call_args.args[0] == netease_session
    app.fields["music_sidecars"].set(True)
    with patch.object(apple_download, "download_netease", side_effect=lambda url, stage, *args, **kwargs:
                      Path(shutil.copyfile(profile / "local-test.mp3", stage / "fixture.mp3"))) as netease_download, \
         patch.object(apple_download.netease_sidecars, "save_cover", side_effect=RuntimeError("封面网络错误")), \
         patch.object(apple_download.netease_sidecars, "save_lyrics", side_effect=music_lyrics):
        netease._enqueue(True)
        wait_for(window, lambda: not app.busy)
        netease_download.assert_called_once()
        assert netease_download.call_args.kwargs["session"] == netease_session
    private_task, = [item for item in app.queue_store.list_tasks() if item.title == "NetEase Private Fixture"]
    assert private_task.state == "failed" and "封面网络错误" in private_task.error
    assert [Path(name).suffix for name in private_task.files] == [".mp3", ".lrc"]
    assert mutagen.File(private_task.files[0]).tags["TIT2"].text == ["NetEase Private Fixture"]
    app.fields["music_sidecars"].set(False)
    netease_account.logout()
    assert not netease.rows and netease_account.auth.load() is None

    youtube_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    youtube_row = {"source": "youtube", "title": "YouTube Fixture", "artist": "Channel", "url": youtube_url}
    with patch.object(youtube_download, "resolve_youtube", return_value={"title": youtube_row["title"], "rows": [youtube_row]}):
        assert app.add_link_text("推荐一个 YouTube 视频〈https://youtu.be/jNQXAC9IVRw〉")
        youtube = app.video_link_pages["youtube"]
        youtube.resolve()
        wait_for(window, lambda: len(youtube.rows) == 1 and not youtube.busy)
    app.youtube_format.set(Studio.YOUTUBE_FORMATS["audio"])

    def youtube_fixture(url, mode, stage, ffmpeg, stop, on_status):
        assert url == youtube_url and mode == "audio"
        return Path(shutil.copyfile(audio, stage / "fixture.m4a"))

    with patch.object(apple_download, "download_youtube", side_effect=youtube_fixture):
        youtube._enqueue(True)
        wait_for(window, lambda: not app.busy)
    video, = [task for task in app.queue_store.list_tasks() if task.source == "youtube"]
    assert video.state == "complete" and video.options["youtube_mode"] == "audio"
    assert Path(video.files[0]).name == "YouTube Fixture.m4a" and app.youtube_format_box.winfo_manager()
    assert not app.quality_box.winfo_manager() and not app.bili_format_box.winfo_manager()

    with patch.object(video_links_ui.webbrowser, "open") as opened:
        assert app.add_link_text("https://youtu.be/dQw4w9WgXcQ") and app.active_page == "youtube"
        douyin_url = "https://www.douyin.com/video/7431386431445384474"
        assert app.add_link_text(f"复制打开抖音，看看作品：{douyin_url}！") and app.active_page == "douyin"
        opened.assert_not_called()
        app.video_link_pages["douyin"].load_link("复制打开抖音 https://v.douyin.com/Qa23px/。")
        app.video_link_pages["douyin"].open_original()
        opened.assert_called_once_with("https://v.douyin.com/Qa23px/")
    with patch.object(app.local_converter, "choose_files") as choose:
        app.video_link_pages["douyin"].import_saved()
        choose.assert_called_once_with()
        assert app.active_page == "local"
finally:
    app.close()
print("QQ and NetEase routing, encrypted sessions, playlist queue, embedded art, lyrics, and partial failure passed.")

window, app = new_window()
try:
    for source, catalog, template in (
        ("qq", qq_catalog, "https://y.qq.com/n/ryqq_v2/songDetail/{:014d}"),
        ("netease", netease_catalog, "https://music.163.com/song?id={:d}"),
    ):
        calls = []

        def track(index):
            return {"source": source, "url": template.format(index), "title": f"曲目 {index}",
                    "artist": "Fixture Artist", "duration": 70, "position": index}

        def search(query, *, session=None, stop=None, offset=0):
            assert query == "示例"
            calls.append(offset)
            if calls == [0, 3]:
                raise RuntimeError("临时网络错误")
            rows, cursor = {0: ([track(1), track(2), track(3)], 3),
                            3: ([track(3), track(4)], 6),
                            6: ([track(4)], 9)}[offset]
            return {"source": source, "title": query, "rows": rows, "total": 10, "next_offset": cursor}

        app.show(source)
        browser = app.shared_music_browsers[source]
        with patch.object(catalog, "search_songs", side_effect=search):
            browser.link.set("示例")
            browser.resolve()
            wait_for(window, lambda: not browser.busy and len(browser.rows) == 3)
            selected = browser.tree.get_children()[0]
            browser.tree.selection_set(selected)
            with patch.object(browser.tree, "winfo_viewable", return_value=True):
                browser._scroll_changed("0.1", "0.8")
                assert calls == [0]
                browser._scroll_changed("0.5", "0.96")
            wait_for(window, lambda: not browser.busy and bool(browser.search_warning))
            assert len(browser.rows) == 3 and browser.retry_button.winfo_manager()
            browser.retry_button.invoke()
            wait_for(window, lambda: not browser.busy and len(browser.rows) == 4)
            assert browser.tree.selection() == (selected,)
            assert str(browser.queue_all_button.cget("state")) == "disabled"
            browser._load_next_search_page()
            wait_for(window, lambda: not browser.busy and browser.next_search_offset is None)
            assert len(browser.rows) == 4 and calls == [0, 3, 3, 6]
        with patch.object(app, "enqueue_tasks", return_value=1) as enqueue:
            browser.download_button.invoke()
            enqueue.assert_called_once()
            rows, begin = enqueue.call_args.args
            assert begin and rows[0]["url"] == template.format(1) and "position" not in rows[0]
finally:
    app.close()
print("QQ and NetEase song search, scroll pagination, retry, deduplication, and queue actions passed.")

window, app = new_window()
try:
    app.show("youtube")
    page = app.video_link_pages["youtube"]
    requests = []

    def item(index):
        return {"source": "youtube", "title": f"试验视频 {index}", "artist": "试验频道",
                "url": f"https://www.youtube.com/watch?v=YtFixture{index:02d}"}

    def search(query, *, stop=None, offset=0):
        assert query == "游戏原声"
        requests.append(offset)
        if requests == [0, 2]:
            raise RuntimeError("临时网络错误")
        rows = [item(1), item(2)] if offset == 0 else [item(2), item(3)]
        return {"title": query, "rows": rows, "next_offset": 2 if offset == 0 else None}

    with patch.object(youtube_download, "search_youtube", side_effect=search):
        page.load_link("游戏原声")
        page.read_button.invoke()
        wait_for(window, lambda: not page.busy and len(page.rows) == 2)
        assert str(page.download_button.cget("state")) == "disabled"
        row = page.tree.get_children()[0]
        page.tree.selection_set(row)
        with patch.object(page.tree, "winfo_viewable", return_value=True):
            page._scroll_changed("0.3", "0.91")
            assert requests == [0]
            page._scroll_changed("0.5", "0.96")
        wait_for(window, lambda: not page.busy and page.page_error)
        assert page.retry_button.winfo_manager() and len(page.rows) == 2
        page.retry_button.invoke()
        wait_for(window, lambda: not page.busy and len(page.rows) == 3)
        assert page.tree.selection() == (row,) and requests == [0, 2, 2] and page.next_offset is None
    with patch.object(app, "enqueue_tasks") as enqueue:
        page.download_button.invoke()
        enqueue.assert_called_once()
        rows, begin = enqueue.call_args.args
        assert begin and list(rows) == [item(1)]
    with patch.object(video_links_ui.webbrowser, "open", return_value=True) as opened:
        page.open_original()
        opened.assert_called_once_with(item(1)["url"])
finally:
    app.close()
print("YouTube search, scroll continuation, retry, selected download, and original link passed.")

from share_links import links_in_text
from short_music_links import short_music_source

assert not links_in_text("https://sample.invalid/?redirect=https://youtu.be/jNQXAC9IVRw", Studio.Studio.link_source)
assert not links_in_text("复制链接：https://music.163.com.example.org/song?id=1！", Studio.Studio.link_source)
assert short_music_source("https://c6.y.qq.com/base/fcgi-bin/u?__=ydKD1qUQ0Sd7") == "qq"
assert short_music_source("https://163cn.tv/jM4PL2") == "netease"
assert short_music_source("https://163cn.tv@example.org/jM4PL2") is None

window, app = new_window()
try:
    qq_song_url = "https://y.qq.com/n/ryqq_v2/songDetail/0039MnYb0qxYhV"
    netease_song_url = "https://music.163.com/song?id=22197007"
    youtube_video_url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    before = len(app.tasks)

    def public_music(url, *, session, stop):
        source = app.link_source(url)
        return {"source": source, "title": source, "url": url, "total": 1,
                "rows": [{"url": url, "title": source, "position": 1}]}

    with patch.object(Studio, "resolve_public_music_link", side_effect=public_music) as music, \
         patch.object(Studio, "resolve_youtube", return_value={"title": "视频", "rows": [
             {"source": "youtube", "url": youtube_video_url, "title": "视频"}]}):
        assert app.add_link_text(f"分享歌曲《晴天》{qq_song_url}！\n推荐{netease_song_url}。\n<{youtube_video_url}>\n{qq_song_url}")
        wait_for(window, lambda: not app.resolving_links)
        assert music.call_count == 2
    assert len(app.tasks) == before + 3
    assert {task.source for task in list(app.tasks.values())[-3:]} == {"qq", "netease", "youtube"}
finally:
    app.close()
print("Mobile share text, trusted-host handling, official short-link recognition and multi-source queue passed.")

web = backend / "web"
for command, version in (
    ([sys.executable, "-I", str(web / "yt-dlp.pyz"), "--ignore-config", "--version"], b"2026.10.26"),
    ([str(web / "qjs.exe"), "--version"], b"0.18.0"),
):
    completed = subprocess.run(command, capture_output=True, check=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    assert version in completed.stdout + completed.stderr
print("Bundled YouTube tools, source filters, and YouTube/NetEase queue output passed.")

window, app = new_window()
try:
    local_input = profile / "原始音轨.m4a"
    shutil.copyfile(audio, local_input)
    original = local_input.read_bytes()
    app.fields["native_account"].set(False)
    app.fields["auto_service"].set(False)
    converter = app.local_converter
    with patch.object(converter, "quick_actions") as popup:
        app.on_file_drop([str(local_input)])
        popup.assert_called_once_with()
    assert app.active_page == "local"
    assert "MP4" not in converter.format_box.cget("values")
    converter.format.set("MP3")
    converter.start()
    wait_for(window, lambda: not app.busy)
    task, = [item for item in app.queue_store.list_tasks() if item.source == "local"]
    assert task.state == "complete" and app.selected_queue_source() == "local"
    output, = map(Path, task.files)
    assert output == local_input.with_suffix(".mp3") and mutagen.File(output).info.length > 0
    assert local_input.read_bytes() == original
    details, = task.audio_details
    assert details["source"]["codec"] == "AAC-LC" and details["output"]["codec"] == "MP3"
    app.render_task(app.queue_store.set_state(task.id, "complete", audio_details=[]))
    with patch.object(Studio.messagebox, "showinfo") as displayed:
        app.show_task_details(str(task.id))
        wait_for(window, lambda: displayed.called)
        text = displayed.call_args.args[1]
        assert "输出音频 · MP3" in text and "源音频 ·" not in text
finally:
    app.close()
print("Local conversion, measured source/output formats, persistent details, and older task inspection passed.")
'@
    $smokeScript = Join-Path $testRoot 'smoke.py'
    [IO.File]::WriteAllText($smokeScript, $smokeCode, [Text.UTF8Encoding]::new($false))
    & (Join-Path $target 'runtime\python.exe') -I -B $smokeScript $target
    if ($LASTEXITCODE -ne 0) { throw 'The isolated installer smoke test failed.' }
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    $resolved = (Resolve-Path -LiteralPath $testRoot).Path
    if ($resolved -eq (Join-Path $temporaryDirectory $testName)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
