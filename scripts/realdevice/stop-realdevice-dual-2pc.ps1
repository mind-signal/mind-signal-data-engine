# Mind Signal Phase 18.1 dual-2pc stopper (operator PC)
# Pair with start-realdevice-dual-2pc.ps1
# Kills processes by port (BE 5000 / DE_A 5002 / Proxy 5050) and optionally Redis container.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\stop-realdevice-dual-2pc.ps1
#   powershell -ExecutionPolicy Bypass -File .\stop-realdevice-dual-2pc.ps1 -KeepRedis
#   powershell -ExecutionPolicy Bypass -File .\stop-realdevice-dual-2pc.ps1 -KeepProxy

[CmdletBinding()]
param(
    [int]$BackendPort = 5000,
    [int]$DePort = 5002,
    [int]$ProxyPort = 5050,
    [switch]$KeepRedis,
    [switch]$KeepProxy
)

function Stop-ByPort {
    param([int]$Port, [string]$Name)
    $conns = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Host ("  " + $Name + " not listening on " + $Port) -ForegroundColor DarkGray
        return
    }
    foreach ($c in $conns) {
        try {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop
            Write-Host ("  Killed " + $Name + " (PID " + $c.OwningProcess + " port " + $Port + ")") -ForegroundColor Green
        } catch {
            Write-Host ("  Failed to kill PID " + $c.OwningProcess + ": " + $_.Exception.Message) -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "=== Mind Signal Realdevice Dual-2PC Stopper ===" -ForegroundColor Cyan
Write-Host ""

Stop-ByPort -Port $BackendPort -Name "Backend"
Stop-ByPort -Port $DePort -Name "DE_A"
if (-not $KeepProxy) {
    Stop-ByPort -Port $ProxyPort -Name "Proxy"
} else {
    Write-Host "  Proxy kept (KeepProxy flag)" -ForegroundColor DarkYellow
}

if (-not $KeepRedis) {
    docker stop mind-signal-redis 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Stopped mind-signal-redis container" -ForegroundColor Green
    } else {
        Write-Host "  Redis container not running" -ForegroundColor DarkGray
    }
} else {
    Write-Host "  Redis kept (KeepRedis flag)" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "=== Stop done ===" -ForegroundColor Green
