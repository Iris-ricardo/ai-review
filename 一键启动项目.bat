@echo off
setlocal
cd /d "%~dp0"

if /I "%~1"=="--no-browser" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_project.ps1" -NoBrowser
) else (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_project.ps1"
)

if errorlevel 1 (
    echo.
    echo Startup failed. See the message above.
    pause
    exit /b 1
)

exit /b 0
