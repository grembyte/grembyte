@echo off
rem Start Hermes Hook from the project's virtual environment, e.g. from a
rem shortcut. Works from any working directory. Uses pythonw.exe, so no
rem console window stays open.
setlocal

for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
if defined HERMES_VENV (set "VENV_DIR=%HERMES_VENV%") else (set "VENV_DIR=%PROJECT_DIR%\.venv")

if not exist "%VENV_DIR%\Scripts\pythonw.exe" (
    echo No virtual environment at %VENV_DIR% ^(see readme.md, Installation^). 1>&2
    exit /b 1
)

cd /d "%PROJECT_DIR%"
start "" "%VENV_DIR%\Scripts\pythonw.exe" main.py %*
