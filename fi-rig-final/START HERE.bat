@echo off
title FI Rig - Fault Injection Rig
color 0B

echo.
echo  Starting FI Rig...
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo.
    echo  Install Python 3.9+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: Run the launcher
python "%~dp0FI-Rig-Launch.py"

:: If we get here, it exited - pause so user can read any error
if errorlevel 1 (
    echo.
    echo  Something went wrong. See error above.
    pause
)
