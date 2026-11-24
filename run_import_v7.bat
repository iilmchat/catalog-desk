@echo off
chcp 65001 >nul
cd /d "%~dp0"
python import_5ka_exports_v7.py %*
echo.
pause
