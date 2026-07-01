# Mind Signal Phase 18.1 dual-2pc operator PC starter
# Real EMOTIV device + BE + DE_A + proxy 통합 기동. 시연/실측용.
# Idempotent. 별도 PowerShell 창에서 각 서버 띄움.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-dual-2pc.ps1
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-dual-2pc.ps1 -SkipRedis -SkipProxy
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-dual-2pc.ps1 -OperatorIp 100.117.42.107
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-dual-2pc.ps1 -Production
#
# Stop: stop-realdevice-dual-2pc.ps1 (별도 파일)
#
# References:
# - 2026-05-27 cross-machine Tailscale GREEN [[project_mind_signal_phase18_mcafee_removed_cross_machine_green]]
# - 핫스팟/D-0 셋업 스크립트는 Tailscale 대체로 아카이브됨 (99_archive/). 방화벽은 핫스팟 전용이라 Tailscale 후 무관
# - [[feedback_start_process_conda_hook]] conda activate hook 안정성

[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$EngineSecret = "",
    [int]$BackendPort = 5000,
    [int]$DePort = 5002,
    [int]$ProxyPort = 5050,
    [string]$CondaEnv = "mind-signal",
    [string]$OperatorIp = "",
    [switch]$SkipRedis,
    [switch]$SkipProxy,
    [switch]$Production,
    [int]$HealthcheckTimeoutSec = 60,
    [int]$EmotivCortexPort = 6868
)

$ErrorActionPreference = "Stop"

# Resolve ProjectRoot: 미지정 시 스크립트 위치에서 유도함.
# 이 스크립트는 <Team-project>/mind-signal-data-engine/scripts/realdevice/ 에 위치하므로
# 3단계 상위가 Team-project 루트임 (사용자 경로 하드코딩 제거, 머신 무관 동작).
if (-not $ProjectRoot) {
    $here = Split-Path -Parent $PSCommandPath
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $here))
}

function Write-Step  { param($S,$M) Write-Host "[$S] $M" -ForegroundColor Yellow }
function Write-OK    { param($M) Write-Host "  $M" -ForegroundColor Green }
function Write-Warn2 { param($M) Write-Host "  $M" -ForegroundColor DarkYellow }
function Write-Fail  { param($M) Write-Host "  $M" -ForegroundColor Red }

function Get-OperatorTailscaleIp {
    try {
        $ip = (& "C:\Program Files\Tailscale\tailscale.exe" ip -4 2>$null | Select-Object -First 1).Trim()
        if ($ip -and $ip -match '^\d+\.\d+\.\d+\.\d+$') { return $ip }
    } catch { }
    return ""
}

function Wait-HttpHealthy {
    param(
        [string]$Url,
        [int]$TimeoutSec,
        [string]$ServiceName
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $attempt = 0
    while ((Get-Date) -lt $deadline) {
        $attempt++
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) {
                Write-OK ("[$ServiceName] healthy after $attempt attempt(s): $Url -> $($resp.StatusCode)")
                return $true
            }
        } catch {
            # not ready yet, retry
        }
        Start-Sleep -Seconds 1
    }
    Write-Fail ("[$ServiceName] healthcheck timeout after $TimeoutSec sec: $Url")
    return $false
}

function Test-PortInUse {
    param([int]$Port)
    $r = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    return $null -ne $r
}

# 0. Pre-flight
Write-Host ""
Write-Host "=== Mind Signal Realdevice Dual-2PC Starter (operator PC) ===" -ForegroundColor Cyan
Write-Host ("ProjectRoot:  " + $ProjectRoot) -ForegroundColor Gray
Write-Host ("Production:   " + $Production) -ForegroundColor Gray
Write-Host ""

# Resolve ENGINE_SECRET_KEY: param > env var > BE .env.local
if (-not $EngineSecret) { $EngineSecret = $env:ENGINE_SECRET_KEY }
if (-not $EngineSecret) {
    $beEnvPath = Join-Path $ProjectRoot "mind-signal-backend\.env.local"
    if (Test-Path $beEnvPath) {
        $line = Get-Content $beEnvPath | Where-Object { $_ -match "^ENGINE_SECRET_KEY=" } | Select-Object -First 1
        if ($line) { $EngineSecret = ($line -split "=", 2)[1].Trim('"').Trim() }
    }
}
if (-not $EngineSecret) {
    Write-Host "  ENGINE_SECRET_KEY not found. Pass -EngineSecret, set `$env:ENGINE_SECRET_KEY, or add to BE .env.local" -ForegroundColor Red
    exit 2
}

Write-Step "0/9" "Pre-flight (Tailscale + third-party AV + conda env)"

# 0a. Tailscale running
$tsService = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
if (-not $tsService -or $tsService.Status -ne "Running") {
    Write-Fail "Tailscale service not running. Start Tailscale UI first."
    exit 2
}
Write-OK "Tailscale service Running"

# 0b. Resolve operator IP
if (-not $OperatorIp) { $OperatorIp = Get-OperatorTailscaleIp }
if (-not $OperatorIp) {
    Write-Fail "Operator Tailscale IP resolve failed. Pass -OperatorIp explicitly."
    exit 2
}
Write-OK ("Operator IP = " + $OperatorIp)

# 0c. Third-party AV check (McAfee/Norton/AhnLab 등)
$avBlockers = @("mc-fw-host", "mcshield", "norton", "navapsvc", "ahnlab")
$avFound = Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $svc = $_
    $avBlockers | Where-Object { $svc.Name -like "*$_*" -or $svc.DisplayName -like "*$_*" } | Select-Object -First 1
}
if ($avFound) {
    Write-Warn2 "Third-party AV/Firewall detected. May block inbound TCP. See [[feedback_windows_third_party_av_firewall_layer]]"
    $avFound | ForEach-Object { Write-Warn2 ("  - " + $_.DisplayName + " (" + $_.Status + ")") }
} else {
    Write-OK "No third-party AV firewall blockers detected"
}

# 0d. Conda env exists (default: .conda\envs, fallback: miniconda3\envs)
$condaCandidates = @(
    "C:\Users\gs071\.conda\envs\$CondaEnv",
    "C:\Users\gs071\miniconda3\envs\$CondaEnv"
)
$condaPath = $condaCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $condaPath) {
    Write-Fail ("Conda env not found in any of: " + ($condaCandidates -join ", "))
    exit 2
}
Write-OK ("Conda env $CondaEnv at " + $condaPath)

# 0e. EMOTIV launcher (warn only, 사용자 위임)
if (Test-PortInUse -Port $EmotivCortexPort) {
    Write-OK ("EMOTIV Cortex listening on port " + $EmotivCortexPort)
} else {
    Write-Warn2 ("EMOTIV Cortex (port " + $EmotivCortexPort + ") not running. Launch EMOTIV Launcher 후 헤드셋 페어링 필요.")
}

Write-Host ""

# 1. Docker Redis
Write-Step "1/9" "Docker Redis"
if ($SkipRedis) {
    Write-Warn2 "SkipRedis flag set"
} else {
    docker info > $null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Docker Desktop not running. Start it first then re-run."
        exit 3
    }
    $existing = docker ps -a --filter "name=mind-signal-redis" --format "{{.Names}}" 2>$null
    if ($existing -match "mind-signal-redis") {
        $running = docker ps --filter "name=mind-signal-redis" --format "{{.Names}}" 2>$null
        if ($running -match "mind-signal-redis") {
            Write-OK "Redis container already running"
        } else {
            docker start mind-signal-redis | Out-Null
            Write-OK "Redis container started (existing)"
        }
    } else {
        Push-Location (Join-Path $ProjectRoot "mind-signal-backend")
        docker-compose up -d | Out-Null
        Pop-Location
        Write-OK "Redis container created via docker-compose"
    }
}

Write-Host ""

# 2. BE
# 환경변수 주입은 PowerShell 의 $env: 로 사전 set 후 Start-Process 자식 cmd 가 부모 env 상속하도록 함.
# cmd /k 안에서 "set X=val && npm" 패턴은 Start-Process ArgumentList 처리 시 자식 npm process 까지 환경변수 전달이 안 되는 사례 실측 ([[feedback_powershell_start_process_env_inject]]).
$env:ENGINE_SECRET_KEY = $EngineSecret
$env:NODE_ENV = 'local'
Write-Step "2/9" "Backend (port $BackendPort)"
if (Test-PortInUse -Port $BackendPort) {
    Write-Warn2 ("Port " + $BackendPort + " already in use. Assuming BE already running.")
} else {
    $beDir = Join-Path $ProjectRoot "mind-signal-backend"
    $beCmd = if ($Production) { "npm run build && npm start" } else { "npm run dev" }
    $beArgs = "/k cd /d `"$beDir`" && $beCmd"
    Start-Process -FilePath "cmd.exe" -ArgumentList $beArgs -WindowStyle Normal
    Write-OK "Backend terminal launched (env inherited from PowerShell scope)"
}

Write-Host ""

# 3. BE healthcheck
Write-Step "3/9" "Backend healthcheck (GET /api-docs)"
$beHealthy = Wait-HttpHealthy -Url "http://localhost:$BackendPort/api-docs" -TimeoutSec $HealthcheckTimeoutSec -ServiceName "BE"
if (-not $beHealthy) {
    Write-Fail "Backend failed to become healthy. Check BE terminal output."
    exit 4
}

Write-Host ""

# 4. DE_A
# $env: 사전 set 으로 자식 cmd + conda + python 까지 환경변수 정합 전달.
$env:LAN_IP = $OperatorIp
$env:PROXY_URL = "http://${OperatorIp}:$ProxyPort"
$env:BACKEND_URL = "http://localhost:$BackendPort"
if ($Production) { $env:UVICORN_RELOAD = 'false' }
Write-Step "4/9" "Data Engine A (port $DePort)"
if (Test-PortInUse -Port $DePort) {
    Write-Warn2 ("Port " + $DePort + " already in use. Assuming DE_A already running.")
} else {
    $deDir = Join-Path $ProjectRoot "mind-signal-data-engine"
    # 절대 conda python 경로로 직접 기동함 (cmd 자식에서 conda activate 미동작 사례 우회, AGENTS _start-de-test.bat 패턴).
    $deCmd = "`"$condaPath\python.exe`" run_server.py"
    $deArgs = "/k cd /d `"$deDir`" && $deCmd"
    Start-Process -FilePath "cmd.exe" -ArgumentList $deArgs -WindowStyle Normal
    Write-OK "DE_A terminal launched (env inherited from PowerShell scope)"
}

Write-Host ""

# 5. DE_A healthcheck
Write-Step "5/9" "DE_A healthcheck (GET /health)"
$deHealthy = Wait-HttpHealthy -Url "http://localhost:$DePort/health" -TimeoutSec $HealthcheckTimeoutSec -ServiceName "DE_A"
if (-not $deHealthy) {
    Write-Fail "DE_A failed to become healthy. Check DE_A terminal output."
    exit 5
}

Write-Host ""

# 6. Proxy
Write-Step "6/9" "Proxy (port $ProxyPort)"
if ($SkipProxy) {
    Write-Warn2 "SkipProxy flag set"
} elseif (Test-PortInUse -Port $ProxyPort) {
    Write-OK ("Port " + $ProxyPort + " already listening. Assuming proxy already running.")
} else {
    $proxyDir = Join-Path $ProjectRoot "mind-signal-proxy"
    # ENGINE_SECRET_KEY, BACKEND_URL 은 BE 단계에서 이미 $env: set 됨. PROXY_PORT 만 추가.
    $env:PROXY_PORT = "$ProxyPort"
    $proxyArgs = "/k cd /d `"$proxyDir`" && npm start"
    Start-Process -FilePath "cmd.exe" -ArgumentList $proxyArgs -WindowStyle Normal
    Write-OK "Proxy terminal launched (env inherited from PowerShell scope)"
}

Write-Host ""

# 7. Proxy healthcheck
Write-Step "7/9" "Proxy healthcheck (GET /health)"
if (-not $SkipProxy) {
    $proxyHealthy = Wait-HttpHealthy -Url "http://localhost:$ProxyPort/health" -TimeoutSec $HealthcheckTimeoutSec -ServiceName "Proxy"
    if (-not $proxyHealthy) {
        Write-Fail "Proxy failed to become healthy."
        exit 6
    }
}

Write-Host ""

# 8. Cross-machine Tailscale endpoint verification (self-curl from Tailscale IP)
Write-Step "8/9" "Tailscale endpoint self-verification"
$tsProxy = Wait-HttpHealthy -Url "http://${OperatorIp}:$ProxyPort/health" -TimeoutSec 5 -ServiceName "Proxy-via-Tailscale"
if (-not $tsProxy) {
    Write-Warn2 "Proxy not accessible via Tailscale IP. firewall/listen address 확인 필요."
}

Write-Host ""

# 9. Status report
Write-Step "9/9" "Status report"
Write-Host ""
Write-Host "  Service endpoints (operator PC):" -ForegroundColor Cyan
Write-Host ("    BE:       http://localhost:" + $BackendPort + "/api-docs") -ForegroundColor Gray
Write-Host ("    DE_A:     http://localhost:" + $DePort + "/health") -ForegroundColor Gray
Write-Host ("    Proxy:    http://localhost:" + $ProxyPort + "/health") -ForegroundColor Gray
Write-Host ("    Proxy(TS): http://" + $OperatorIp + ":" + $ProxyPort + "/health") -ForegroundColor Gray
Write-Host ""
Write-Host "  Notebook B side (시연 시 노트북 B에서 실행):" -ForegroundColor Cyan
Write-Host ("    Tailscale 동일 tailnet 로그인 확인") -ForegroundColor Gray
Write-Host ('    curl http://' + $OperatorIp + ':' + $ProxyPort + '/health  (응답 200 + {"status":"ok"} 기대)') -ForegroundColor Gray
Write-Host ("    .\start-realdevice-notebook-b.ps1 -OperatorIp " + $OperatorIp + "  (별도 스크립트 생성 필요)") -ForegroundColor Gray
Write-Host ""
Write-Host "  EMOTIV 페어링 (사용자 직접):" -ForegroundColor Cyan
Write-Host ("    1. EMOTIV Launcher 기동 (이미 떠 있으면 skip)") -ForegroundColor Gray
Write-Host ("    2. USB 동글 연결 + 헤드셋 페어링") -ForegroundColor Gray
Write-Host ("    3. Cortex API port " + $EmotivCortexPort + " listening 확인") -ForegroundColor Gray
Write-Host ""
Write-Host "=== All services healthy ===" -ForegroundColor Green

# 상태 대시보드 자동 오픈 (BE static)
$dashboardUrl = "http://localhost:$BackendPort/dashboard.html"
Write-Host ("  상태 대시보드: " + $dashboardUrl) -ForegroundColor Cyan
Start-Process $dashboardUrl
Write-Host ""
