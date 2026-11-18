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
    if (-not (Test-Path -LiteralPath (Join-Path $target 'MediaDownloadDesk.exe') -PathType Leaf) -or
        (Test-Path -LiteralPath (Join-Path $target 'amdl.exe')) -or
        (Get-ChildItem -LiteralPath $target -Filter 'unins*')) {
        throw 'Setup did not produce a standalone, unregistered application directory.'
    }
    $launcher = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $target 'MediaDownloadDesk.exe'))
    if ($launcher.ProductName -cne '影音下载台') { throw 'Setup did not contain the branded launcher.' }
    $installedVersion = (Get-Content -LiteralPath (Join-Path $target 'app-version.txt') -Raw).Trim()
    if ($installedVersion -cne $launcher.ProductVersion) { throw 'Setup did not stamp the launcher with its update version.' }
    if ((Get-Content -LiteralPath (Join-Path $target 'Run.cmd') -Raw) -notmatch 'MediaDownloadDesk\.exe') {
        throw 'The portable launcher still references the old executable.'
    }

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
import app_update
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
        [str(backend / "tools/ffmpeg.exe"), "-v", "error", "-i", str(audio), "-c:a", codec,
         *(["-b:a", "128k"] if suffix == "mp3" else []), str(converted)],
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
release_url = "https://github.com/haiyang172/Apple-music-downloader/releases/tag/v999.0.0"
with patch.object(app_update, "check_update", return_value=app_update.UpdateCheck("available", "999.0.0", release_url)) as check:
    window, app = new_window()
    wait_for(window, lambda: app.update_release == release_url)
    check.assert_called_once_with(Studio.APP_VERSION)
try:
    assert window.title() == Studio.APP_TITLE
    assert Studio.APP_VERSION and Studio.APP_VERSION == (root / "app-version.txt").read_text(encoding="ascii").strip()
    assert app.navigation["settings"].cget("text") == "设置 · 有更新"
    with patch.object(Studio.webbrowser, "open") as open_release:
        app.release_button.invoke()
        open_release.assert_called_once_with(release_url)
    with patch.object(app_update, "check_update", return_value=app_update.UpdateCheck("current")):
        app.update_button.invoke()
        wait_for(window, lambda: not app.update_in_progress)
    assert app.update_release is None and "已是最新版本" in app.update_status.get()
    assert app.profile_text.get() == "登录" and app.brand_icon.width() >= 32
    assert app.profile_button.master == app.rail and app.navigation["components"].master == app.nav_content
    assert int(app.profile_button.grid_info()["row"]) > int(app.nav_canvas.master.grid_info()["row"])
    platforms = ("music", "bilibili", "qq", "netease", "youtube", "douyin")
    assert tuple(app.navigation)[:6] == platforms
    assert [app.navigation[key].cget("text") for key in platforms] == [
        "Apple Music", "B 站", "QQ 音乐", "网易云音乐", "YouTube", "抖音",
    ]
    assert int(app.navigation["douyin"].grid_info()["row"]) < int(app.nav_separator.grid_info()["row"])
    assert int(app.nav_separator.grid_info()["row"]) < int(app.navigation["download"].grid_info()["row"])
    for source in platforms:
        app.navigation[source].invoke()
        assert app.active_page == source and app.navigation[source].cget("style") == "NavActive.TButton"
    app.navigation["music"].invoke()
    app.music_browser.library_tab.invoke()
    assert app.active_page == "library" and app.navigation["music"].cget("style") == "NavActive.TButton"
    app.library_browser.account_tab.invoke()
    assert app.active_page == "account" and app.navigation["music"].cget("style") == "NavActive.TButton"
    app.apple_account_back.invoke()
    assert app.active_page == "library"
    app.library_browser.browse_tab.invoke()
    assert app.active_page == "music"
    app.profile_button.invoke()
    assert app.active_page == "account"
    app.apple_account_back.invoke()
    assert app.active_page == "music"
    for source, address in (("youtube", "https://www.youtube.com/"), ("douyin", "https://www.douyin.com/")):
        app.show(source)
        video = app.video_link_pages[source]
        video.account_button.invoke()
        assert app.active_page == Studio.ACCOUNT_PAGES[source]
        assert app.navigation[source].cget("style") == "NavActive.TButton"
        with patch.object(Studio.webbrowser, "open", return_value=True) as official_browser:
            video.browser_account_button.invoke()
            official_browser.assert_called_once_with(address)
        video.account_back_button.invoke()
        assert app.active_page == source
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

import account_ui
import apple_web_login

web_files = ("AppleMusicLogin.exe", "Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll",
             "WebView2Loader.dll", "WebView2-LICENSE.txt", "WebView2-NOTICE.txt",
             "MicrosoftEdgeWebView2RuntimeInstallerX64.exe")
assert all((root / "apple-web-login" / name).is_file() for name in web_files)
from hashlib import file_digest
runtime_installer = root / "apple-web-login/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
assert runtime_installer.stat().st_size == apple_web_login.RUNTIME_INSTALLER_SIZE
with runtime_installer.open("rb") as source:
    assert file_digest(source, "sha256").hexdigest() == apple_web_login.RUNTIME_INSTALLER_SHA256

if apple_web_login.runtime_available():
    web_ready, web_stop = threading.Event(), threading.Event()
    def official_page(status):
        if status.startswith("请在 Apple"):
            web_ready.set()
            web_stop.set()
    timeout = threading.Timer(25, web_stop.set)
    timeout.start()
    try:
        try:
            apple_web_login.connect_library(root, profile / "official-webview-smoke", "au", Studio.CatalogClient(), web_stop, official_page)
        except apple_web_login.Cancelled:
            pass
        else:
            raise AssertionError("An isolated unsigned web profile returned an account token.")
        assert web_ready.is_set(), "The official Apple Music page did not load in the bundled WebView helper."
        apple_web_login.clear_profile(root, profile / "official-webview-smoke", threading.Event())
    finally:
        timeout.cancel()
    print("Bundled native Apple web helper loaded the official page, cancelled cleanly and cleared its isolated profile.")

window, app = new_window()
try:
    candidate = "fixture-official-web-token-" + "b" * 60
    app.password.set("form-password-stays")
    def official_connection(root_path, profile_path, region, catalog, stop, report):
        assert root_path == root and profile_path == settings.DATA / "apple-web-profile"
        report("正在验证 Apple Music 资料库授权…")
        music_browser.LibraryClient(candidate, region, catalog=catalog).items(limit=1)
        return candidate
    with patch.object(apple_web_login, "connect_library", side_effect=official_connection), \
         patch.object(app.catalog, "request", return_value={"data": []}) as authorized:
        app.connect_library()
        wait_for(window, lambda: app.active_page == "library" and app.library_token() == candidate and not app.web_login_busy)
        assert authorized.call_args_list[0].kwargs["user_token"] == candidate
        assert candidate not in settings.FILE.read_text(encoding="utf-8")
        assert app.password.get() == "form-password-stays"
    app.clear_account()

    started, released = threading.Event(), threading.Event()
    def delayed_connection(*args):
        started.set()
        assert released.wait(10)
        return candidate
    with patch.object(apple_web_login, "connect_library", side_effect=delayed_connection):
        app.connect_library()
        wait_for(window, started.is_set)
        dialog = app.library_connection_dialog
        dialog._dismiss()
        assert dialog._stop.is_set()
        released.set()
        wait_for(window, lambda: not app.web_login_busy)
        assert not app.library_token()
    web_profile = settings.DATA / "apple-web-profile"
    web_profile.mkdir(parents=True, exist_ok=True)
    with patch.object(apple_web_login, "clear_profile", side_effect=RuntimeError("fixture cleanup failure")):
        app.clear_account()
        wait_for(window, lambda: not app.web_profile_clearing)
    assert app.preferences["apple_web_clear_pending"] and settings.load()["apple_web_clear_pending"]
    assert str(app.logout_button.cget("state")) == "normal"
    with patch.object(apple_web_login, "clear_profile", return_value=None) as cleared:
        app.logout_button.invoke()
        wait_for(window, lambda: not app.web_profile_clearing and not app.preferences["apple_web_clear_pending"])
        cleared.assert_called_once_with(root, web_profile, app.web_profile_stop)
    web_profile.rmdir()
finally:
    app.close()
print("Apple official authorization automatically opened the library, encrypted the validated session and ignored cancelled results.")

original_parts = bili_catalog._fetch_parts
original_metadata = bili_catalog._fetch_video_metadata
original_bili_download = apple_download.download_video
original_danmaku = apple_download.save_danmaku
original_subtitle = apple_download.save_subtitle
original_cover = apple_download.save_cover
session = BiliSession((("SESSDATA", "fixture-account-session"),))
parts_calls = []
assert bili_catalog.classify_title("周杰倫 Jay Chou【夜曲 Nocturne】-Official", "音乐") == {
    "audio_title": "夜曲 Nocturne", "artist": "周杰倫 Jay Chou", "media_category": "music",
}
assert bili_catalog.classify_title("夜曲", "音乐", part=True)["audio_title"] == "夜曲"
assert bili_catalog.classify_title("《三体》解说", "音乐") == {
    "audio_title": None, "artist": "", "media_category": "video",
}
assert bili_catalog.classify_title("《女神异闻录5》原声 · 歌名：《Life Will Change》｜演唱：Lyn", "音乐") == {
    "audio_title": "Life Will Change", "artist": "Lyn", "media_category": "music",
}
assert bili_catalog.classify_title("《富士山下》六种语言翻唱｜原唱：陈奕迅", "音乐")["artist"] == ""
assert bili_catalog.suggest_folder("Persona 5 OST全集", "游戏", "music") == "Persona 系列"


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
        account.button.invoke()
        assert app.active_page == "bilibili_account" and account.page is app.pages["bilibili_account"]
        wait_for(window, lambda: account.image is not None)
        scan_serial, scan_stop = account.serial, account.stop
        app.navigation["qq"].invoke()
        assert scan_stop.is_set() and account.image is None and app.active_page == "qq"
        account.receive(scan_serial, "signed_in", (session, BiliAccount(1, "Stale login")))
        assert account.session is None and app.bili_auth.load() is None
        app.show("bilibili")
        app.profile_button.invoke()
        assert app.active_page == "bilibili_account"
        wait_for(window, lambda: account.image is not None)
        assert account.image.width() >= 100
        account.receive(account.serial, "signed_in", (session, BiliAccount(1, "Fixture account")))
        assert app.bili_auth.load() == session
        assert b"fixture-account-session" not in app.bili_auth.path.read_bytes()
        assert app.profile_button.cget("style") == "AccountSignedInActive.TButton"
        account.back_button.invoke()
        assert app.active_page == "bilibili"
    browser.link.set("【哔哩哔哩】分享作品 https://www.bilibili.com/video/BV1GJ411x7h7）。")
    browser.resolve()
    wait_for(window, lambda: len(browser.rows) == 2 and not browser.busy)
    assert browser.caption.get() == "公开演出合集"
    assert browser.metadata.get() == "投稿者测试  ·  音乐"
    track_row = browser.tree.get_children()[1]
    assert browser.tree.set(track_row, "title") == "夜曲" and browser.tree.set(track_row, "artist") == "周杰伦"
    assert tuple(browser.tree.cget("displaycolumns")) == ("position", "title", "artist", "category", "duration")
    assert browser.tree.set(track_row, "category") == "音乐"
    width, height = window.minsize()
    window.geometry(f"{width}x{height}")
    window.attributes("-alpha", 0.0)
    window.deiconify()
    window.update()
    assert all(app.navigation[page].winfo_viewable() for page in Studio.PLATFORM_PAGES)
    x, _, duration_width, _ = browser.tree.bbox(track_row, "duration")
    assert x + duration_width <= browser.tree.winfo_width()
    window.withdraw()
    app.fields["category"].set("车载")
    assert browser.tree.set(track_row, "category") == "车载"
    app.fields["category"].set("")
    app.fields["bili_auto_category"].set(False)
    assert not browser.tree.set(track_row, "category")
    app.fields["bili_auto_category"].set(True)
    app.bili_format.set(Studio.BILI_FORMATS["video"])
    assert "《夜曲》" in browser.tree.set(track_row, "title")
    assert tuple(browser.tree.cget("displaycolumns")) == ("position", "title", "duration")
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
        {"source": "bilibili", "title": "Persona 5 OST · 《Life Will Change》", "url": "https://www.bilibili.com/video/BV1GJ411x7h7?p=1",
         "preview_audio_title": "Life Will Change", "preview_artist": "Lyn", "preview_media_category": "music",
         "position": 4, "total": 12, "duration": 35, "batch_key": "bilibili:collection:3993361", "batch_title": "测试合集"},
    ]}
    with patch.object(bili_collections, "resolve_collection", return_value=collection_result):
        assert app.add_link_text(collection)
        wait_for(window, lambda: len(browser.rows) == 1 and not browser.busy)
    row, = browser.tree.get_children()
    assert browser.tree.item(row, "values")[0] in (4, "4")
    assert browser.tree.set(row, "title") == "Life Will Change"
    assert browser.tree.set(row, "artist") == "Lyn" and browser.tree.set(row, "category") == "Persona 系列"
    browser.open_parts()
    wait_for(window, lambda: len(browser.rows) == 2 and not browser.busy)
    assert browser.back_button.winfo_manager() and "/video/" in browser.link.get()
    browser.back()
    assert browser.caption.get() == "测试合集" and browser.link.get() == collection
    browser.queue_all()
    collection_task, = [item for item in app.tasks.values() if item.batch_title == "测试合集"]
    assert (collection_task.position, collection_task.total) == (4, 12) and collection_task.url.endswith("?p=1")
    assert collection_task.audio_title is None and not collection_task.artist

    browser.open_parts()
    wait_for(window, lambda: len(browser.rows) == 2 and not browser.busy)
    part_row = browser.tree.get_children()[0]
    browser.tree.selection_set(part_row)
    position = browser._position()
    part_url = browser.rows[part_row]["url"]
    browser._render(browser.result)
    browser._restore_position(position)
    assert browser.rows[browser.tree.selection()[0]]["url"] == part_url
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
    assert browser.tree.set(edition_row, "title") == "测试歌曲" and browser.tree.set(edition_row, "artist") == "测试歌手"
    assert browser.rows[edition_row]["title"] == original_title
    browser.queue_selected()
    edited, = [item for item in app.tasks.values() if item.source == "bilibili" and item.song_edited]
    assert (edited.audio_title, edited.artist) == ("测试歌曲", "测试歌手")
    persisted, = app.queue_store.add([source_row])
    assert (persisted.id, persisted.audio_title, persisted.artist) == (edited.id, "测试歌曲", "测试歌手")

    uid = account.account.uid
    folder = {"id": "favorite:4242", "kind": "favorite", "collection_id": 4242, "owner_id": uid,
              "title": "驾驶音乐", "total": 2, "_account_id": uid,
              "url": f"https://space.bilibili.com/{uid}/favlist?fid=4242"}
    followed = {"id": "season:9393", "kind": "season", "collection_id": 9393, "owner_id": 20259914,
                "title": "已收藏的合集", "total": 8, "_account_id": uid,
                "url": "https://space.bilibili.com/20259914/lists/9393?type=season"}
    favorites = {"source": "bilibili", "view": "favorites", "title": "我的收藏夹", "rows": [folder, followed],
                 "_account_id": uid, "total": 2, "complete": True, "warning": ""}
    first_video = {"source": "bilibili", "id": "BV1GJ411x7h7", "title": "Persona 5 OST · 《Life Will Change》",
                   "url": "https://www.bilibili.com/video/BV1GJ411x7h7?p=1", "uploader": "收藏夹投稿者", "artist": "",
                   "preview_audio_title": "Life Will Change", "preview_artist": "Lyn", "preview_media_category": "music",
                   "preview_source_category": "音乐", "position": 2, "duration": 35, "total": 2,
                   "batch_key": "bilibili:favorite:4242", "batch_title": folder["title"]}
    contents = {"source": "bilibili", "id": "4242", "title": folder["title"], "collection_kind": "favorite", "url": folder["url"],
                "total": 2, "scanned": 2, "skipped": 1, "complete": True, "_account_id": uid,
                "folder_id": folder["id"], "rows": [first_video],
                "warning": "已跳过 1 项不可用或不支持的内容"}
    cache = app.bili_favorites_cache
    assert cache.replace(uid, favorites) and cache.replace(uid, contents, folder=folder)
    assert cache.read(uid + 1) is None
    index_started, index_release = threading.Event(), threading.Event()
    folder_started, folder_release = threading.Event(), threading.Event()

    def refresh_index(current_session, current_account, *, stop=None):
        assert current_session == session and current_account.uid == uid
        index_started.set()
        assert index_release.wait(10)
        return dict(favorites, complete=False, rows=[folder], warning="临时只返回部分收藏夹")

    def refresh_folder(current_session, current_account, selection, *, stop=None):
        assert current_session == session and current_account.uid == uid and selection["id"] == folder["id"]
        folder_started.set()
        assert folder_release.wait(10)
        return contents

    with patch.object(bili_collections, "list_my_favorites", side_effect=refresh_index), \
         patch.object(bili_collections, "resolve_my_favorite", side_effect=refresh_folder):
        try:
            app.show("bilibili")
            browser.favorites_button.invoke()
            wait_for(window, lambda: index_started.is_set() and len(browser.rows) == 2 and not browser.busy)
            assert browser.favorites_refreshing
            open_row = next(row for row, item in browser.rows.items() if item["id"] == folder["id"])
            browser.tree.selection_set(open_row)
            window.update()
            assert str(browser.queue_all_button.cget("state")) == "disabled"
            assert str(browser.download_button.cget("state")) == "disabled"
            index_release.set()
            wait_for(window, lambda: not browser.favorites_refreshing)
            assert browser.rows[browser.tree.selection()[0]]["id"] == folder["id"]
            assert len(cache.read(uid)["rows"]) == 2

            browser.open_folder()
            wait_for(window, lambda: folder_started.is_set() and len(browser.rows) == 1 and not browser.busy)
            assert browser.favorites_refreshing and browser.caption.get() == folder["title"]
            cached_row, = browser.tree.get_children()
            assert browser.tree.set(cached_row, "title") == "Life Will Change"
            browser.queue_all()
            favorite_task, = [item for item in app.tasks.values() if item.batch_key == "bilibili:favorite:4242"]
            assert favorite_task.url.endswith("?p=1") and (favorite_task.position, favorite_task.total) == (2, 2)
            browser.open_parts()
            folder_release.set()
            wait_for(window, lambda: browser.caption.get() == "公开演出合集" and len(browser.rows) == 2 and not browser.busy)
            browser.back()
            assert browser.caption.get() == folder["title"] and len(browser.rows) == 1
            browser.back()
            assert browser.caption.get() == favorites["title"]
            assert browser.rows[browser.tree.selection()[0]]["id"] == folder["id"]
            account.logout()
            assert account.session is None and not browser.rows
        finally:
            index_release.set()
            folder_release.set()
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
print("Bilibili personal favorites cache, account scoping, two-level return, and P1 queue passed.")

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
    collection = {"id": "42", "tid": 42, "dir_id": 0, "kind": "created", "title": "QQ 测试歌单",
                  "total": 1, "url": "https://y.qq.com/n/ryqq_v2/playlist/42", "private": False,
                  "_account_id": qq_session.musicid}
    with patch.object(account.auth, "begin", return_value=login_image), \
         patch.object(qq_catalog, "list_playlists", return_value={"rows": [collection]}) as my_playlists:
        account.open()
        assert app.active_page == "qq_account"
        wait_for(window, lambda: account.image is not None)
        assert account.image.width() >= 100
        account.receive(account.serial, "signed_in", (qq_session, QqAccount(qq_session.musicid, "Fixture account")))
        wait_for(window, lambda: browser.view == "library" and not browser.busy)
        assert my_playlists.call_args.args == (qq_session,)
        assert account.auth.load() == qq_session
        assert b"isolated-fixture-qq-session" not in account.auth.path.read_bytes()
        account.back_button.invoke()
        assert app.active_page == "qq"

    qq_song = result["rows"][0]["url"]
    qq_playlist_result = dict(result, title=collection["title"], total=1, rows=[
        dict(result["rows"][0], title="QQ Fixture", source="qq", id="0039MnYb0qxYhV"),
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

    def qq_fixture(url, stage, ffmpeg, stop, on_status, *, session, expected_duration=None, source_quality=None):
        assert url == qq_song and session == qq_session and expected_duration == 90
        assert source_quality == "auto"
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
    app.output_label.set(Studio.audio.OUTPUTS["original"])
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
    app.output_label.set(Studio.audio.OUTPUTS["original"])
    netease = app.shared_music_browsers["netease"]
    netease._enqueue(False, all_rows=True)
    song, = [task for task in app.tasks.values() if task.source == "netease"]
    assert (song.url, song.position, song.total, song.duration) == (netease_song, 1, 5, 90)

    def netease_fixture(url, stage, ffmpeg, stop, on_status, *, session=None, expected_duration=None, source_quality=None):
        assert url == netease_song and expected_duration == 90 and session is None
        assert source_quality == "auto"
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
    my_netease_playlist = {"id": "1234", "title": "网易云测试歌单", "kind": "liked", "total": 1, "url": "",
                          "private": True, "_account_id": netease_session.user_id}
    with patch.object(netease_account.auth, "begin", return_value=netease_qr), \
         patch.object(netease_catalog, "list_playlists", return_value={"rows": [my_netease_playlist]}) as netease_list:
        netease_account.open()
        assert app.active_page == "netease_account"
        wait_for(window, lambda: netease_account.image is not None)
        netease_account.receive(netease_account.serial, "signed_in", (netease_session, NeteaseAccount(12345, "网易云测试")))
        wait_for(window, lambda: netease.view == "library" and not netease.busy)
        assert netease_list.call_args.args == (netease_session,)
        assert netease_account.auth.load() == netease_session
        assert b"isolated-fixture-netease-cookie" not in netease_account.auth.path.read_bytes()
        netease_account.back_button.invoke()
        assert app.active_page == "netease"

    private_netease = dict(netease_result, title=my_netease_playlist["title"], url="", total=1, rows=[
        dict(netease_result["rows"][0], title="NetEase Private Fixture", source="netease", id="1973665667"),
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

    web_preferences = app.snapshot()
    web_output = profile / "web-audio-outputs"
    web_output.mkdir()
    app.fields["folder"].set(str(web_output))
    app.fields["music_sidecars"].set(True)
    app.source_quality_boxes["qq"].set(Studio.MUSIC_SOURCE_QUALITIES["qq"]["128"])
    app.source_quality_boxes["netease"].set(Studio.MUSIC_SOURCE_QUALITIES["netease"]["flac"])
    app.music_output_box.set(Studio.audio.OUTPUTS["aac"])
    app.music_bitrate_box.set("192")
    app.quality_changed(None)
    saved = settings.load()
    assert (saved["qq_quality"], saved["netease_quality"], saved["output"], saved["mp3_bitrate"]) == (
        "128", "flac", "aac", 192,
    )
    mp3_details = Studio.audio.audio_details(profile / "local-test.mp3")
    flac_details = Studio.audio.audio_details(profile / "local-test.flac")
    assert mp3_details["codec"] == "MP3" and abs(mp3_details["bitrate"] - 128_000) < 4000
    mp3_duration = mutagen.File(profile / "local-test.mp3").info.length
    flac_duration = mutagen.File(profile / "local-test.flac").info.length

    def qq_aac_fixture(url, stage, ffmpeg, stop, on_status, *, session, expected_duration, source_quality):
        assert url == qq_song and session == qq_session
        assert expected_duration == mp3_duration and source_quality == "128"
        return Path(shutil.copyfile(profile / "local-test.mp3", stage / "native.mp3"))

    app.shared_music_browsers["qq"].account.session = qq_session
    with patch.object(apple_download, "download_qq", side_effect=qq_aac_fixture) as qq_aac_download, \
         patch.object(apple_download.qq_sidecars, "save_cover", side_effect=music_cover), \
         patch.object(apple_download.qq_sidecars, "save_lyrics", side_effect=music_lyrics):
        assert app.enqueue_tasks([{"source": "qq", "url": qq_song, "title": "QQ AAC Fixture", "artist": "Artist",
                                  "duration": mp3_duration, "batch_key": "web-audio-qq-aac"}], True) == 1
        wait_for(window, lambda: not app.busy)
        qq_aac_download.assert_called_once()
    aac_task, = [task for task in app.queue_store.list_tasks() if task.title == "QQ AAC Fixture"]
    assert aac_task.state == "complete", aac_task.error
    assert aac_task.options["qq_quality"] == "128" and aac_task.options["output"] == "aac"
    m4a, artwork, lyrics = map(Path, aac_task.files)
    assert m4a.name == "Artist - QQ AAC Fixture.m4a" and m4a.parent == web_output
    assert artwork == m4a.with_suffix(".png") and lyrics == m4a.with_suffix(".lrc")
    tags = mutagen.File(m4a).tags
    assert tags["\xa9nam"] == ["QQ AAC Fixture"] and tags["\xa9ART"] == ["Artist"]
    assert bytes(tags["covr"][0]) == artwork.read_bytes() and tags["covr"][0].imageformat == 14
    assert tags["\xa9lyr"] == [lyrics.read_text(encoding="utf-8")]
    measured, = aac_task.audio_details
    assert measured["source"] == mp3_details and measured["output"] == Studio.audio.audio_details(m4a)
    assert measured["output"]["codec"] == "AAC-LC" and measured["output"]["bitrate"] > 0

    app.music_output_box.set(Studio.audio.OUTPUTS["mp3"])
    app.quality_changed(None)
    entered_native_download = threading.Event()

    def pause_netease_flac(url, stage, ffmpeg, stop, on_status, *, session, expected_duration, source_quality):
        assert url == netease_song and session is None
        assert expected_duration == flac_duration and source_quality == "flac"
        entered_native_download.set()
        assert stop.wait(10), "The normal pause action did not reach the downloader"
        raise apple_download.Cancelled()

    with patch.object(apple_download, "download_netease", side_effect=pause_netease_flac):
        assert app.enqueue_tasks([{"source": "netease", "url": netease_song, "title": "NetEase FLAC Fixture",
                                  "artist": "Artist", "duration": flac_duration,
                                  "batch_key": "web-audio-netease-mp3"}], True) == 1
        wait_for(window, entered_native_download.is_set)
        app.pause()
        wait_for(window, lambda: not app.busy)
    flac_task, = [task for task in app.queue_store.list_tasks() if task.title == "NetEase FLAC Fixture"]
    assert flac_task.state == "interrupted" and not flac_task.files
    captured_options = dict(flac_task.options)
    assert (captured_options["netease_quality"], captured_options["output"], captured_options["mp3_bitrate"]) == (
        "flac", "mp3", 192,
    )
    app.source_quality_boxes["netease"].set(Studio.MUSIC_SOURCE_QUALITIES["netease"]["128"])
    app.music_output_box.set(Studio.audio.OUTPUTS["original"])
    app.music_bitrate_box.set("320")
    app.quality_changed(None)
    app.close()
    window, app = new_window()
    assert app.source_quality_labels["qq"].get() == Studio.MUSIC_SOURCE_QUALITIES["qq"]["128"]
    assert app.source_quality_labels["netease"].get() == Studio.MUSIC_SOURCE_QUALITIES["netease"]["128"]
    assert app.snapshot()["output"] == "original" and app.snapshot()["mp3_bitrate"] == 320
    assert app.queue_store.get(flac_task.id).options == captured_options
    assert app.queue_store.get(aac_task.id).audio_details == aac_task.audio_details

    def netease_flac_fixture(url, stage, ffmpeg, stop, on_status, *, session, expected_duration, source_quality):
        assert url == netease_song and session is None
        assert expected_duration == flac_duration and source_quality == "flac"
        return Path(shutil.copyfile(profile / "local-test.flac", stage / "native.flac"))

    with patch.object(apple_download, "download_netease", side_effect=netease_flac_fixture) as flac_download, \
         patch.object(apple_download.netease_sidecars, "save_cover", side_effect=music_cover), \
         patch.object(apple_download.netease_sidecars, "save_lyrics", side_effect=music_lyrics):
        app.start_download([flac_task.id])
        wait_for(window, lambda: not app.busy)
        flac_download.assert_called_once()
    flac_task = app.queue_store.get(flac_task.id)
    assert flac_task.state == "complete" and flac_task.options == captured_options, flac_task.error
    mp3, artwork, lyrics = map(Path, flac_task.files)
    assert mp3.name == "Artist - NetEase FLAC Fixture.mp3" and mp3.parent == web_output
    tags = mutagen.File(mp3).tags
    assert tags["TIT2"].text == ["NetEase FLAC Fixture"] and tags["TPE1"].text == ["Artist"]
    front_cover, = tags.getall("APIC")
    embedded_lyrics, = tags.getall("USLT")
    assert front_cover.data == artwork.read_bytes() and embedded_lyrics.text == lyrics.read_text(encoding="utf-8")
    measured, = flac_task.audio_details
    assert measured["source"] == flac_details and measured["source"]["codec"] == "FLAC"
    assert measured["output"] == Studio.audio.audio_details(mp3) and measured["output"]["codec"] == "MP3"
    assert abs(measured["output"]["bitrate"] - 192_000) < 6000
    assert app.snapshot()["netease_quality"] == "128" and app.snapshot()["output"] == "original"

    # Pre-.34 tasks omit source quality; the provider's original default must receive no new keyword.
    legacy_options = {key: value for key, value in captured_options.items()
                      if key not in ("qq_quality", "netease_quality")}
    legacy_options.update(output="original", music_sidecars=False)
    app.source_quality_boxes["qq"].set(Studio.MUSIC_SOURCE_QUALITIES["qq"]["flac"])
    app.source_quality_boxes["netease"].set(Studio.MUSIC_SOURCE_QUALITIES["netease"]["hires"])
    app.music_output_box.set(Studio.audio.OUTPUTS["aac"])
    app.quality_changed(None)
    app.shared_music_browsers["qq"].account.session = qq_session

    def legacy_music_fixture(url, stage, ffmpeg, stop, on_status, *, session, expected_duration):
        assert (url, session) in ((qq_song, qq_session), (netease_song, None))
        assert expected_duration == mp3_duration
        return Path(shutil.copyfile(profile / "local-test.mp3", stage / "legacy.mp3"))

    with patch.object(apple_download, "download_qq", side_effect=legacy_music_fixture) as legacy_qq, \
         patch.object(apple_download, "download_netease", side_effect=legacy_music_fixture) as legacy_netease:
        assert app.enqueue_tasks([
            {"source": source, "url": link, "title": f"Legacy {source} Fixture", "duration": mp3_duration,
             "batch_key": "web-audio-legacy", "options": legacy_options}
            for source, link in (("qq", qq_song), ("netease", netease_song))
        ], True) == 2
        wait_for(window, lambda: not app.busy)
        for download in (legacy_qq, legacy_netease):
            download.assert_called_once()
            assert "source_quality" not in download.call_args.kwargs
    for source in ("qq", "netease"):
        legacy_task, = [task for task in app.queue_store.list_tasks() if task.title == f"Legacy {source} Fixture"]
        assert legacy_task.state == "complete" and legacy_task.options == legacy_options, legacy_task.error
        assert Path(legacy_task.files[0]).read_bytes() == (profile / "local-test.mp3").read_bytes()
        measured, = legacy_task.audio_details
        assert measured["source"] == measured["output"] == mp3_details
    app.shared_music_browsers["qq"].account.session = None
    for key in ("folder", "category", "music_sidecars", "mp3_bitrate"):
        app.fields[key].set(web_preferences[key])
    for source in ("qq", "netease"):
        app.source_quality_labels[source].set(Studio.MUSIC_SOURCE_QUALITIES[source][web_preferences[f"{source}_quality"]])
    app.output_label.set(Studio.audio.OUTPUTS[web_preferences["output"]])
    app.save_preferences()
    print("QQ/NetEase source quality, native-to-output encoding, tagged sidecars, saved restart settings, and legacy defaults passed.")

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

from contextlib import ExitStack
from shared_music_cache import SharedMusicCache

for source, catalog, session, verified_account, owner, other_owner, song_link in (
    ("qq", qq_catalog, QqSession("2345678", "cache-fixture-qq-key", "Cache account"),
     QqAccount("2345678", "Cache account"), "2345678", "3456789", "https://y.qq.com/n/ryqq_v2/songDetail/"),
    ("netease", netease_catalog, NeteaseSession(2345678, (("MUSIC_U", "cache-fixture-netease-key"),)),
     NeteaseAccount(2345678, "Cache account"), 2345678, 3456789, "https://music.163.com/song?id="),
):
    cache_path = settings.DATA / "shared-music.sqlite3"
    private_cache = SharedMusicCache(cache_path)

    def playlist(identifier, title):
        base = {"id": str(identifier), "title": title, "kind": "created", "total": 2,
                "private": source == "netease", "_account_id": owner,
                "url": f"https://y.qq.com/n/ryqq_v2/playlist/{identifier}" if source == "qq" else ""}
        if source == "qq":
            base.update(tid=identifier, dir_id=0)
        return base

    def library(rows, warning=""):
        return {"source": source, "view": "library", "title": "我的歌单", "rows": rows,
                "total": len(rows), "warnings": [warning] if warning else []}

    def track(number, position):
        identifier = f"{number:014}" if source == "qq" else str(number)
        return {"source": source, "id": identifier, "url": song_link + identifier, "title": f"缓存曲目 {number}",
                "artist": "Cache Artist", "album": "Cache Album", "duration": 90, "position": position}

    def tracks(rows):
        return {"source": source, "title": second["title"], "rows": rows, "total": len(rows), "url": second["url"]}

    first, second = playlist(43001, "上次的歌单"), playlist(43002, "选中的歌单")
    old_tracks = [track(44001, 1), track(44002, 2)]
    assert private_cache.replace(source, owner, library([first, second]))
    assert private_cache.replace(source, owner, tracks(old_tracks), playlist=second)
    assert private_cache.read(source, other_owner) is None

    library_fetching, library_reply = threading.Event(), threading.Event()
    track_fetching, track_reply = threading.Event(), threading.Event()

    def incomplete_library(current, *, stop=None):
        assert current == session
        library_fetching.set()
        assert library_reply.wait(10)
        return library([first], "平台暂只返回部分歌单。")

    def updated_tracks(current, selection, *, stop=None):
        assert current == session and selection["id"] == second["id"]
        track_fetching.set()
        assert track_reply.wait(10)
        return tracks([track(44002, 1), track(44003, 2)])

    with ExitStack() as context:
        for provider, auth in (("qq", Studio.QqAuth), ("netease", Studio.NeteaseAuth)):
            context.enter_context(patch.object(auth, "load", return_value=session if provider == source else None))
            context.enter_context(patch.object(auth, "check", return_value=verified_account if provider == source else None))
        context.enter_context(patch.object(catalog, "list_playlists", side_effect=incomplete_library))
        context.enter_context(patch.object(catalog, "resolve_playlist", side_effect=updated_tracks))
        window, app = new_window()
        try:
            app.show(source)
            browser = app.shared_music_browsers[source]
            wait_for(window, lambda: library_fetching.is_set() and browser.view == "library" and len(browser.rows) == 2)
            assert browser.private_refreshing and not browser.busy
            selected = next(row for row, item in browser.rows.items() if item["id"] == second["id"])
            browser.tree.selection_set(selected)
            library_reply.set()
            wait_for(window, lambda: not browser.private_refreshing)
            assert browser.rows[browser.tree.selection()[0]]["id"] == second["id"]
            assert [item["id"] for item in private_cache.read(source, owner)["rows"]] == [first["id"], second["id"]]

            browser._open_selected()
            wait_for(window, lambda: track_fetching.is_set() and browser.view == "tracks" and len(browser.rows) == 2)
            assert browser.private_refreshing and not browser.busy
            selected = next(row for row, item in browser.rows.items() if item["id"] == old_tracks[1]["id"])
            browser.tree.selection_set(selected)
            window.update()
            assert str(browser.queue_button.cget("state")) == "normal"
            track_reply.set()
            wait_for(window, lambda: not browser.private_refreshing)
            assert browser.rows[browser.tree.selection()[0]]["id"] == old_tracks[1]["id"]
            assert [item["title"] for item in private_cache.read(source, owner, playlist=second)["rows"]] == [
                "缓存曲目 44002", "缓存曲目 44003",
            ]
            browser.library_button.invoke()
            wait_for(window, lambda: browser.view == "library" and not browser.private_refreshing)
            expired = (qq_catalog.QqCatalogError("QQ 登录已过期", code=1000) if source == "qq"
                       else netease_catalog.NeteaseCatalogError("网易云登录已过期", code=301))
            with patch.object(catalog, "list_playlists", side_effect=expired):
                browser.read_library()
                wait_for(window, lambda: browser.account.session is None)
            assert not browser.rows
            assert "过期" in browser.account.status.get()

            new_session = (replace(session, musicid=other_owner) if source == "qq"
                           else replace(session, user_id=other_owner))
            new_account = (replace(verified_account, musicid=other_owner) if source == "qq"
                           else replace(verified_account, uid=other_owner))
            switch_read, switch_reply = threading.Event(), threading.Event()

            def empty_library(current, *, stop=None):
                assert current == new_session
                switch_read.set()
                assert switch_reply.wait(10)
                return library([])

            with patch.object(catalog, "list_playlists", side_effect=empty_library):
                browser.account.session, browser.account.account = new_session, new_account
                browser.account.sync_button()
                try:
                    wait_for(window, switch_read.is_set)
                    assert not browser.rows and browser.private_refreshing and browser.busy
                finally:
                    switch_reply.set()
                wait_for(window, lambda: browser.view == "library" and not browser.private_refreshing)
            assert private_cache.read(source, other_owner)["rows"] == [] and not browser.rows
        finally:
            library_reply.set()
            track_reply.set()
            app.close()
    assert b"cache-fixture-qq-key" not in cache_path.read_bytes()
    assert b"cache-fixture-netease-key" not in cache_path.read_bytes()
print("Private QQ and NetEase caches appeared before refresh, retained complete lists on partial results, kept selection, and cleared on expiry.")

for source, catalog, session, verified_account, owner, song_url in (
    ("qq", qq_catalog, QqSession("5678901", "batch-fixture-qq-key", "Batch account"),
     QqAccount("5678901", "Batch account"), "5678901", "https://y.qq.com/n/ryqq_v2/songDetail/00000000073001"),
    ("netease", netease_catalog, NeteaseSession(5678901, (("MUSIC_U", "batch-fixture-netease-key"),)),
     NeteaseAccount(5678901, "Batch account"), 5678901, "https://music.163.com/song?id=73001"),
):
    playlists = [
        {"id": f"directory:{number}" if source == "qq" else str(number),
         "dir_id": number, "tid": 0, "kind": "created", "title": "同名私密歌单", "total": 2,
         "url": "", "private": True, "_account_id": owner}
        for number in (53001, 53002)
    ]
    rows = [
        {"source": source, "id": "00000000073001" if source == "qq" else "73001",
         "title": "重复曲目", "artist": "Batch Artist", "album": "", "duration": 90,
         "url": song_url, "position": position}
        for position in (1, 2)
    ]
    library = {"source": source, "rows": playlists, "total": 2, "warnings": []}
    songs = {"source": source, "rows": rows, "total": 2, "title": "同名私密歌单", "url": "", "warning": ""}
    cache = SharedMusicCache(settings.DATA / "shared-music.sqlite3")
    for selected_playlist in playlists:
        assert cache.replace(source, owner, songs, playlist=selected_playlist)
    calls = []

    def read_batch(current_session, selected_playlist, *, stop=None):
        assert current_session == session
        calls.append(selected_playlist["id"])
        if selected_playlist["id"] == playlists[1]["id"]:
            raise RuntimeError("临时网络错误")
        return songs

    with ExitStack() as context:
        for provider, auth in (("qq", Studio.QqAuth), ("netease", Studio.NeteaseAuth)):
            context.enter_context(patch.object(auth, "load", return_value=session if provider == source else None))
            context.enter_context(patch.object(auth, "check", return_value=verified_account if provider == source else None))
        context.enter_context(patch.object(catalog, "list_playlists", return_value=library))
        context.enter_context(patch.object(catalog, "resolve_playlist", side_effect=read_batch))
        window, app = new_window()
        try:
            app.show(source)
            browser = app.shared_music_browsers[source]
            wait_for(window, lambda: browser.view == "library" and not browser.private_refreshing and len(browser.rows) == 2)
            browser.tree.selection_set(browser.tree.get_children())
            window.update()
            assert str(browser.queue_button.cget("state")) == "normal"
            before = {item.id for item in app.queue_store.list_tasks()}
            browser.queue_button.invoke()
            wait_for(window, lambda: not browser.batch_busy and len(set(app.tasks) - before) == 4)
            queued = [item for item in app.queue_store.list_tasks() if item.id not in before]
            keys = {item.batch_key for item in queued}
            assert len(keys) == 2 and all(sorted(item.position for item in queued if item.batch_key == key) == [1, 2] for key in keys)
            assert all(item.total == 2 and "batch-fixture" not in item.batch_key for item in queued)
            assert calls == [playlist["id"] for playlist in playlists]
            browser.queue_button.invoke()
            wait_for(window, lambda: not browser.batch_busy)
            assert len([item for item in app.queue_store.list_tasks() if item.id not in before]) == 4

            with patch.object(app, "start_download") as begin:
                browser.download_button.invoke()
                wait_for(window, lambda: not browser.batch_busy and begin.called)
                assert len(begin.call_args.args[0]) == 4

            app.show(source)
            browser.tree.selection_set(browser.tree.get_children()[0])
            browser._open_selected()
            wait_for(window, lambda: browser.view == "tracks" and not browser.private_refreshing)
            browser._enqueue(False, all_rows=True)
            assert len([item for item in app.queue_store.list_tasks() if item.id not in before]) == 4
            browser.account.account = None
            browser._enqueue(False, all_rows=True)
            assert len([item for item in app.queue_store.list_tasks() if item.id not in before]) == 4
            browser.library_button.invoke()
            wait_for(window, lambda: browser.view == "library" and not browser.private_refreshing)
            browser.tree.selection_set(browser.tree.get_children())
            window.update()
            with patch.object(catalog, "resolve_playlist", return_value=songs) as fresh, \
                 patch.object(app.shared_music_cache, "read", side_effect=AssertionError("Unverified cache read")), \
                 patch.object(app.shared_music_cache, "replace", side_effect=AssertionError("Unverified cache write")):
                browser.queue_button.invoke()
                wait_for(window, lambda: not browser.batch_busy)
                assert fresh.call_count == 2
            assert len([item for item in app.queue_store.list_tasks() if item.id not in before]) == 4
        finally:
            app.close()
print("Multi-selected private playlists expanded in order, retained duplicate positions, reused complete caches, and preserved stable task groups.")

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
