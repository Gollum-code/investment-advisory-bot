@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---------------------------------------------------------------
rem  Investment Advisory Bot - launcher
rem  NOTE: keep this file ASCII-only and CRLF. cmd.exe mis-parses
rem        non-ASCII bytes, and LF-only .bat breaks if-blocks.
rem ---------------------------------------------------------------

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)

if not defined PY (
  echo [ERROR] Python not found.
  echo         Install Python 3.9+ from https://www.python.org/downloads/
  echo         and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

%PY% -m iabot %*
set "RC=%ERRORLEVEL%"

rem 0 = normal exit; 130 / -1073741510 = user pressed Ctrl+C
if "%RC%"=="0" exit /b 0
if "%RC%"=="130" exit /b 0
if "%RC%"=="-1073741510" exit /b 0

echo.
echo [HINT] iabot exited with code %RC%.
echo        Network / CA error : run.bat check
echo        Anything else      : see README.md
echo.
pause
exit /b %RC%