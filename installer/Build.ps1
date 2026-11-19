[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(\.\d+)?$')]
    [string]$Version,
    [string]$ApmyxHome = (Join-Path $env:LOCALAPPDATA 'Programs\apmyx'),
    [string]$PythonHome = (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python'),
    [string]$PythonLib,
    [string]$AudioTools,
    [string]$WrapperLiteArchive,
    [string]$InnoCompiler
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$studio = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $studio
if (-not $Version) { $Version = (Get-Content -LiteralPath (Join-Path $studio 'app-version.txt') -Raw).Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { throw 'Invalid application version.' }
if (-not $PythonLib) { $PythonLib = Join-Path $workspace 'playlist-download\python-deps' }
if (-not $AudioTools) { $AudioTools = Join-Path $workspace 'wsl1\deps-win' }
$distribution = Join-Path (Join-Path $PSScriptRoot 'dist') $Version
$portable = Join-Path $distribution 'amdl'
$buildTools = Join-Path $PSScriptRoot '.tools'
$setup = Join-Path $distribution 'MediaDownloadDesk-Setup.exe'

function Assert-LocalDirectory([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Expected an ordinary directory: $Path"
    }
}

function Copy-SafeTree([string]$From, [string]$To) {
    Assert-LocalDirectory $From
    [void][IO.Directory]::CreateDirectory($To)
    foreach ($item in Get-ChildItem -LiteralPath $From -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            $item.Name -match '(?i)^(?:\..+|__pycache__|logs?|downloads?|config(?:uration)?\.ya?ml(?:\..*)?|settings\.json|cookies?(?:\..*)?|credentials?(?:\..*)?)$' -or
            $item.Extension -match '(?i)^\.(?:key|pfx|p12|log|bak|pyc|m4a|mp3|flac|wav|opus|ogg)$') {
            continue
        }
        $target = Join-Path $To $item.Name
        if ($item.PSIsContainer) { Copy-SafeTree $item.FullName $target }
        else { Copy-Item -LiteralPath $item.FullName -Destination $target }
    }
}

function Get-InnoCompiler {
    $compiler = Join-Path $buildTools 'inno\ISCC.exe'
    if (Test-Path -LiteralPath $compiler -PathType Leaf) { return $compiler }
    $source = Join-Path $buildTools 'innosetup-7.1.1-x64.exe'
    $expected = '8A5A6711C3766A6E5FB74F280BA0E3551E719CE94538D901FDBD1F8F9920B3F0'
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        $url = 'https://github.com/jrsoftware/issrc/releases/download/is-7_1_1/innosetup-7.1.1-x64.exe'
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $source
    }
    $sig = Get-AuthenticodeSignature -LiteralPath $source
    $kind = [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne $expected -or
        $sig.Status -ne 'Valid' -or $sig.SignerCertificate.GetNameInfo($kind, $false) -ne 'Pyrsys B.V.') {
        throw 'Inno Setup failed the pinned hash or publisher check.'
    }
    $target = Join-Path $buildTools 'inno'
    $arguments = @('/PORTABLE=1', '/CURRENTUSER', '/VERYSILENT', '/NORESTART', '/SP-', '/SUPPRESSMSGBOXES',
        '/NOICONS', ('/DIR="' + $target + '"'))
    $child = Start-Process -FilePath $source -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($child.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw "Could not prepare the portable Inno Setup compiler (exit $($child.ExitCode))."
    }
    return $compiler
}

function Test-PinnedArchive([string]$Path, [long]$Length, [string]$Hash) {
    $file = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    return ($null -ne $file -and -not $file.PSIsContainer -and
        -not ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $file.Length -eq $Length -and
        (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -eq $Hash)
}

function Copy-PinnedWebFile([string]$Name, [string]$Url, [long]$Length, [string]$Hash, [string]$Destination) {
    $cache = Join-Path $buildTools $Name
    if (-not (Test-PinnedArchive $cache $Length $Hash)) {
        $partial = Join-Path $buildTools "$Name.$([Guid]::NewGuid().ToString('N')).partial"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $partial
            if (-not (Test-PinnedArchive $partial $Length $Hash)) {
                throw "The downloaded $Name does not match the pinned SHA-256 and size."
            }
            Move-Item -LiteralPath $partial -Destination $cache -Force
        } finally {
            if (Test-Path -LiteralPath $partial -PathType Leaf) { Remove-Item -LiteralPath $partial }
        }
    }
    Copy-Item -LiteralPath $cache -Destination $Destination
}

function Install-WebWorker([string]$Backend) {
    $web = Join-Path $Backend 'web'
    [void][IO.Directory]::CreateDirectory($web)
    Copy-PinnedWebFile 'yt-dlp-2026.10.26.pyz' 'https://github.com/yt-dlp/yt-dlp/releases/download/2026.10.26/yt-dlp' `
        3073072 'e51e69e89489f199530f13d32a4342da3ddb2cf34842f364603a066db05afbf3' (Join-Path $web 'yt-dlp.pyz')
    Copy-PinnedWebFile 'qjs-v0.18.0-windows-x86_64.exe' 'https://github.com/quickjs-ng/quickjs/releases/download/v0.18.0/qjs-windows-x86_64.exe' `
        1873152 'ff03398d74ea6e6bd3a8e58058f7003225abcac229d7d57775b7aaae812a49c7' (Join-Path $web 'qjs.exe')
    Copy-PinnedWebFile 'yt-dlp-2026.10.26-LICENSE.txt' 'https://raw.githubusercontent.com/yt-dlp/yt-dlp/2026.10.26/LICENSE' `
        1211 '7e12e5df4bae12cb21581ba157ced20e1986a0508dd10d0e8a4ab9a4cf94e85c' (Join-Path $web 'yt-dlp-LICENSE.txt')
    Copy-PinnedWebFile 'quickjs-ng-v0.18.0-LICENSE.txt' 'https://raw.githubusercontent.com/quickjs-ng/quickjs/v0.18.0/LICENSE' `
        1212 '96f73f9d2a16c21a36b418f06073be26e7d6d5e7c1bc99756b21a4f2c74ef171' (Join-Path $web 'quickjs-ng-LICENSE.txt')
    Copy-PinnedWebFile 'yt-dlp-ejs-0.8.0-LICENSE.txt' 'https://raw.githubusercontent.com/yt-dlp/ejs/0.8.0/LICENSE' `
        1212 'b5065838cbac452dfc855ba6e6e031481ad2c68406f70d21ead9321374653e6c' (Join-Path $web 'yt-dlp-ejs-LICENSE.txt')
}

function Install-QrEncoder([string]$Python, [string]$Library) {
    $name = 'qrcodegen-1.8.0-py3-none-any.whl'
    $hash = '06bf87021bc351f1c6e722ac67db8a059c4fe27bbfbca39ed7a9a2364abdd7a6'
    $length = 14443
    $cache = Join-Path $buildTools $name
    if (-not (Test-PinnedArchive $cache $length $hash)) {
        $partial = Join-Path $buildTools "$name.$([Guid]::NewGuid().ToString('N')).partial"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "https://files.pythonhosted.org/packages/10/2b/fb03f5a840469f627d71690a95e8a53e3ea53ef6d752c93f34a5a65ee061/$name" -OutFile $partial
            if (-not (Test-PinnedArchive $partial $length $hash)) {
                throw 'The QR encoder did not match its pinned wheel.'
            }
            Move-Item -LiteralPath $partial -Destination $cache -Force
        } finally {
            if (Test-Path -LiteralPath $partial -PathType Leaf) { Remove-Item -LiteralPath $partial }
        }
    }
    & $Python -I -B -c 'import pathlib, sys, zipfile; package = zipfile.ZipFile(sys.argv[1]); (pathlib.Path(sys.argv[2]) / sys.argv[3]).write_bytes(package.read(sys.argv[3]))' $cache $Library qrcodegen.py
    if ($LASTEXITCODE -ne 0) { throw 'Could not stage the pinned QR encoder.' }
}

function Install-AppleWebLogin([string]$Destination) {
    [void][IO.Directory]::CreateDirectory($Destination)
    $sdkName = 'microsoft.web.webview2.1.0.4314.35.nupkg'
    $sdkUrl = 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4314.35/microsoft.web.webview2.1.0.4314.35.nupkg'
    $sdkHash = '90778deb1b77e36292828b4b5833c9255c27533f40e0ddd55891ae718944edc4'
    Copy-PinnedWebFile $sdkName $sdkUrl 9818548 $sdkHash (Join-Path $Destination 'sdk.nupkg')
    $sdk = Join-Path $buildTools $sdkName
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($sdk)
    try {
        $entries = @{
            'lib/net462/Microsoft.Web.WebView2.Core.dll' = 'Microsoft.Web.WebView2.Core.dll'
            'lib/net462/Microsoft.Web.WebView2.WinForms.dll' = 'Microsoft.Web.WebView2.WinForms.dll'
            'runtimes/win-x64/native/WebView2Loader.dll' = 'WebView2Loader.dll'
            'LICENSE.txt' = 'WebView2-LICENSE.txt'
            'NOTICE.txt' = 'WebView2-NOTICE.txt'
        }
        foreach ($name in $entries.Keys) {
            $entry = $archive.GetEntry($name)
            if ($null -eq $entry) { throw "Missing WebView2 package entry: $name" }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $Destination $entries[$name]), $true)
        }
    } finally {
        $archive.Dispose()
        Remove-Item -LiteralPath (Join-Path $Destination 'sdk.nupkg')
    }
    $runtimeUrl = 'https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/52cfd0a8-f256-48d4-9b18-467f2b3cda78/MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
    $runtimeHash = 'd3cbe8fedb104282d745fff106993efead51ae94f5595a4e75804446a974f9c3'
    $runtimeFile = Join-Path $Destination 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
    Copy-PinnedWebFile 'webview2-runtime-x64-d3cbe8fe.exe' $runtimeUrl 215116512 $runtimeHash $runtimeFile
    $signature = Get-AuthenticodeSignature -LiteralPath $runtimeFile
    $nameType = [Security.Cryptography.X509Certificates.X509NameType]::SimpleName
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.GetNameInfo($nameType, $false) -ne 'Microsoft Corporation') {
        throw 'The WebView2 runtime installer does not have a valid Microsoft signature.'
    }
    $compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    $compile = @('/nologo', '/codepage:65001', '/target:winexe', '/platform:x64', '/optimize+',
        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
        "/reference:$(Join-Path $Destination 'Microsoft.Web.WebView2.Core.dll')",
        "/reference:$(Join-Path $Destination 'Microsoft.Web.WebView2.WinForms.dll')",
        "/win32manifest:$(Join-Path $PSScriptRoot 'Launcher.manifest')",
        "/out:$(Join-Path $Destination 'AppleMusicLogin.exe')", (Join-Path $PSScriptRoot 'AppleMusicLogin.cs'))
    $icon = Join-Path (Split-Path -Parent $Destination) 'assets\AMDLStudio.ico'
    if (Test-Path -LiteralPath $icon -PathType Leaf) { $compile += "/win32icon:$icon" }
    & $compiler @compile
    if ($LASTEXITCODE -ne 0) { throw 'The Apple Music web login helper could not be compiled.' }
}

function Get-WrapperLiteArchive([string]$Python, [string]$LoginSource) {
    $json = & $Python -I -B -c 'import json, runpy, sys; m = runpy.run_path(sys.argv[1]); print(json.dumps({k: m[k] for k in sys.argv[2:]}))' $LoginSource VERSION ASSET ASSET_URL ASSET_SIZE ASSET_SHA256
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the pinned Wrapper Lite release.' }
    $pin = $json | ConvertFrom-Json
    $name = "wrapper-$($pin.VERSION)-windows-x86_64.zip"
    $url = "https://github.com/itouakirai/wrapper/releases/download/$($pin.VERSION)/$name"
    if ($pin.VERSION -notmatch '^v\d+\.\d+\.\d+$' -or $pin.ASSET -cne $name -or $pin.ASSET_URL -cne $url -or
        $pin.ASSET_SIZE -le 0 -or $pin.ASSET_SHA256 -notmatch '^[a-f0-9]{64}$') {
        throw 'The Wrapper Lite release pin is invalid.'
    }
    if ($WrapperLiteArchive) {
        if (-not (Test-PinnedArchive $WrapperLiteArchive $pin.ASSET_SIZE $pin.ASSET_SHA256)) {
            throw 'Wrapper Lite archive does not match the pinned official release.'
        }
        return [pscustomobject]@{ Name = $name; FullName = (Get-Item -LiteralPath $WrapperLiteArchive).FullName }
    }

    $cached = Join-Path $buildTools $name
    if (Test-PinnedArchive $cached $pin.ASSET_SIZE $pin.ASSET_SHA256) {
        return [pscustomobject]@{ Name = $name; FullName = $cached }
    }
    if ((Test-Path -LiteralPath $cached) -and -not (Test-Path -LiteralPath $cached -PathType Leaf)) {
        throw "Expected a file at the Wrapper Lite cache path: $cached"
    }
    $partial = Join-Path $buildTools "$name.$([Guid]::NewGuid().ToString('N')).partial"
    try {
        $localArchive = Join-Path ([IO.Path]::GetTempPath()) "AMDL-wrapper-$($pin.VERSION)-auth-smoke\wrapper.zip"
        if (Test-PinnedArchive $localArchive $pin.ASSET_SIZE $pin.ASSET_SHA256) {
            Copy-Item -LiteralPath $localArchive -Destination $partial
        } else {
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $partial
        }
        if (-not (Test-PinnedArchive $partial $pin.ASSET_SIZE $pin.ASSET_SHA256)) {
            throw 'The downloaded Wrapper Lite archive failed the pinned size or SHA-256 check.'
        }
        Move-Item -LiteralPath $partial -Destination $cached -Force
        return [pscustomobject]@{ Name = $name; FullName = $cached }
    } finally {
        if (Test-Path -LiteralPath $partial -PathType Leaf) { Remove-Item -LiteralPath $partial }
    }
}

$sources = @('Studio.py', 'app_update.py', 'apple_download.py', 'apple_web_login.py', 'download_queue.py', 'engine.py', 'audio.py', 'settings.py', 'storage.py', 'login_service.py', 'library_cache.py',
    'music_catalog.py', 'music_library.py', 'music_browser.py', 'music_preflight_ui.py', 'preview_layout.py', 'account_ui.py', 'round_theme.py',
    'bili_auth.py', 'qr_account_ui.py', 'bili_catalog.py', 'bili_collections.py', 'bili_favorites_cache.py',
    'bili_download.py', 'bili_resume.py',
    'bili_sidecars.py', 'bili_ui.py', 'bili_preflight_ui.py',
    'qq_auth.py', 'qq_catalog.py', 'qq_download.py', 'qq_sidecars.py', 'share_links.py', 'short_music_links.py',
    'shared_music.py', 'shared_music_cache.py', 'shared_music_ui.py',
    'local_convert.py', 'local_convert_ui.py', 'windows_drop.py', 'video_links_ui.py',
    'youtube_download.py', 'youtube_browse.py', 'youtube_browse_worker.py',
    'netease_auth.py', 'netease_catalog.py', 'netease_download.py', 'netease_sidecars.py',
    'music_tagging.py', 'web_audio.py', 'playlist_export.py', 'tag_edit.py', 'tag_edit_ui.py',
    'StudioQt.py')
foreach ($file in $sources) { Get-Item -LiteralPath (Join-Path $studio $file) | Out-Null }
foreach ($path in @($distribution, $buildTools)) {
    [void][IO.Directory]::CreateDirectory($path)
    Assert-LocalDirectory $path
}
if (-not $InnoCompiler) { $InnoCompiler = Get-InnoCompiler }
if (-not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) { throw 'ISCC.exe was not found.' }

$stage = Join-Path $PSScriptRoot ('.stage-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($stage)
try {
    foreach ($file in $sources) { Copy-Item -LiteralPath (Join-Path $studio $file) -Destination $stage }
    $qtui = Join-Path $stage 'qtui'
    [void][IO.Directory]::CreateDirectory($qtui)
    foreach ($file in @('__init__.py', 'main_window.py', 'pages.py', 'theme.py')) {
        Copy-Item -LiteralPath (Join-Path $studio ('qtui\' + $file)) -Destination $qtui
    }
    Set-Content -LiteralPath (Join-Path $stage 'app-version.txt') -Value $Version -Encoding ASCII
    foreach ($file in @('README.md', 'THIRD_PARTY.md', 'LICENSE', 'LICENSE.txt')) {
        $source = Join-Path $studio $file
        if (Test-Path -LiteralPath $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination $stage }
    }
    $assets = Join-Path $studio 'assets'
    if (Test-Path -LiteralPath $assets -PathType Container) { Copy-SafeTree $assets (Join-Path $stage 'assets') }
    $runtime = Join-Path $stage 'runtime'
    [void][IO.Directory]::CreateDirectory($runtime)
    foreach ($file in @('python.exe', 'pythonw.exe', 'python3.dll', 'python312.dll', 'vcruntime140.dll',
        'vcruntime140_1.dll', 'LICENSE.txt')) {
        Copy-Item -LiteralPath (Join-Path $PythonHome $file) -Destination $runtime
    }
    foreach ($name in @('DLLs', 'tcl')) {
        Copy-SafeTree (Join-Path $PythonHome $name) (Join-Path $runtime $name)
    }
    $standardLibrary = Join-Path $PythonHome 'Lib'
    $runtimeLib = Join-Path $runtime 'Lib'
    [void][IO.Directory]::CreateDirectory($runtimeLib)
    foreach ($item in Get-ChildItem -LiteralPath $standardLibrary) {
        if ($item.PSIsContainer) {
            if ($item.Name -notin @('site-packages', '__pycache__', 'test', 'idlelib', 'ensurepip', 'tkinter_tests')) {
                Copy-SafeTree $item.FullName (Join-Path $runtimeLib $item.Name)
            }
        } elseif ($item.Extension -eq '.py' -or $item.Name -eq 'codex-rejected-cas.pem') {
            Copy-Item -LiteralPath $item.FullName -Destination $runtimeLib
        }
    }
    # Copy the standard-library parser explicitly. The generic file filter skips cookie files.
    Copy-Item -LiteralPath (Join-Path $standardLibrary 'http\cookies.py') -Destination (Join-Path $runtimeLib 'http\cookies.py')
    $python = Join-Path $runtime 'python.exe'

    $archive = Get-WrapperLiteArchive $python (Join-Path $stage 'login_service.py')
    $wrapperBundle = Join-Path $stage 'wrapper-lite'
    [void][IO.Directory]::CreateDirectory($wrapperBundle)
    Copy-Item -LiteralPath $archive.FullName -Destination (Join-Path $wrapperBundle $archive.Name)

    $lib = Join-Path $stage 'lib'
    [void][IO.Directory]::CreateDirectory($lib)
    foreach ($item in Get-ChildItem -LiteralPath $PythonLib -Directory) {
        if ($item.Name -match '^(?:yaml|_yaml|mutagen|mutagen-[^\\/]+\.dist-info|pyyaml-[^\\/]+\.dist-info|PySide6|shiboken6|PySide6_Essentials-[^\\/]+\.dist-info|shiboken6-[^\\/]+\.dist-info)$') {
            Copy-SafeTree $item.FullName (Join-Path $lib $item.Name)
        }
    }
    foreach ($file in @('yaml\__init__.py', 'mutagen\__init__.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $lib $file) -PathType Leaf)) {
            throw "Python dependency missing: $file"
        }
    }
    Install-QrEncoder $python $lib
    Install-AppleWebLogin (Join-Path $stage 'apple-web-login')
    & $python -I -B -c 'import sqlite3, ssl, sys, tkinter; sys.path.insert(0, sys.argv[1]); import Studio; Studio.configure_windows(); sqlite3.connect(sys.argv[2]).close(); ssl.create_default_context(); tkinter.Tcl()' $stage ':memory:'
    if ($LASTEXITCODE -ne 0) { throw 'The packaged application could not initialize SQLite, TLS, or Tcl/Tk.' }

    $backend = Join-Path $stage 'backend'
    Install-WebWorker $backend
    $relativeBridge = '_internal\backend\bin\bridge'
    Copy-SafeTree (Join-Path $ApmyxHome $relativeBridge) (Join-Path $backend $relativeBridge)
    $apmyxLicenses = Join-Path $backend 'licenses'
    Copy-SafeTree (Join-Path $ApmyxHome 'licenses') $apmyxLicenses
    Copy-Item -LiteralPath (Join-Path $ApmyxHome 'LICENSE.txt') -Destination (Join-Path $apmyxLicenses 'apmyx-LICENSE.txt')
    $media = Join-Path $backend 'tools'
    [void][IO.Directory]::CreateDirectory($media)
    Copy-Item -LiteralPath (Join-Path $ApmyxHome 'tools\ffmpeg.exe') -Destination $media
    foreach ($file in @('vcruntime140.dll', 'vcruntime140_1.dll')) {
        Copy-Item -LiteralPath (Join-Path $runtime $file) -Destination $media
    }
    foreach ($file in @('mp4box.exe', 'mp4decrypt.exe', 'libgpac.dll', 'libcurl-x64.dll', 'libcryptoMD.dll',
        'libsslMD.dll', 'OpenSVCDecoder.dll', 'avcodec-60.dll', 'avdevice-60.dll', 'avfilter-9.dll',
        'avformat-60.dll', 'avutil-58.dll', 'postproc-57.dll', 'swresample-4.dll', 'swscale-7.dll')) {
        Copy-Item -LiteralPath (Join-Path $AudioTools $file) -Destination $media
    }

    $compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    $application = Join-Path $stage 'MediaDownloadDesk.exe'
    $launcher = Join-Path $PSScriptRoot 'Launcher.cs'
    $manifest = Join-Path $PSScriptRoot 'Launcher.manifest'
    $versionSource = Join-Path $stage 'AssemblyVersion.cs'
    Set-Content -LiteralPath $versionSource -Encoding ASCII -Value @(
        ('[assembly: System.Reflection.AssemblyFileVersion("{0}")]' -f $Version),
        ('[assembly: System.Reflection.AssemblyInformationalVersion("{0}")]' -f $Version)
    )
    $compile = @('/nologo', '/codepage:65001', '/target:winexe', '/optimize+', '/reference:System.Windows.Forms.dll',
        "/out:$application", "/win32manifest:$manifest", $launcher, $versionSource)
    $icon = Join-Path $stage 'assets\AMDLStudio.ico'
    if (Test-Path -LiteralPath $icon -PathType Leaf) { $compile += "/win32icon:$icon" }
    & $compiler @compile
    if ($LASTEXITCODE -ne 0) { throw 'The Media Download Desk launcher could not be compiled.' }
    Remove-Item -LiteralPath $versionSource
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Portable.cmd') -Destination (Join-Path $stage 'Run.cmd')
    if (Test-Path -LiteralPath (Join-Path $studio 'RunQt.cmd') -PathType Leaf) {
        Copy-Item -LiteralPath (Join-Path $studio 'RunQt.cmd') -Destination (Join-Path $stage 'RunQt.cmd')
    }

    & $InnoCompiler /Qp "/O$distribution" "/DAppVersion=$Version" "/DPayloadDir=$stage" (Join-Path $PSScriptRoot 'Setup.iss')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        throw 'The Media Download Desk installer could not be compiled.'
    }
    if (Test-Path -LiteralPath $portable) {
        $resolved = (Resolve-Path -LiteralPath $portable).Path
        $expected = Join-Path ((Resolve-Path -LiteralPath $distribution).Path) 'amdl'
        if ($resolved -ne $expected) { throw 'Portable directory is outside the build output.' }
        Assert-LocalDirectory $portable
        Remove-Item -LiteralPath $portable -Recurse -Force
    }
    Move-Item -LiteralPath $stage -Destination $portable
    Write-Output "Installer: $setup"
    Write-Output "Portable:  $(Join-Path $portable 'MediaDownloadDesk.exe')"
}
finally {
    if (Test-Path -LiteralPath $stage -PathType Container) {
        $resolved = (Resolve-Path -LiteralPath $stage).Path
        $expected = Join-Path ((Resolve-Path -LiteralPath $PSScriptRoot).Path) (Split-Path -Leaf $stage)
        if ($resolved -eq $expected) {
            Assert-LocalDirectory $stage
            Remove-Item -LiteralPath $stage -Recurse -Force
        }
    }
}
