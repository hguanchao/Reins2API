# Reins2API 一键启动(PowerShell):
#   ① 检测并自动安装前后端运行环境(uv 缺失用 pip 装,Node 缺失用 winget 装)
#      → 同步后端依赖 → web-dist 缺失时用 npm 构建前端
#   ② 按端口结束旧实例
#   ③ 后台启动服务(窗口可关,日志落 server/logs/)
#   ④ 轮询健康检查,输出启动成功日志,窗口保持打开滚动实时日志
# 用法: .\start.ps1 [-Port 8787] [-Rebuild]   # -Rebuild 强制重建前端;再次运行 = 重启
param(
  [int]$Port = 8787,
  [switch]$Rebuild
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# PATH 找不到命令时,探测常见安装位置并临时加入 PATH(双击 bat 启动时走注册表
# PATH,不含 ~/.local/bin 这类只在 shell 配置里追加的目录)
function Add-FromKnownDirs([string]$Exe, [string[]]$Dirs) {
  if (Get-Command $Exe -ErrorAction SilentlyContinue) { return }
  foreach ($dir in $Dirs) {
    if ($dir -and (Test-Path (Join-Path $dir $Exe))) {
      $env:PATH = "$dir;$env:PATH"
      Write-Host "    已定位 $Exe : $dir"
      return
    }
  }
}

# ── ① 环境检测与自动安装 ─────────────────────────────────────────
Write-Host "==> [1/4] 检测运行环境"

Add-FromKnownDirs "uv.exe" @(
  "$env:USERPROFILE\.local\bin",
  "$env:USERPROFILE\.cargo\bin",
  "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "    未找到 uv,尝试用 pip 安装..."
  $py = $null
  foreach ($cand in @("python", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
  }
  if (-not $py) {
    Write-Error "没有 uv 也没有可用的 Python,无法自动安装。请先安装 Python 或 uv: https://docs.astral.sh/uv/"
  }
  & $py -m pip install uv
  if ($LASTEXITCODE -ne 0) {
    & $py -m pip install --user uv
  }
  if ($LASTEXITCODE -ne 0) {
    Write-Error "pip 安装 uv 失败,请手动安装后重试: pip install uv"
  }
  # pip 把 uv.exe 放进 Scripts 目录(按安装方式不同二选一),临时加入 PATH
  $scriptsDirs = & $py -c "import sysconfig;print(sysconfig.get_path('scripts'));print(sysconfig.get_path('scripts','nt_user'))"
  foreach ($d in $scriptsDirs) {
    if ($d -and (Test-Path (Join-Path $d "uv.exe"))) {
      $env:PATH = "$d;$env:PATH"
      Write-Host "    uv 已安装: $d"
      break
    }
  }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv 安装后仍不可用,请手动安装: https://docs.astral.sh/uv/"
  }
} else {
  Write-Host "    后端: uv $( (uv --version 2>$null) -replace '^uv ', '' )"
}

Write-Host "==> 后端依赖 (uv sync --directory server)"
uv sync --directory server
if ($LASTEXITCODE -ne 0) { Write-Error "uv sync 失败,请检查输出" }

$needBuild = $Rebuild -or -not (Test-Path "web-dist/index.html")
if ($needBuild) {
  Write-Host "    前端: 检测 Node.js(构建 web-dist 需要)"
  Add-FromKnownDirs "node.exe" @(
    "$env:ProgramFiles\nodejs",
    "${env:ProgramFiles(x86)}\nodejs",
    "$env:APPDATA\nvm"
  )
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "    未找到 Node.js,尝试用 winget 安装..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
      Add-FromKnownDirs "node.exe" @("$env:ProgramFiles\nodejs")
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
      Write-Error "缺少 Node.js (>=18),请安装后重试: https://nodejs.org/"
    }
  }
  Push-Location web
  try {
    if (-not (Test-Path "node_modules")) {
      Write-Host "    安装前端依赖 (npm install)"
      npm install --no-audit --no-fund
      if ($LASTEXITCODE -ne 0) { Write-Error "npm install 失败" }
    }
    Write-Host "    构建前端 (npm run build)"
    npm run build
    if ($LASTEXITCODE -ne 0) { Write-Error "npm run build 失败" }
  } finally {
    Pop-Location
  }
} else {
  Write-Host "    前端: web-dist 已存在,跳过构建(-Rebuild 可强制重建)"
}

# ── ② 按端口结束旧实例 ───────────────────────────────────────────
Write-Host "==> [2/4] 清理端口 $Port"
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conns) {
  foreach ($procId in ($conns.OwningProcess | Sort-Object -Unique)) {
    $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
    Write-Host "    结束旧实例: $name (PID $procId)"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 800
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  Write-Error "端口 $Port 仍被占用且无法结束,请手动处理"
}
Write-Host "    端口 $Port 已就绪"

# ── ③ 后台启动服务 ───────────────────────────────────────────────
Write-Host "==> [3/4] 启动服务(后台)"
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "server\logs") | Out-Null
$outLog = Join-Path $PSScriptRoot "server\logs\serve.out.log"
$errLog = Join-Path $PSScriptRoot "server\logs\serve.err.log"
$uvExe = (Get-Command uv).Source
$proc = Start-Process -FilePath $uvExe `
  -ArgumentList @("run", "--directory", "server", "python", "main.py", "--serve", "$Port") `
  -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Write-Host "    启动器 PID $($proc.Id),控制台副本: server\logs\serve.out.log"

# ── ④ 健康检查 + 成功日志 ────────────────────────────────────────
Write-Host "==> [4/4] 等待就绪 (healthz)"
$ready = $false
for ($i = 1; $i -le 30; $i++) {
  Start-Sleep -Seconds 1
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect("127.0.0.1", $Port)
    $client.Close()
    $ready = $true
    break
  } catch { }
}
if (-not $ready) {
  Write-Host "    服务未在 30 秒内就绪,最近错误:"
  Get-Content $errLog -Tail 15 -ErrorAction SilentlyContinue
  Write-Error "启动失败,请查看 server\logs\ 下的日志"
}
$listenPid = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
  Select-Object -First 1).OwningProcess
Write-Host ""
Write-Host "[OK] 启动成功,服务已在后台运行 (PID $listenPid,端口 $Port)"
Write-Host "    推理:  http://127.0.0.1:$Port/v1/chat/completions | /v1/responses | /v1/messages"
Write-Host "    模型:  http://127.0.0.1:$Port/v1/models"
Write-Host "    管理台: http://127.0.0.1:$Port/"
Write-Host "    健康:  http://127.0.0.1:$Port/healthz"
Write-Host "    日志:  server\logs\server.log(按日滚动)"
Write-Host "    重启:  再次运行本脚本(自动结束旧实例);停止: 结束 PID $listenPid"
Write-Host ""
Write-Host "本窗口保持打开并实时滚动日志;直接关闭窗口不影响后台服务(Ctrl+C 只停止滚动)"
Write-Host ""
Get-Content (Join-Path $PSScriptRoot "server\logs\server.log") -Wait -Tail 15 -Encoding UTF8 -ErrorAction SilentlyContinue
