@echo off
REM Reins2API launcher: delegates to server/scripts/start.py (env setup, deps, build,
REM port cleanup, start, health check, console view).
REM Modes: start (default) / --status / --stop / --doctor / --attach (run with /?)
REM Keep this file ASCII-only: cmd mis-parses UTF-8 Chinese batch files on GBK consoles.
setlocal
set "SERVER_DIR=%~dp0server"

REM Prefer the project venv python, fall back to whatever python is on PATH.
set "PY=%SERVER_DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  powershell -NoProfile -Command "if(Get-Command uv -EA SilentlyContinue){uv sync --directory 'server'}" >nul 2>&1
  if not exist "%PY%" (
    echo [ERROR] Python venv not found. Install uv ^(https://docs.astral.sh/uv/^) then run: uv sync --directory server
    pause
    exit /b 1
  )
)

"%PY%" "%SERVER_DIR%\scripts\start.py" %*
if errorlevel 1 (
  echo.
  echo [ERROR] Start failed. See messages above.
  pause
)
