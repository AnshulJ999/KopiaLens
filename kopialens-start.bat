@echo off
REM Console dev launcher for KopiaLens
REM Plain HTTP like the desktop app: a server-profile start would otherwise serve HTTPS
set KOPIALENS_HTTPS=off
python "%~dp0main.py"
pause
