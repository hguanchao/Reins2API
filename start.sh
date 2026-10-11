#!/usr/bin/env bash
# Reins2API 一键启动(开发/本机):
#   ① 检测并自动安装前后端运行环境(uv 缺失用 pip 装,Node 缺失给出安装指引)
#      → 同步后端依赖 → web/dist 缺失时用 npm 构建前端
#   ② 按端口结束旧实例
#   ③ 后台启动服务(nohup,日志落 server/logs/)
#   ④ 轮询健康检查,输出启动成功日志
# 用法: ./start.sh [端口] [--rebuild]   # --rebuild 强制重建前端;再次运行 = 重启
# 输出风格:圆点步骤(模板 A)——绿●完成 红✗失败;子进程原始输出缩进弱色。
# 颜色仅在 TTY 上启用(重定向/CI 自动退化为纯文本)。
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

# ── 输出助手(模板 A:圆点步骤 + 汇总卡片) ─────────────────────────

if [ -t 1 ]; then
  C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_DIM=$'\033[2m'; C_RESET=$'\033[0m'
else
  C_CYAN=""; C_GREEN=""; C_RED=""; C_DIM=""; C_RESET=""
fi

hero() { printf "\n%bReins2API  ·  一键启动%b\n\n" "$C_CYAN" "$C_RESET"; }

# 一行步骤:绿● 标签(对齐) 值。标签集固定,按渲染宽度手工补位(CJK=2 列)。
step() {  # $1=标签 $2=值 [$3=颜色,默认绿]
  local color="${3:-$C_GREEN}" pad
  case "$1" in
    "前端"|"服务")        pad="        " ;;
    "端口 ${PORT}")       pad="   " ;;
    *)                    pad="    " ;;
  esac
  printf "  %b●%b %s%s%s\n" "$color" "$C_RESET" "$1" "$pad" "$2"
}

dim()  { printf "  %b│ %s%b\n" "$C_DIM" "$1" "$C_RESET"; }
rule() { printf "  %b%s%b\n" "$C_DIM" "──────────────────────────────────────────────" "$C_RESET"; }

die() {  # $1=错误 $2=提示(可选)
  printf "  %b✗ %s%b\n" "$C_RED" "$1" "$C_RESET"
  if [ -n "${2:-}" ]; then
    printf "    %b%s%b\n" "$C_DIM" "$2" "$C_RESET"
  fi
  exit 1
}

card() { printf "     %s%s\n" "$1" "$2"; }  # $1=已补位标签 $2=值

# 子进程输出缩进弱色转发。
dim_pipe() {
  while IFS= read -r line; do
    printf "  %b│ %s%b\n" "$C_DIM" "$line" "$C_RESET"
  done
}

# ── ① 环境检测与自动安装 ─────────────────────────────────────────

hero

if ! command -v uv >/dev/null 2>&1; then
  dim "未找到 uv,尝试用 pip 安装…"
  PY=""
  for cand in python3 python; do
    command -v "$cand" >/dev/null 2>&1 && { PY="$cand"; break; }
  done
  [ -n "$PY" ] || die "没有 uv 也没有可用的 Python,无法自动安装" "请先装 Python 或 uv: https://docs.astral.sh/uv/"
  "$PY" -m pip install uv 2>&1 | dim_pipe || "$PY" -m pip install --user uv 2>&1 | dim_pipe || \
    die "pip 安装 uv 失败" "请手动安装后重试: pip install uv"
fi
command -v uv >/dev/null 2>&1 || die "缺少 uv" "请先安装: https://docs.astral.sh/uv/"
UV_VERSION="$(uv --version | awk '{print $2}')"
step "运行环境" "uv ${UV_VERSION}"

# ── 后端依赖 ─────────────────────────────────────────────────────

if ! uv sync --directory server 2>&1 | dim_pipe; then
  die "后端依赖同步失败 (uv sync)" "请检查上方输出"
fi
step "后端依赖" "uv sync 完成"

# ── 前端 ─────────────────────────────────────────────────────────

if [ "$REBUILD" = 1 ] || [ ! -f web/dist/index.html ]; then
  command -v node >/dev/null 2>&1 || die "缺少 Node.js (>=18)" "请安装: https://nodejs.org/"
  command -v npm  >/dev/null 2>&1 || die "缺少 npm"
  NODE_VERSION="$(node --version)"
  if ! (
    cd web
    if [ ! -d node_modules ]; then
      printf "  %b│ npm install …%b\n" "$C_DIM" "$C_RESET"
      npm install --no-audit --no-fund 2>&1 | dim_pipe
    fi
    printf "  %b│ npm run build …%b\n" "$C_DIM" "$C_RESET"
    npm run build 2>&1 | dim_pipe
  ); then
    die "前端构建失败" "请检查上方输出"
  fi
  step "前端" "构建完成 · node ${NODE_VERSION}"
else
  step "前端" "web/dist 已就绪 · 跳过构建"
fi

# ── ② 按端口结束旧实例 ───────────────────────────────────────────

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
  dim "结束旧实例: $(echo "$PIDS" | tr '\n' ' ')"
  kill -9 $PIDS 2>/dev/null || true
  sleep 0.8
  step "端口 ${PORT}" "清理完成"
else
  step "端口 ${PORT}" "空闲"
fi

# ── ③ 后台启动服务 ───────────────────────────────────────────────

mkdir -p server/logs
# 进程 stdout/stderr 丢进 /dev/null:日志由 loguru 全量写 server/logs/server.log,不再另开 serve.*.log。
nohup uv run --directory server python main.py --serve "${PORT}" >/dev/null 2>&1 &
SERVICE_PID=$!
disown || true
step "服务" "后台运行 · 启动器 PID ${SERVICE_PID}"
dim "日志: server/logs/server.log"

# ── ④ 健康检查 + 汇总卡片 ────────────────────────────────────────

dim "等待服务就绪 (healthz,最长 30s)…"
T0=$SECONDS
READY=0
for _ in $(seq 1 30); do
  sleep 1
  if curl -s -m 2 --noproxy '*' "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"ok"'; then
    READY=1
    break
  fi
done
if [ "$READY" != 1 ]; then
  dim "服务未就绪,最近日志:"
  tail -15 server/logs/server.log 2>/dev/null | dim_pipe || true
  die "启动失败" "请查看 server/logs/server.log"
fi
step "健康检查" "就绪 ($((SECONDS - T0))s)"

printf "\n"
rule
printf "  %b✓ 已就绪%b   http://127.0.0.1:%s\n\n" "$C_GREEN" "$C_RESET" "${PORT}"
card "管理台  " "http://127.0.0.1:${PORT}/"
card "推理    " "/v1/chat/completions · /v1/responses · /v1/messages"
card "模型    " "/v1/models"
card "健康    " "/healthz"
card "日志    " "server/logs/server.log(按日滚动)"
printf "\n"
card "重启    " "再次运行本脚本(自动结束旧实例)"
card "停止    " "结束端口 ${PORT} 进程"
printf "\n"
rule
printf "\n"
printf "%b本窗口保持打开并实时滚动日志;直接关闭窗口不影响后台服务(Ctrl+C 只停止滚动)%b\n" "$C_DIM" "$C_RESET"
printf "\n"
tail -n 15 -F server/logs/server.log
