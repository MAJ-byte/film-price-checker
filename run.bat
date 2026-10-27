@echo off
rem Starts Film Price Checker with a console window showing what it's doing (handy for
rem troubleshooting). Extra options pass straight through, e.g.  run.bat --selftest
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Not installed yet - double-click install.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" filmprice.py %*
pause
