@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Pyaterochka Catalog Desk V10...
python catalog_ui_server_v10.py
pause
