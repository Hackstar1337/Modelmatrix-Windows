@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
exit /b %errorlevel%
