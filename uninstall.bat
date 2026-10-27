@echo off
setlocal
title Film Price Checker - Uninstall
cd /d "%~dp0"
echo.
echo  This removes the desktop icon, the start-with-Windows entry and the
echo  installed packages. Your watchlist is kept unless you choose otherwise.
echo.
choice /c YN /m "  Continue"
if errorlevel 2 exit /b 0

rem stop it if it's running
powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/quit -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null } catch {}"

powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); Remove-Item -ErrorAction SilentlyContinue (Join-Path $d 'Film Price Checker.lnk'); $s=[Environment]::GetFolderPath('Startup'); Remove-Item -ErrorAction SilentlyContinue (Join-Path $s 'Film Price Checker (watchlist).lnk')"
echo  - shortcuts removed

timeout /t 2 /nobreak >nul
if exist ".venv" rmdir /s /q ".venv"
for /d %%p in (browser_profile*) do rmdir /s /q "%%p"
if exist "debug" rmdir /s /q "debug"
del /q filmprice.log filmprice.ico selftest.txt 2>nul
echo  - packages and helper-browser data removed

if exist watchlist.json (
    choice /c YN /m "  Also delete your watchlist"
    if not errorlevel 2 del /q watchlist.json
)
echo.
echo  Uninstalled. You can now delete this folder.
pause
