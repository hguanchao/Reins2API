#!/usr/bin/env bash
# Reins2API 一键启动(开发/本机):
#   ① 检测并自动安装前后端运行环境(uv 缺失用 pip 装,Node 缺失给出安装指引)
#      → 同步后端依赖 → web-dist 缺失时用 npm 构建前端
#   ② 按端口结束旧实例
#   ③ 后台启动服务(nohup,日志落 server/logs/)
#   ④ 轮询健康检查,输出启动成功日志
# 用法: ./start.sh [端口] [--rebuild]   # --rebuild 强制重建前端;再次运行 = 重启
set -euo pipefail
cd "$(dirname "$0")"

PORT=8787
REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild) REBUILD=1 ;;
    *) PORT="$arg" ;;
  esac
done

# ── ① 环境检测与自动安装 ─────────────────────────────────────────
echo "==> [1/4] 检测运行环境"
command -v uv >/dev/null 2>&1 || {
  echo "    未找到 uv,尝试用 pip 安装..."
  PY=""
  for cand in python3 python; do
    command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
  done
  [ -n "$PY" ] || { echo "缺少 uv 且无 Python,无法自动安装。请先装 Python 或 uv: https://docs.astral.sh/uv/"; exit 1; }
  "$PY" -m pip install uv || "$PY" -m pip install --user uv || {
    echo "pip 安装 uv 失败,请手动安装后重试: pip install uv"; exit 1; }
}
command -v uv >/dev/null 2>&1 || { echo "缺少 uv,请先安装: https://docs.astral.sh/uv/"; exit 1; }
echo "    后端: $(uv --version)"

echo "==> 后端依赖 (uv sync --directory server)"
uv sync --directory server

if [ "$REBUILD" = 1 ] || [ ! -f web-dist/index.html ]; then
  echo "    前端: 检测 Node.js(构建 web-dist 需要)"
  command -v node >/dev/null 2>&1 || { echo "缺少 Node.js (>=18),请安装: https://nodejs.org/"; exit 1; }
  command -v npm  >/dev/null 2>&1 || { echo "缺少 npm"; exit 1; }
  (
    cd web
    if [ ! -d node_modules ]; then
      echo "    安装前端依赖 (npm install)"
      npm install --no-audit --no-fund
    fi
    echo "    构建前端 (npm run build)"
    npm run build
  )
else
  echo "    前端: web-dist 已存在,跳过构建(--rebuild 可强制重建)"
fi

# ── ② 按端口结束旧实例 ───────────────────────────────────────────
echo "==> [2/4] 清理端口 ${PORT}"
PIDS=""
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti tcp:"${PORT}" -s tcp:listen 2>/dev/null || true)
elif command -v fuser >/dev/null 2>&1; then
  PIDS=$(fuser "${PORT}"/tcp 2>/dev/null || true)
elif netstat -ano >/dev/null 2>&1; then
  # Git Bash / Windows:netstat -ano 第 5 列是 PID
  PIDS=$(netstat -ano | awk -v p=":${PORT}" '$2 ~ p"$" && /LISTENING/ {print $5}' | sort -u)
fi
if [ -n "$PIDS" ]; then
  echo "    结束旧实例: $(echo "$PIDS" | tr '\n' ' ')"
  kill -9 $PIDS 2>/dev/null || true
  sleep 0.8
else
  echo "    端口 ${PORT} 空闲"
fi

# ── ③ 后台启动服务 ───────────────────────────────────────────────
echo "==> [3/4] 启动服务(后台)"
mkdir -p server/logs
LOG_OUT="server/logs/serve.out.log"
nohup uv run --directory server python main.py --serve "${PORT}" >"$LOG_OUT" 2>&1 &
SERVICE_PID=$!
disown || true
echo "    启动器 PID ${SERVICE_PID},控制台副本: ${LOG_OUT}"

# ── ④ 健康检查 + 成功日志 ────────────────────────────────────────
echo "==> [4/4] 等待就绪 (healthz)"
READY=0
for _ in $(seq 1 30); do
  sleep 1
  if curl -s -m 2 --noproxy '*' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"ok"'; then
    READY=1
    break
  fi
done
if [ "$READY" != 1 ]; then
  echo "    服务未在 30 秒内就绪,最近输出:"
  tail -15 "$LOG_OUT" 2>/dev/null || true
  echo "启动失败,请查看 ${LOG_OUT} 与 server/logs/"
  exit 1
fi
echo ""
echo "[OK] 启动成功,服务已在后台运行 (端口 ${PORT})"
echo "    推理:  http://127.0.0.1:${PORT}/v1/chat/completions | /v1/responses | /v1/messages"
echo "    模型:  http://127.0.0.1:${PORT}/v1/models"
echo "    管理台: http://127.0.0.1:${PORT}/"
echo "    健康:  http://127.0.0.1:${PORT}/healthz"
echo "    日志:  server/logs/server.log(按日滚动)"
echo "    重启:  再次运行本脚本(自动结束旧实例);停止: kill 掉 ${PORT} 端口进程"
echo ""
echo "本窗口保持打开并实时滚动日志;直接关闭窗口不影响后台服务(Ctrl+C 只停止滚动)"
echo ""
tail -n 15 -F server/logs/server.log
