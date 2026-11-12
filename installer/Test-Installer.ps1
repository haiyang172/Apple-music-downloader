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
        foreach ($name in @('amdl.lnk', 'AMDL Studio.lnk')) {
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

    foreach ($name in $environment.Keys) {
        $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
    }

    $smokeCode = @'
from pathlib import Path
import os
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk

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

Studio.configure_windows()
window = tk.Tk()
window.withdraw()
app = Studio.Studio(window)
try:
    assert app.profile_text.get() == "登录" and app.brand_icon.width() >= 32
    assert int(app.profile_button.grid_info()["row"]) > int(app.navigation["components"].grid_info()["row"])
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
