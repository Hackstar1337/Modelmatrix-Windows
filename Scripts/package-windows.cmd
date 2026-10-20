@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0package-windows.ps1" %*
exit /b %errorlevel%
