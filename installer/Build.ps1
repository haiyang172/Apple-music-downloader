[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(\.\d+)?$')]
    [string]$Version = '0.3.2',
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
if (-not $PythonLib) { $PythonLib = Join-Path $workspace 'playlist-download\python-deps' }
if (-not $AudioTools) { $AudioTools = Join-Path $workspace 'wsl1\deps-win' }
$distribution = Join-Path (Join-Path $PSScriptRoot 'dist') $Version
$portable = Join-Path $distribution 'amdl'
$buildTools = Join-Path $PSScriptRoot '.tools'
$setup = Join-Path $distribution 'amdl-setup.exe'

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

function Get-WrapperLiteArchive([string]$Python, [string]$LoginSource) {
    $json = & $Python -I -B -c 'import json, runpy, sys; m = runpy.run_path(sys.argv[1]); print(json.dumps({k: m[k] for k in ("VERSION", "ASSET", "ASSET_URL", "ASSET_SIZE", "ASSET_SHA256")}))' $LoginSource
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

$sources = @('Studio.py', 'engine.py', 'audio.py', 'settings.py', 'storage.py', 'login_service.py',
    'music_catalog.py', 'music_library.py', 'music_browser.py', 'account_ui.py', 'round_theme.py')
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
    $python = Join-Path $runtime 'python.exe'

    $archive = Get-WrapperLiteArchive $python (Join-Path $stage 'login_service.py')
    $wrapperBundle = Join-Path $stage 'wrapper-lite'
    [void][IO.Directory]::CreateDirectory($wrapperBundle)
    Copy-Item -LiteralPath $archive.FullName -Destination (Join-Path $wrapperBundle $archive.Name)

    $lib = Join-Path $stage 'lib'
    [void][IO.Directory]::CreateDirectory($lib)
    foreach ($item in Get-ChildItem -LiteralPath $PythonLib -Directory) {
        if ($item.Name -match '^(?:yaml|_yaml|mutagen|mutagen-[^\\/]+\.dist-info|pyyaml-[^\\/]+\.dist-info)$') {
            Copy-SafeTree $item.FullName (Join-Path $lib $item.Name)
        }
    }
    foreach ($file in @('yaml\__init__.py', 'mutagen\__init__.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $lib $file) -PathType Leaf)) {
            throw "Python dependency missing: $file"
        }
    }
    & $python -I -B -c 'import ssl, sys, tkinter; sys.path.insert(0, sys.argv[1]); import Studio; Studio.configure_windows(); ssl.create_default_context(); tkinter.Tcl()' $stage
    if ($LASTEXITCODE -ne 0) { throw 'The packaged application could not initialize its TLS and Tcl/Tk runtime.' }

    $backend = Join-Path $stage 'backend'
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
    $application = Join-Path $stage 'amdl.exe'
    $launcher = Join-Path $PSScriptRoot 'Launcher.cs'
    $manifest = Join-Path $PSScriptRoot 'Launcher.manifest'
    $compile = @('/nologo', '/codepage:65001', '/target:winexe', '/optimize+', '/reference:System.Windows.Forms.dll',
        "/out:$application", "/win32manifest:$manifest", $launcher)
    $icon = Join-Path $stage 'assets\AMDLStudio.ico'
    if (Test-Path -LiteralPath $icon -PathType Leaf) { $compile += "/win32icon:$icon" }
    & $compiler @compile
    if ($LASTEXITCODE -ne 0) { throw 'The amdl launcher could not be compiled.' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Portable.cmd') -Destination (Join-Path $stage 'Run.cmd')

    & $InnoCompiler /Qp "/O$distribution" "/DAppVersion=$Version" "/DPayloadDir=$stage" (Join-Path $PSScriptRoot 'Setup.iss')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        throw 'The amdl installer could not be compiled.'
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
    Write-Output "Portable:  $(Join-Path $portable 'amdl.exe')"
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
