@echo off
rem ===================================================================
rem  cx - Command-line entry for the Chaoxing automation platform.
rem
rem  Usage:   cx <command> [options]
rem  Examples:
rem      cx cookies_login
rem      cx cookies_verify
rem      cx list_courses
rem      cx --help
rem
rem  IMPORTANT - DO NOT DOUBLE-CLICK THIS FILE.
rem  With no arguments it can only print a guide, and without the
rem  pause below the window would close before you could read it.
rem  To START THE BROWSER, double-click the separate launcher file
rem  that sits next to this one (the other .cmd in this folder).
rem  Its name is printed by the guide when you run this file.
rem
rem  NOTE: This file is intentionally pure ASCII with CRLF endings.
rem  A .cmd is parsed using the console code page, so non-ASCII text
rem  (even inside a rem) can be corrupted. All Chinese output comes
rem  from Python, which writes through WriteConsoleW and is therefore
rem  code-page independent. The project root is derived at runtime
rem  via %~dp0 and never hard-coded.
rem ===================================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY="

rem 1) project-local virtualenv takes priority
if not defined PY if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"

rem 2) python on PATH
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)

rem 3) py launcher
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)

rem 4) known install locations (last resort)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not defined PY (
  echo.
  echo [ERROR] Python interpreter not found.
  echo         Install Python 3.11+ and add it to PATH,
  echo         or create a .venv inside this project.
  echo.
  echo [ Press any key to close this window ]
  pause >nul
  exit /b 1
)

rem -------------------------------------------------------------------
rem No arguments means this was almost certainly double-clicked.
rem Print the guide, then PAUSE so it does not vanish.
rem -------------------------------------------------------------------
if "%~1"=="" (
  cd /d "%ROOT%"
  "%PY%" -m orchestrator.cli
  echo [ Press any key to close this window ]
  echo.
  pause >nul
  exit /b 0
)

cd /d "%ROOT%"
"%PY%" -m orchestrator.cli %*
exit /b %ERRORLEVEL%
