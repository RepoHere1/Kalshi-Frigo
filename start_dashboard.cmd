@echo off
setlocal enabledelayedexpansion
REM ============================================================================
REM  Kalshi-Frigo - local dashboard launcher
REM
REM  Usage:
REM    start_dashboard.cmd          start on http://localhost:5000 (LIVE creds)
REM    start_dashboard.cmd demo     start on http://localhost:5000 (demo creds)
REM    start_dashboard.cmd 5050     start on http://localhost:5050 (LIVE creds)
REM
REM  Credentials are read from D:\master.env - never hard-coded here.
REM ============================================================================

cd /d "%~dp0"

set "PORT=5000"
set "MODE=live"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="demo" goto gotodemo
set "ARG=%~1"
echo %ARG%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 goto badarg
set "PORT=%ARG%"
shift
goto parse
:gotodemo
set "MODE=demo"
shift
goto parse
:badarg
echo Unknown argument "%~1". Usage: start_dashboard.cmd [demo] [port]
exit /b 1
:parsed

set "MASTER=D:\master.env"
if not exist "%MASTER%" (
  echo.
  echo ERROR: %MASTER% not found.
  echo The Kalshi / OpenRouter / dashboard credentials live in that file.
  echo.
  pause
  exit /b 1
)

REM --- pull the values we need out of master.env -------------------------------
set "KALSHI_API_KEY="
set "KALSHI_API_KEY_ID="
set "KALSHI_PRIVATE_KEY_PATH="
set "OPENROUTER_API_KEY="
set "DASHBOARD_TOKEN="

for /f "usebackq tokens=1,* delims==" %%A in ("%MASTER%") do (
  set "NAME=%%A"
  set "VALUE=%%B"
  if "!NAME!"=="KALSHI_API_KEY"      set "KALSHI_API_KEY=!VALUE!"
  if "!NAME!"=="KALSHI_API_KEY_ID"   set "KALSHI_API_KEY_ID=!VALUE!"
  if "!NAME!"=="KALSHI_PRIVATE_KEY_PATH" set "KALSHI_PRIVATE_KEY_PATH=!VALUE!"
  if "!NAME!"=="OPENROUTER_API_KEY"  set "OPENROUTER_API_KEY=!VALUE!"
  if "!NAME!"=="DASHBOARD_TOKEN"     set "DASHBOARD_TOKEN=!VALUE!"
)

REM --- demo credentials -------------------------------------------------------
if /i "%MODE%"=="demo" (
  set "KALSHI_API_KEY=5dd1a149-7b4b-4b2d-988e-05d947fc6706"
  set "KALSHI_PRIVATE_KEY_PATH=C:\Users\Device\kalshi_demo_key.pem"
)

REM --- the code reads KALSHI_API_KEY; master.env names it KALSHI_API_KEY_ID ---
if not defined KALSHI_API_KEY set "KALSHI_API_KEY=%KALSHI_API_KEY_ID%"

if not defined KALSHI_API_KEY (
  echo.
  echo ERROR: no Kalshi API key id found in %MASTER%
  pause
  exit /b 1
)
if not exist "%KALSHI_PRIVATE_KEY_PATH%" (
  echo.
  echo ERROR: private key not found at %KALSHI_PRIVATE_KEY_PATH%
  pause
  exit /b 1
)

set "PORT=%PORT%"
set "PYTHONUNBUFFERED=1"

echo.
echo  Kalshi-Frigo dashboard
echo  ----------------------
echo   mode    : %MODE%
echo   key id  : %KALSHI_API_KEY%
echo   key file: %KALSHI_PRIVATE_KEY_PATH%
echo   url     : http://localhost:%PORT%/
echo.
echo  (close this window to stop the dashboard)
echo.

start "" "http://localhost:%PORT%/"
python web_dashboard.py

endlocal
