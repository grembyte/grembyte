@echo off
rem Start Grembyte from the project's virtual environment, e.g. from a
rem shortcut. Works from any working directory. Uses pythonw.exe, so no
rem console window stays open.
setlocal

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
if defined GREMBYTE_VENV (set "VENV_DIR=%GREMBYTE_VENV%") else (set "VENV_DIR=%PROJECT_DIR%\.venv")

if not exist "%VENV_DIR%\Scripts\pythonw.exe" (
    echo No virtual environment at %VENV_DIR% ^(see docs\getting-started\installation.md^). 1>&2
    exit /b 1
)

cd /d "%PROJECT_DIR%"
start "" "%VENV_DIR%\Scripts\pythonw.exe" -m grembyte %*
