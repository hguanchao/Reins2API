@echo off
REM Reins2API launcher: delegates to start.ps1 (4 steps: env setup, port cleanup, start, health check)
REM Keep this file ASCII-only: cmd mis-parses UTF-8 Chinese batch files on GBK consoles.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo [ERROR] Start failed. See messages above.
  pause
)
