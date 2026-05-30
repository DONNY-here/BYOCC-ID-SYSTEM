@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0start_web_app.ps1"
pause
