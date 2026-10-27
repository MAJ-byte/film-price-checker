@echo off
setlocal EnableExtensions
title Film Price Checker - Installer
cd /d "%~dp0"
echo.
echo  ===========================================================
echo     FILM PRICE CHECKER  -  installer
echo     DVD / Blu-ray / 4K price comparison for UK shops
echo  ===========================================================
echo.
echo  This sets up a private copy of Python packages inside this folder
echo  (in ".venv"), so it won't interfere with anything else on your PC.
echo.

rem ---- 1. find Python 3.9 or newer ------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if defined PY goto :have_python

echo  Python 3.9 or newer was not found on this PC.
echo.
where winget >nul 2>&1
if errorlevel 1 goto :manual_python
choice /c YN /m "  Install Python 3.12 now (free, from python.org via winget)"
if errorlevel 2 goto :manual_python
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
echo.
echo  Python is installed. Please CLOSE this window and double-click install.bat again.
pause
exit /b 0

:manual_python
echo.
echo  Please install Python from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH" on the first screen), then run install.bat again.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:have_python
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo  [1/4] Found Python %PYVER%

rem ---- 2. private environment -----------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo  [2/4] Using existing private environment
) else (
    echo  [2/4] Creating private environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
set "VPY=%~dp0.venv\Scripts\python.exe"

rem ---- 3. packages -----------------------------------------------------------
rem If Film Price Checker is running (e.g. it starts with Windows), its files are locked and
rem can't be updated - ask it to quit first, then make sure no copy from this folder is left.
powershell -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/quit -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null; Start-Sleep -Seconds 2 } catch {}"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*filmprice.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo  [3/4] Installing packages (a minute or two the first time)...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 goto :fail
rem optional extra: makes shops far less likely to block searches - carry on without it if it fails
"%VPY%" -m pip install "curl_cffi>=0.7" --quiet --disable-pip-version-check
if errorlevel 1 echo        (optional curl_cffi could not be installed - continuing without it)

rem The helper browser uses Microsoft Edge or Google Chrome if you have them (every
rem Windows 10/11 PC has Edge). Only download Playwright's own Chromium if neither works.
"%VPY%" -c "import sys; from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(channel='msedge'); b.close(); p.stop()" >nul 2>&1
if errorlevel 1 (
    echo        Edge not usable for the helper browser - downloading Chromium instead...
    "%VPY%" -m playwright install chromium
)

rem ---- 4. shortcuts ----------------------------------------------------------
echo  [4/4] Adding the desktop icon...
"%VPY%" filmprice.py --shortcut
echo.
choice /c YN /m "  Start quietly with Windows, so watchlist price alerts keep working"
if errorlevel 2 goto :no_startup
"%VPY%" filmprice.py --startup
:no_startup

echo.
echo  ===========================================================
echo   All done!  Use the "Film Price Checker" icon on your desktop.
echo   To remove it later, run uninstall.bat in this folder.
echo  ===========================================================
echo.
choice /c YN /m "  Open Film Price Checker now"
if errorlevel 2 exit /b 0
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0filmprice.py"
exit /b 0

:fail
echo.
echo  Something went wrong - see the messages above.
echo  If it says "Access is denied", Film Price Checker is still running:
echo  click QUIT on its page (or end "pythonw.exe" in Task Manager), then run install.bat again.
echo  Otherwise check your internet connection and run install.bat again.
pause
exit /b 1
