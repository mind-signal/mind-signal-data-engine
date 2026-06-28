# Mind Signal Phase 18.1 dual-2pc notebook B starter
# DE_B 만 기동. BE/Proxy/Redis 는 operator PC 의존 (Tailscale 경유).
# Idempotent. operator PC start-realdevice-dual-2pc.ps1 GREEN 이후 실행.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-notebook-b.ps1
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-notebook-b.ps1 -OperatorIp 100.117.42.107
#   powershell -ExecutionPolicy Bypass -File .\start-realdevice-notebook-b.ps1 -SubjectIndex 2 -Production
#
# Stop: stop-realdevice-notebook-b.ps1
#
# References:
# - operator PC starter: start-realdevice-dual-2pc.ps1
# - 2026-05-27 cross-machine Tailscale GREEN [[project_mind_signal_phase18_mcafee_removed_cross_machine_green]]
# - [[feedback_powershell_start_process_env_inject]] $env: 사전 set 패턴

[CmdletBinding()]
param(
    [string]$ProjectRoot = "C:\Users\gs071\Team-project",
    [string]$OperatorIp = "100.117.42.107",
    [int]$BackendPort = 5000,
    [int]$DePort = 5002,
    [int]$ProxyPort = 5050,
    [string]$CondaEnv = "mind-signal",
    [string]$EngineSecret = "",
    [ValidateSet(1, 2)]
    [int]$SubjectIndex = 2,
    [string]$DualGroupId = "",
    [switch]$Production,
    [int]$HealthcheckTimeoutSec = 60,
    [int]$ReachabilityTimeoutSec = 10,
    [int]$EmotivCortexPort = 6868
)

$ErrorActionPreference = "Stop"

function Write-Step  { param($S,$M) Write-Host "[$S] $M" -ForegroundColor Yellow }
function Write-OK    { param($M) Write-Host "  $M" -ForegroundColor Green }
function Write-Warn2 { param($M) Write-Host "  $M" -ForegroundColor DarkYellow }
function Write-Fail  { param($M) Write-Host "  $M" -ForegroundColor Red }

function Get-NotebookBTailscaleIp {
    try {
        $ip = (& "C:\Program Files\Tailscale\tailscale.exe" ip -4 2>$null | Select-Object -First 1).Trim()
        if ($ip -and $ip -match '^\d+\.\d+\.\d+\.\d+$') { return $ip }
    } catch { }
    return ""
}

function Wait-HttpHealthy {
    param([string]$Url, [int]$TimeoutSec, [string]$ServiceName)
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
        } catch { }
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

function Test-TailscalePing {
    param([string]$PeerIp, [int]$TimeoutSec)
    try {
        $output = & "C:\Program Files\Tailscale\tailscale.exe" ping --timeout="${TimeoutSec}s" --c=1 $PeerIp 2>&1
        if ($output -match "^pong from") { return $true }
    } catch { }
    return $false
}

# 0. Pre-flight
Write-Host ""
Write-Host "=== Mind Signal Realdevice Notebook B Starter (DE_B only) ===" -ForegroundColor Cyan
Write-Host ("ProjectRoot:    " + $ProjectRoot) -ForegroundColor Gray
Write-Host ("OperatorIp:     " + $OperatorIp) -ForegroundColor Gray
Write-Host ("SubjectIndex:   " + $SubjectIndex) -ForegroundColor Gray
if ($DualGroupId) { Write-Host ("DualGroupId:    " + $DualGroupId + " (즉시 DUAL_2PC 등록 분기)") -ForegroundColor Gray }
else { Write-Host ("DualGroupId:    (미설정 - pending 분기, /control/assign-group 대기)") -ForegroundColor Gray }
Write-Host ("Production:     " + $Production) -ForegroundColor Gray
Write-Host ""

# Resolve ENGINE_SECRET_KEY: param > env var (notebook B has no BE .env.local)
if (-not $EngineSecret) { $EngineSecret = $env:ENGINE_SECRET_KEY }
if (-not $EngineSecret) {
    Write-Host "  ENGINE_SECRET_KEY not found. Pass -EngineSecret or set `$env:ENGINE_SECRET_KEY before running." -ForegroundColor Red
    exit 2
}

Write-Step "0/7" "Pre-flight (Tailscale + operator reachability + conda env)"

# 0a. Tailscale running
$tsService = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
if (-not $tsService -or $tsService.Status -ne "Running") {
    Write-Fail "Tailscale service not running. Start Tailscale UI first."
    exit 2
}
Write-OK "Tailscale service Running"

# 0b. Notebook B own Tailscale IP
$selfIp = Get-NotebookBTailscaleIp
if (-not $selfIp) {
    Write-Fail "Notebook B Tailscale IP resolve failed. Login to same tailnet first."
    exit 2
}
Write-OK ("Notebook B Tailscale IP = " + $selfIp)

# 0c. Same tailnet check via ping
Write-OK "Pinging operator PC via Tailscale..."
if (-not (Test-TailscalePing -PeerIp $OperatorIp -TimeoutSec $ReachabilityTimeoutSec)) {
    Write-Fail ("Tailscale ping to " + $OperatorIp + " failed. Same tailnet (account) 확인 필요.")
    exit 2
}
Write-OK ("Tailscale ping " + $OperatorIp + " GREEN")

# 0d. Operator Proxy reachable
$proxyUrl = "http://${OperatorIp}:$ProxyPort"
$proxyReachable = Wait-HttpHealthy -Url ($proxyUrl + "/health") -TimeoutSec 5 -ServiceName "OperatorProxy"
if (-not $proxyReachable) {
    Write-Fail ("Operator proxy not reachable at " + $proxyUrl + ". start-realdevice-dual-2pc.ps1 GREEN 인지 확인.")
    exit 2
}

# 0e. Operator BE reachable (Swagger UI proxy 미사용 시 backup)
$beUrl = "http://${OperatorIp}:$BackendPort"
$beReachable = Wait-HttpHealthy -Url ($beUrl + "/api-docs") -TimeoutSec 5 -ServiceName "OperatorBE"
if (-not $beReachable) {
    Write-Warn2 ("Operator BE not reachable at " + $beUrl + ". proxy mode 만 검증 됨. BE 직접 호출 시 fail 위험.")
}

# 0f. Third-party AV check
$avBlockers = @("mc-fw-host", "mcshield", "norton", "navapsvc", "ahnlab")
$avFound = Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $svc = $_
    $avBlockers | Where-Object { $svc.Name -like "*$_*" -or $svc.DisplayName -like "*$_*" } | Select-Object -First 1
}
if ($avFound) {
    Write-Warn2 "Third-party AV/Firewall detected. May block outbound TCP. See [[feedback_windows_third_party_av_firewall_layer]]"
    $avFound | ForEach-Object { Write-Warn2 ("  - " + $_.DisplayName + " (" + $_.Status + ")") }
} else {
    Write-OK "No third-party AV firewall blockers detected"
}

# 0g. Conda env
$condaCandidates = @(
    "C:\Users\gs071\.conda\envs\$CondaEnv",
    "C:\Users\gs071\miniconda3\envs\$CondaEnv",
    "$env:USERPROFILE\.conda\envs\$CondaEnv",
    "$env:USERPROFILE\miniconda3\envs\$CondaEnv"
)
$condaPath = $condaCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $condaPath) {
    Write-Fail ("Conda env not found in any of: " + ($condaCandidates -join ", "))
    exit 2
}
Write-OK ("Conda env $CondaEnv at " + $condaPath)

# 0h. DE_A project dir
$deDir = Join-Path $ProjectRoot "mind-signal-data-engine"
if (-not (Test-Path $deDir)) {
    Write-Fail ("Data engine directory not found: " + $deDir + ". git clone or update -ProjectRoot")
    exit 2
}
Write-OK ("Data engine dir at " + $deDir)

# 0i. EMOTIV launcher (warn only)
if (Test-PortInUse -Port $EmotivCortexPort) {
    Write-OK ("EMOTIV Cortex listening on port " + $EmotivCortexPort)
} else {
    Write-Warn2 ("EMOTIV Cortex (port " + $EmotivCortexPort + ") not running. EMOTIV Launcher + 헤드셋 페어링 필요.")
}

Write-Host ""

# 1. Env injection via $env: (자식 cmd 가 부모 PowerShell env 상속함)
# 함정 우회: cmd /k 안의 "set X=val && python ..." 패턴은 자식 process 까지 env 전달이 불완전한 사례 박제됨
# 참고: [[feedback_powershell_start_process_env_inject]]
Write-Step "1/7" "Inject environment variables (PowerShell scope)"
$env:ENGINE_SECRET_KEY = $EngineSecret
$env:LAN_IP = $selfIp
$env:PROXY_URL = $proxyUrl
$env:BACKEND_URL = $beUrl
$env:ALIGNMENT_LOCATION = "proxy"
$env:REGISTRATION_MODE = "local"
$env:DUAL_2PC_SUBJECT_INDEX = "$SubjectIndex"
if ($DualGroupId) { $env:DUAL_2PC_GROUP_ID = $DualGroupId }
if ($Production) { $env:UVICORN_RELOAD = "false" }
Write-OK "Injected: ENGINE_SECRET_KEY LAN_IP PROXY_URL BACKEND_URL ALIGNMENT_LOCATION REGISTRATION_MODE DUAL_2PC_SUBJECT_INDEX"

Write-Host ""

# 2. DE_B
Write-Step "2/7" ("Data Engine B (port " + $DePort + ", subject_index=" + $SubjectIndex + ")")
if (Test-PortInUse -Port $DePort) {
    Write-Warn2 ("Port " + $DePort + " already in use. Assuming DE_B already running.")
} else {
    $deCmd = "conda activate $CondaEnv && python run_server.py"
    $deArgs = "/k cd /d `"$deDir`" && $deCmd"
    Start-Process -FilePath "cmd.exe" -ArgumentList $deArgs -WindowStyle Normal
    Write-OK "DE_B terminal launched (env inherited from PowerShell scope)"
}

Write-Host ""

# 3. DE_B local healthcheck
Write-Step "3/7" "DE_B healthcheck (GET /health)"
$deHealthy = Wait-HttpHealthy -Url "http://localhost:$DePort/health" -TimeoutSec $HealthcheckTimeoutSec -ServiceName "DE_B"
if (-not $deHealthy) {
    Write-Fail "DE_B failed to become healthy. Check DE_B terminal output."
    exit 5
}

Write-Host ""

# 4. DE_B Tailscale-side reachability (cross-machine view from operator PC 측 시뮬레이션)
Write-Step "4/7" "DE_B reachable via Tailscale IP"
$deTsHealthy = Wait-HttpHealthy -Url "http://${selfIp}:$DePort/health" -TimeoutSec 10 -ServiceName "DE_B-via-Tailscale"
if (-not $deTsHealthy) {
    Write-Warn2 "DE_B not reachable via Tailscale IP. Operator PC 측 ingest 시 fail 가능. firewall/listen address 확인 필요."
} else {
    Write-OK ("DE_B Tailscale endpoint = http://" + $selfIp + ":" + $DePort + "/health")
}

Write-Host ""

# 5. Proxy 등록 confirm (proxy mode 분기 DE_B → proxy /register POST)
# DE_B startup 시 자동 register_to_proxy 호출 (app.py L102+). 등록 결과는 proxy 측 endpoint 로 직접 확인 어려움.
# 대안: 잠시 대기 후 DE_B stdout 에 "proxy 등록" 메시지 확인 (사용자 위임).
Write-Step "5/7" "Proxy registration (DE_B auto-registers to operator proxy)"
Start-Sleep -Seconds 3
Write-OK ("DE_B 가 " + $proxyUrl + "/register 에 자동 등록 시도함 (retry 3회). DE_B 터미널의 'proxy registration' 로그 확인 필요.")

Write-Host ""

# 6. EMOTIV pairing reminder
Write-Step "6/7" "EMOTIV pairing check"
if (Test-PortInUse -Port $EmotivCortexPort) {
    Write-OK "EMOTIV Cortex 이미 listening. 헤드셋 페어링 상태 확인 필요 (Launcher UI)."
} else {
    Write-Warn2 "EMOTIV Cortex 미기동. Launcher 실행 + 노트북 B 측 EMOTIV USB 동글 + 헤드셋 페어링 필요."
}

Write-Host ""

# 7. Status report
Write-Step "7/7" "Status report"
Write-Host ""
Write-Host "  Notebook B endpoints:" -ForegroundColor Cyan
Write-Host ("    DE_B (local):    http://localhost:" + $DePort + "/health") -ForegroundColor Gray
Write-Host ("    DE_B (Tailscale): http://" + $selfIp + ":" + $DePort + "/health") -ForegroundColor Gray
Write-Host ""
Write-Host "  Operator PC dependencies (read-only check):" -ForegroundColor Cyan
Write-Host ("    Operator Proxy:  " + $proxyUrl + "/health") -ForegroundColor Gray
Write-Host ("    Operator BE:     " + $beUrl + "/api-docs") -ForegroundColor Gray
Write-Host ""
Write-Host "  Phase 18 dual-2pc routing:" -ForegroundColor Cyan
Write-Host ("    DE_B subject_index = " + $SubjectIndex) -ForegroundColor Gray
Write-Host ("    DE_B registered to: " + $proxyUrl + "/register") -ForegroundColor Gray
if ($DualGroupId) {
    Write-Host ("    DUAL_2PC group_id = " + $DualGroupId + " (immediate 분기)") -ForegroundColor Gray
} else {
    Write-Host ("    Pending mode: DE_B awaits POST /control/assign-group from operator") -ForegroundColor Gray
}
Write-Host ""
Write-Host "  Next actions (manual):" -ForegroundColor Cyan
Write-Host ("    1. EMOTIV Launcher 띄움 + 노트북 B 헤드셋 페어링 확인") -ForegroundColor Gray
Write-Host ("    2. DE_B 터미널의 'proxy registration' 로그 확인 (3 회 retry 안에 GREEN 인지)") -ForegroundColor Gray
Write-Host ("    3. Operator PC FE 에서 measurement 시작 -> dual-2pc 흐름 검증") -ForegroundColor Gray
Write-Host ""
Write-Host "=== Notebook B DE_B healthy ===" -ForegroundColor Green
Write-Host ""
