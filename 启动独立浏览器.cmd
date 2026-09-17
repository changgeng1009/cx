@echo off
rem ===================================================================
rem  Start the isolated Edge instance used for Chaoxing login.
rem
rem  Double-click this file. It is deliberately:
rem    - pure ASCII   (a .cmd is parsed with the console code page)
rem    - CRLF ended   (LF can break parenthesised blocks)
rem    - ends in pause (so the output stays on screen)
rem
rem  Why a separate launcher instead of cx.cmd: when you double-click
rem  this, the parent process is explorer.exe, so the browser window
rem  is NOT tied to any agent session and will stay up.
rem ===================================================================
rem  Usage:
rem      just double-click it  -> opens Chaoxing login page
rem      pass extra flags      -> this file forwards them, e.g.
rem          --port 9334
rem          --url https://i.chaoxing.com

setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

rem Show UTF-8 correctly: Python writes UTF-8, cmd defaults to cp936.
chcp 65001 >nul 2>nul

set "PY="
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not defined PY (
  echo.
  echo [ERROR] Python interpreter not found.
  echo         Install Python 3.11+ and add it to PATH,
  echo         or create a .venv in this folder.
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   Isolated Edge instance
echo   NOT your daily Chrome.
echo   NOT the Edge window another agent is using.
echo ============================================================
echo.

"%PY%" -m orchestrator.browser --launch --url https://i.chaoxing.com %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo ============================================================
  echo   Next steps:
  echo     1. In the Edge window that just opened, log in to Chaoxing
  echo        ^(scan the QR code or use your password^).
  echo     2. Back here, run:   cx cookies_login --no-open
  echo        That detects the login and saves the cookies.
  echo     3. Then verify:      cx cookies_verify
  echo ============================================================
) else (
  echo ============================================================
  echo   LAUNCH FAILED ^(exit code %RC%^)
  echo.
  echo   Run the diagnosis and send me its output:
  echo     python -m orchestrator.browser --diagnose
  echo ============================================================
)
echo.
pause
