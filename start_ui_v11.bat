@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    where py >nul 2>&1
    if errorlevel 1 (
        echo Python was not found. Install Python 3.11 or later and enable Add Python to PATH.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

echo Starting Pyaterochka Catalog Desk V11.1...
%PYTHON_CMD% "%~dp0catalog_ui_server_v11.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo The server stopped with an error. See the message above.
    pause
)
exit /b %RC%
