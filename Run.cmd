@echo off
setlocal
set "PYTHONDONTWRITEBYTECODE=1"
set "AMDL_APP=%~dp0Studio.py"
set "AMDL_PY=%~dp0runtime\pythonw.exe"
if not exist "%AMDL_PY%" set "AMDL_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe"
if exist "%AMDL_PY%" (
    start "" "%AMDL_PY%" "%AMDL_APP%"
    exit /b
)
py -3 "%AMDL_APP%"
if errorlevel 1 pause
