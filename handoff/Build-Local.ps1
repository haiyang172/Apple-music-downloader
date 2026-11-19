[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $root 'installer'
$build = @{
    PythonHome = Join-Path $root 'runtime'
    PythonLib = Join-Path $root 'lib'
    ApmyxHome = Join-Path $root 'backend'
    AudioTools = Join-Path $root 'backend\tools'
    WrapperLiteArchive = Join-Path $root 'wrapper-lite\wrapper-v3.0.4-windows-x86_64.zip'
    InnoCompiler = Join-Path $installer '.tools\inno\ISCC.exe'
}
$inputs = @(
    'runtime\python.exe', 'runtime\python312.dll', 'runtime\Lib\http\cookies.py',
    'lib\mutagen\__init__.py', 'lib\yaml\__init__.py', 'lib\qrcodegen.py',
    'backend\_internal\backend\bin\bridge\bridge.exe', 'backend\LICENSE.txt',
    'backend\tools\ffmpeg.exe', 'backend\tools\mp4box.exe', 'backend\tools\mp4decrypt.exe',
    'apple-web-login\AppleMusicLogin.exe',
    'installer\.tools\webview2-runtime-x64-d3cbe8fe.exe',
    'installer\.tools\microsoft.web.webview2.1.0.4314.35.nupkg',
    'installer\.tools\qrcodegen-1.8.0-py3-none-any.whl',
    'installer\.tools\yt-dlp-2026.10.26.pyz', 'installer\.tools\qjs-v0.18.0-windows-x86_64.exe'
)
$files = @($inputs | ForEach-Object { Join-Path $root $_ })
$files += $build.WrapperLiteArchive, $build.InnoCompiler, (Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe')
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing build input: $file" }
}
if ($CheckOnly) {
    & (Join-Path $build.PythonHome 'python.exe') -I -B -c 'import pathlib,sys; root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root));import Studio,tag_edit,tag_edit_ui,mutagen,yaml,tkinter,PySide6.QtWidgets,StudioQt;assert Studio.ROOT==root and Studio.find_backend()==root.joinpath(sys.argv[2]) and pathlib.Path(mutagen.__file__).is_relative_to(root.joinpath(sys.argv[3]));print(sys.argv[1], Studio.APP_VERSION)' $root 'backend' 'lib'
    if ($LASTEXITCODE -ne 0) { throw 'The local runtime could not import the working copy.' }
    return
}
& (Join-Path $installer 'Build.ps1') @build
