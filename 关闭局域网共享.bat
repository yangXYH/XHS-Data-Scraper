@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动下载页面.ps1" -LocalOnly
if errorlevel 1 pause
