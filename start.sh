#!/usr/bin/env bash
# Reins2API 启动器：转发到 server/scripts/start.py（环境准备、依赖、构建、端口清理、
# 拉起、健康检查、控制台视图）。所有参数原样透传。
# 模式：start（默认）| --status | --stop | --doctor | --attach
# 退出码：0 成功 / 2 参数错 / 3 没在跑 / 4 环境缺失 / 5 依赖与构建失败
#         6 端口被非本服务占用 / 7 另一个启动器在跑 / 8 服务拉起但未就绪
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
