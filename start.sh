#!/usr/bin/env bash
# Reins2API launcher: delegates to server/scripts/start.py (env setup, deps, build,
# port cleanup, start, health check, console view). All arguments pass through.
set -euo pipefail
cd "$(dirname "$0")"

# venv 路径按平台二选一;缺失时用 uv 补一次依赖
case "$(uname -s)" in
  Linux*|Darwin) VENV_PY="server/.venv/bin/python" ;;
  *)             VENV_PY="server/.venv/Scripts/python.exe" ;;
esac
if [ ! -f "$VENV_PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv sync --directory server
  else
    echo "[ERROR] Python venv not found. Install uv (https://docs.astral.sh/uv/) then run: uv sync --directory server" >&2
    exit 1
  fi
fi

exec "$VENV_PY" server/scripts/start.py "$@"
