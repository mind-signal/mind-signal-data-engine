# Mind Signal Phase 18.1 dual-2pc notebook B stopper
# DE_B 만 stop. operator PC 서비스는 별개로 stop.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\stop-realdevice-notebook-b.ps1

[CmdletBinding()]
param(
    [int]$DePort = 5002
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

function Stop-PythonRunServer {
    # uvicorn reload=True 일 때 부모 process kill 후 child python 잔존 사례 우회 (실측 2026-05-27).
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq "python.exe" -and $_.CommandLine -match "run_server\.py"
    }
    if (-not $procs) {
        Write-Host "  No residual python run_server.py process" -ForegroundColor DarkGray
        return
    }
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host ("  Killed residual python (PID " + $p.ProcessId + ")") -ForegroundColor Green
        } catch {
            Write-Host ("  Failed to kill PID " + $p.ProcessId + ": " + $_.Exception.Message) -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "=== Mind Signal Realdevice Notebook B Stopper ===" -ForegroundColor Cyan
Write-Host ""

Stop-ByPort -Port $DePort -Name "DE_B"
Stop-PythonRunServer

Write-Host ""
Write-Host "=== Stop done ===" -ForegroundColor Green
