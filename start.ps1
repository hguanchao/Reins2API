# Reins2API 一键启动(PowerShell):
#   ① 检测并自动安装前后端运行环境(uv 缺失用 pip 装,Node 缺失用 winget 装)
#      → 同步后端依赖 → 前端产物缺失或源码更新时用 npm 构建前端
#   ② 按端口结束旧实例
#   ③ 后台启动服务(窗口可关,日志落 server/logs/)
#   ④ 轮询健康检查,输出启动成功日志,窗口保持打开滚动本次启动后的实时日志
# 用法: .\start.ps1 [-Port 8787] [-Rebuild]   # 源码新于产物会自动重建,-Rebuild 强制重建;再次运行 = 重启
# 输出风格:圆点步骤(模板 A)——绿●完成 黄●进行中 红✗失败;子进程原始输出缩进弱色。
param(
  [int]$Port = 8787,
  [switch]$Rebuild
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ── 输出助手 ──────────────────────────────────────────────────────

# 渲染宽度:CJK/全角按 2 列计(标签对齐用,覆盖本文案用到的字符集足够)。
function Get-RenderWidth([string]$Text) {
  $width = 0
  foreach ($ch in $Text.ToCharArray()) {
    $code = [int]$ch
    if (($code -ge 0x2E80 -and $code -le 0x9FFF) -or
        ($code -ge 0xF900 -and $code -le 0xFAFF) -or
        ($code -ge 0xFF00 -and $code -le 0xFF60)) {
      $width += 2
    } else {
      $width += 1
    }
  }
  return $width
}

function Write-Hero {
  Write-Host ""
  Write-Host "Reins2API  ·  一键启动" -ForegroundColor Cyan
  Write-Host ""
}

# 一行步骤:绿● 标签(对齐) 值。$Color: Green 完成 / Yellow 进行中 / Red 失败。
function Write-Step([string]$Label, [string]$Value, [string]$Color = "Green") {
  $pad = " " * [Math]::Max(1, 12 - (Get-RenderWidth $Label))
  Write-Host "  " -NoNewline
  Write-Host "● " -NoNewline -ForegroundColor $Color
  Write-Host ($Label + $pad + $Value)
}

function Write-Dim([string]$Text) {
  Write-Host ("    │ " + $Text) -ForegroundColor DarkGray
}

function Write-Rule {
  Write-Host ("  " + ("─" * 46)) -ForegroundColor DarkGray
}

function Die([string]$Message, [string]$Hint = "") {
  Write-Host "  " -NoNewline
  Write-Host "✗ " -NoNewline -ForegroundColor Red
  Write-Host $Message -ForegroundColor Red
  if ($Hint) { Write-Host "    $Hint" -ForegroundColor DarkGray }
  exit 1
}

# 汇总卡片行:5 空格缩进 + 标签对齐到 8 渲染列 + 值。
function Write-CardLine([string]$Label, [string]$Value) {
  $pad = " " * [Math]::Max(1, 8 - (Get-RenderWidth $Label))
  Write-Host ("     " + $Label + $pad + "  " + $Value)
}

# 子进程输出缩进弱色转发。临时降级 $ErrorActionPreference:PS 5.1 在 EAP=Stop 下
# 重定向 native stderr 会把进度行当错误抛 NativeCommandError(uv/pip/npm 都写 stderr)。
function Invoke-Dim([scriptblock]$Command) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Command 2>&1 | ForEach-Object { Write-Dim "$_" }
  } finally {
    $ErrorActionPreference = $prev
  }
}

# ── ① 环境检测与自动安装 ─────────────────────────────────────────

Write-Hero

# PATH 找不到命令时,探测常见安装位置并临时加入 PATH(双击 bat 启动时走注册表
# PATH,不含 ~/.local/bin 这类只在 shell 配置里追加的目录)
function Add-FromKnownDirs([string]$Exe, [string[]]$Dirs) {
  if (Get-Command $Exe -ErrorAction SilentlyContinue) { return }
  foreach ($dir in $Dirs) {
    if ($dir -and (Test-Path (Join-Path $dir $Exe))) {
      $env:PATH = "$dir;$env:PATH"
      Write-Dim "已定位 $Exe → $dir"
      return
    }
  }
}

Add-FromKnownDirs "uv.exe" @(
  "$env:USERPROFILE\.local\bin",
  "$env:USERPROFILE\.cargo\bin",
  "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Dim "未找到 uv,尝试用 pip 安装…"
  $py = $null
  foreach ($cand in @("python", "py")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
  }
  if (-not $py) {
    Die "没有 uv 也没有可用的 Python,无法自动安装" "请先安装 Python 或 uv: https://docs.astral.sh/uv/"
  }
  Invoke-Dim { & $py -m pip install uv }
  if ($LASTEXITCODE -ne 0) {
    Invoke-Dim { & $py -m pip install --user uv }
  }
  if ($LASTEXITCODE -ne 0) {
    Die "pip 安装 uv 失败" "请手动安装后重试: pip install uv"
  }
  # pip 把 uv.exe 放进 Scripts 目录(按安装方式不同二选一),临时加入 PATH
  $scriptsDirs = & $py -c "import sysconfig;print(sysconfig.get_path('scripts'));print(sysconfig.get_path('scripts','nt_user'))"
  foreach ($d in $scriptsDirs) {
    if ($d -and (Test-Path (Join-Path $d "uv.exe"))) {
      $env:PATH = "$d;$env:PATH"
      break
    }
  }
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "uv 安装后仍不可用" "请手动安装: https://docs.astral.sh/uv/"
  }
}
$uvVersion = (((uv --version 2>$null) -replace '^uv ', '')).Split(" ")[0]
Write-Step "运行环境" "uv $uvVersion"

# ── 后端依赖 ─────────────────────────────────────────────────────

Invoke-Dim { uv sync --directory server }
if ($LASTEXITCODE -ne 0) {
  Die "后端依赖同步失败 (uv sync)" "请检查上方输出"
}
Write-Step "后端依赖" "uv sync 完成"

# ── 前端 ─────────────────────────────────────────────────────────

# 构建输入(web 源码/配置)是否新于 web/dist/index.html。dist 未纳入 git,
# 只判"产物是否存在"会让 pull 后的前端一直停在旧产物上,故按 mtime 补一次新鲜度判断。
function Test-WebSourcesNewer([string]$DistIndex) {
  $distTime = (Get-Item -LiteralPath $DistIndex).LastWriteTimeUtc
  $inputs = @(Get-ChildItem -LiteralPath "web\src", "web\public" -Recurse -File -ErrorAction SilentlyContinue)
  $inputs += @(Get-Item -LiteralPath @(
    "web\index.html", "web\package.json", "web\package-lock.json", "web\vite.config.ts",
    "web\tsconfig.json", "web\tsconfig.app.json", "web\tsconfig.node.json"
  ) -ErrorAction SilentlyContinue)
  foreach ($f in $inputs) {
    if ($f.LastWriteTimeUtc -gt $distTime) { return $true }
  }
  return $false
}

$buildReason = ""
if ($Rebuild) { $buildReason = "强制重建" }
elseif (-not (Test-Path "web/dist/index.html")) { $buildReason = "产物缺失" }
elseif (Test-WebSourcesNewer "web/dist/index.html") { $buildReason = "源码有更新" }

if ($buildReason) {
  Write-Dim "重建前端($buildReason)…"
  Add-FromKnownDirs "node.exe" @(
    "$env:ProgramFiles\nodejs",
    "${env:ProgramFiles(x86)}\nodejs",
    "$env:APPDATA\nvm"
  )
  if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Dim "未找到 Node.js,尝试用 winget 安装…"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
      Invoke-Dim { winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements }
      Add-FromKnownDirs "node.exe" @("$env:ProgramFiles\nodejs")
    }
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
      Die "缺少 Node.js (>=18)" "请安装后重试: https://nodejs.org/"
    }
  }
  $nodeVersion = (node --version 2>$null)
  Push-Location web
  try {
    if (-not (Test-Path "node_modules")) {
      Write-Dim "npm install …"
      Invoke-Dim { npm install --no-audit --no-fund }
      if ($LASTEXITCODE -ne 0) { Die "前端依赖安装失败 (npm install)" }
    }
    Write-Dim "npm run build …"
    Invoke-Dim { npm run build }
    if ($LASTEXITCODE -ne 0) { Die "前端构建失败 (npm run build)" }
  } finally {
    Pop-Location
  }
  Write-Step "前端" "构建完成 · node $nodeVersion"
} else {
  Write-Step "前端" "web/dist 已就绪 · 跳过构建"
}

# ── ② 按端口结束旧实例 ───────────────────────────────────────────

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conns) {
  foreach ($procId in ($conns.OwningProcess | Sort-Object -Unique)) {
    $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
    Write-Dim "结束旧实例: $name (PID $procId)"
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 800
  Write-Step "端口 $Port" "清理完成"
} else {
  Write-Step "端口 $Port" "空闲"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  Die "端口 $Port 仍被占用且无法结束" "请手动处理"
}

# ── ③ 后台启动服务 ───────────────────────────────────────────────

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "server\logs") | Out-Null
$serverLog = Join-Path $PSScriptRoot "server\logs\server.log"
$uvExe = (Get-Command uv).Source
# 进程 stdout/stderr 丢进空设备:日志由 loguru 全量写 server.log,不再另开 serve.err/serve.out。
$proc = Start-Process -FilePath $uvExe `
  -ArgumentList @("run", "--directory", "server", "python", "main.py", "--serve", "$Port") `
  -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "NUL" -RedirectStandardError "\\.\NUL"
Write-Step "服务" "后台运行 · 启动器 PID $($proc.Id)"
Write-Dim "日志: server\logs\server.log"

# ── ④ 健康检查 + 汇总卡片 ────────────────────────────────────────

# server.log 跨重启持续追加,窗口实时日志从本次启动行号接:先记下旧行数,
# 末尾 tail 时跳过,避免一开窗先滚出上一次运行的日志。
$logOffset = (Get-Content $serverLog -ErrorAction SilentlyContinue | Measure-Object -Line).Lines

Write-Dim "等待服务就绪 (healthz,最长 30s)…"
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
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
  Write-Dim "服务未就绪,最近日志:"
  Get-Content $serverLog -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Write-Dim "$_" }
  Die "启动失败" "请查看 server\logs\server.log"
}
$stopwatch.Stop()
Write-Step "健康检查" ("就绪 ({0:0.0}s)" -f $stopwatch.Elapsed.TotalSeconds)

$listenPid = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
  Select-Object -First 1).OwningProcess

Write-Host ""
Write-Rule
Write-Host "  " -NoNewline
Write-Host "✓ 已就绪   " -NoNewline -ForegroundColor Green
Write-Host "http://127.0.0.1:$Port"
Write-Host ""
Write-CardLine "管理台" "http://127.0.0.1:$Port/"
Write-CardLine "推理" "/v1/chat/completions · /v1/responses · /v1/messages"
Write-CardLine "模型" "/v1/models"
Write-CardLine "健康" "/healthz"
Write-CardLine "日志" "server\logs\server.log(按日滚动)"
Write-Host ""
Write-CardLine "重启" "再次运行本脚本(自动结束旧实例)"
Write-CardLine "停止" "结束端口 $Port 进程(当前 PID $listenPid)"
Write-Host ""
Write-Rule
Write-Host ""
Write-Host "本窗口实时滚动本次启动后的日志;直接关闭窗口不影响后台服务(Ctrl+C 只停止滚动)" -ForegroundColor DarkGray
Write-Host ""
# 无 -Tail:-Wait 先流式读完旧内容再由计数器丢弃(首屏空),此后逐行跟新;
# -ReadCount 1 保证单行到达,计数器行号才与文件行号对齐。
$seen = 0
Get-Content (Join-Path $PSScriptRoot "server\logs\server.log") -Wait -ReadCount 1 -Encoding UTF8 -ErrorAction SilentlyContinue |
  Where-Object { $script:seen++; $script:seen -gt $logOffset }
