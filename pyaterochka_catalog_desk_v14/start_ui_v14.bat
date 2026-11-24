@echo off
setlocal
chcp 65001>nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found in PATH.
  echo Install Python 3.11 or add python.exe to PATH.
  pause
  exit /b 1
)
python catalog_ui_server_v14.py --port 8767
if errorlevel 1 (
  echo.
  echo Catalog Desk V14 stopped with an error.
)
pause
endlocal
