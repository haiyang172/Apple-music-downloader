[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(\.\d+)?$')]
    [string]$Version = '0.1.0',
    [string]$ApmyxHome = (Join-Path $env:LOCALAPPDATA 'Programs\apmyx'),
    [string]$PythonHome = (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python'),
    [string]$PythonLib,
    [string]$AudioTools,
    [string]$InnoCompiler
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$studio = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $studio
if (-not $PythonLib) { $PythonLib = Join-Path $workspace 'playlist-download\python-deps' }
if (-not $AudioTools) { $AudioTools = Join-Path $workspace 'wsl1\deps-win' }
$distribution = Join-Path $PSScriptRoot 'dist'
$portable = Join-Path $distribution 'AMDL-Studio'
$buildTools = Join-Path $PSScriptRoot '.tools'
$setup = Join-Path $distribution 'AMDL-Studio-Setup.exe'

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

$sources = @('Studio.py', 'engine.py', 'audio.py', 'settings.py', 'storage.py')
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
        } elseif ($item.Extension -eq '.py') {
            Copy-Item -LiteralPath $item.FullName -Destination $runtimeLib
        }
    }

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

    $wslLauncher = Join-Path $workspace 'wsl1\LxRunOffline.exe'
    if (Test-Path -LiteralPath $wslLauncher -PathType Leaf) {
        $wsl = Join-Path $stage 'wsl1'
        [void][IO.Directory]::CreateDirectory($wsl)
        Copy-Item -LiteralPath $wslLauncher -Destination $wsl
        $license = Join-Path $buildTools 'LxRunOffline-LICENSE.txt'
        if (-not (Test-Path -LiteralPath $license -PathType Leaf)) {
            $url = 'https://raw.githubusercontent.com/DDoSolitary/LxRunOffline/bdc6d7d77f886c6dcdab3b4c9c136557ca6694c4/LICENSE'
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $license
        }
        Copy-Item -LiteralPath $license -Destination $wsl
    }

    $compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    $application = Join-Path $stage 'AMDLStudio.exe'
    $launcher = Join-Path $PSScriptRoot 'Launcher.cs'
    $compile = @('/nologo', '/codepage:65001', '/target:winexe', '/optimize+', '/reference:System.Windows.Forms.dll',
        "/out:$application", $launcher)
    $icon = Join-Path $stage 'assets\AMDLStudio.ico'
    if (Test-Path -LiteralPath $icon -PathType Leaf) { $compile += "/win32icon:$icon" }
    & $compiler @compile
    if ($LASTEXITCODE -ne 0) { throw 'The AMDL Studio launcher could not be compiled.' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Portable.cmd') -Destination (Join-Path $stage 'Run.cmd')

    & $InnoCompiler /Qp "/DAppVersion=$Version" "/DPayloadDir=$stage" (Join-Path $PSScriptRoot 'Setup.iss')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        throw 'The AMDL Studio installer could not be compiled.'
    }
    if (Test-Path -LiteralPath $portable) {
        $resolved = (Resolve-Path -LiteralPath $portable).Path
        $expected = Join-Path ((Resolve-Path -LiteralPath $distribution).Path) 'AMDL-Studio'
        if ($resolved -ne $expected) { throw 'Portable directory is outside the build output.' }
        Assert-LocalDirectory $portable
        Remove-Item -LiteralPath $portable -Recurse -Force
    }
    Move-Item -LiteralPath $stage -Destination $portable
    Write-Output "Installer: $setup"
    Write-Output "Portable:  $(Join-Path $portable 'AMDLStudio.exe')"
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
