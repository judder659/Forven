# Forven Service Watchdog
# Run as a Windows Scheduled Task to auto-restart services if they go down.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File watchdog.ps1
#
# Create a Scheduled Task that runs every 2 minutes:
#   schtasks /create /tn "ForvenWatchdog" /tr "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\path\to\Forven\watchdog.ps1" /sc minute /mo 2 /rl highest
# Then mark the task itself Hidden in Task Scheduler (or via Set-ScheduledTask) to avoid visible shell popups.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:WatchdogOwnerLockStream = $null
$script:WatchdogOwnerName = $null
$script:WatchdogOwnerAcquiredAt = $null
$logDir = Join-Path (Join-Path $RepoRoot ".tmp") "logs"
$LogFile = Join-Path $logDir "watchdog.log"

function Write-Log {
    param([string]$m)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $m"
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue } catch {}
}

if (-not (Test-Path $logDir)) { New-Item -Path $logDir -ItemType Directory -Force | Out-Null }

$BackendPort = 8003
if (-not [string]::IsNullOrWhiteSpace($env:FORVEN_PORT)) { $BackendPort = [int]$env:FORVEN_PORT }
$BackendHost = if (-not [string]::IsNullOrWhiteSpace($env:FORVEN_BIND_HOST)) {
    $env:FORVEN_BIND_HOST.Trim()
} elseif (-not [string]::IsNullOrWhiteSpace($env:FORVEN_HOST)) {
    $env:FORVEN_HOST.Trim()
} else {
    "127.0.0.1"
}
$FrontendPort = 5173
if (-not [string]::IsNullOrWhiteSpace($env:VITE_PORT)) { $FrontendPort = [int]$env:VITE_PORT }
$HealthUrl = "http://127.0.0.1:${BackendPort}/api/health"

function Test-HttpHealthy {
    param([string]$Url)
    try {
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-ListeningPids {
    param([int]$Port)
    $result = @()
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($conns) {
            $result = @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
        }
    } catch {}
    return $result
}

function Get-BackendProcessIds {
    # Every backend process for THIS repo, listener or not. A backend whose main
    # thread died closes its listener but can survive as a zombie (background
    # threads wedge interpreter teardown) while still holding the runtime-worker
    # and daemon file locks - killing only the listening PIDs leaves it alive and
    # the replacement backend then boots with no background loops.
    $result = @()
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
            Where-Object {
                $cmd = [string]$_.CommandLine
                $cmd -match "forven\.api" -and $cmd.ToLowerInvariant().Contains($RepoRoot.ToLowerInvariant())
            }
        foreach ($proc in @($procs)) {
            if ($proc -and $proc.ProcessId) { $result += [int]$proc.ProcessId }
        }
    } catch {}
    return @($result | Select-Object -Unique)
}

function Stop-BackendProcesses {
    param([int[]]$ListenerPids)

    $targets = @((@($ListenerPids) + @(Get-BackendProcessIds)) | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    foreach ($procId in $targets) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Log ("Stopped backend PID " + $procId)
        } catch {
            if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
                Write-Log ("WARN: Could not stop backend PID " + $procId + ": " + $_.Exception.Message)
            }
        }
    }
}

function Find-Python {
    $venvPy = Join-Path (Join-Path $RepoRoot ".venv") "Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    $sysPy = Get-Command python -ErrorAction SilentlyContinue
    if ($sysPy) { return $sysPy.Source }
    return $null
}

function Get-WatchdogOwnerLockPath {
    return Join-Path (Join-Path $RepoRoot ".tmp") "watchdog.owner.lock"
}

function Test-RunningProcessId {
    param([object]$ProcessId)

    try {
        $normalized = [int]$ProcessId
    } catch {
        return $false
    }
    if ($normalized -le 0) { return $false }
    try {
        $null = Get-Process -Id $normalized -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Read-WatchdogOwnerPayload {
    $lockPath = Get-WatchdogOwnerLockPath
    if (-not (Test-Path $lockPath)) { return $null }
    try {
        $raw = (Get-Content -Path $lockPath -Raw -ErrorAction Stop).Trim()
    } catch {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        try {
            return [pscustomobject]@{ pid = [int]$raw }
        } catch {
            return $null
        }
    }
}

function Get-WatchdogOwnerStatus {
    $lockPath = Get-WatchdogOwnerLockPath
    $heldByCurrentProcess = $null -ne $script:WatchdogOwnerLockStream
    $payload = if ($heldByCurrentProcess) { $null } else { Read-WatchdogOwnerPayload }
    $activePid = if ($heldByCurrentProcess) { $PID } elseif ($null -ne $payload -and $null -ne $payload.pid) { [int]$payload.pid } else { 0 }
    $ownerName = if ($heldByCurrentProcess) { $script:WatchdogOwnerName } elseif ($null -ne $payload -and $null -ne $payload.owner_name) { [string]$payload.owner_name } else { $null }
    $acquiredAt = if ($heldByCurrentProcess) { $script:WatchdogOwnerAcquiredAt } elseif ($null -ne $payload -and $null -ne $payload.acquired_at) { [string]$payload.acquired_at } else { $null }
    $activePidRunning = if ($heldByCurrentProcess) { $true } else { Test-RunningProcessId -ProcessId $activePid }
    if ($heldByCurrentProcess) {
        $lockHeld = $true
    } else {
        try {
            $probe = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $probe.Dispose()
            $lockHeld = $false
        } catch {
            $lockHeld = $true
        }
    }
    $stalePid = ($activePid -gt 0) -and (-not $activePidRunning)
    $otherProcessActive = $lockHeld -and $activePidRunning -and $activePid -ne $PID
    return [pscustomobject]@{
        lock_path = $lockPath
        active_pid = if ($activePid -gt 0) { $activePid } else { $null }
        active_pid_running = [bool]$activePidRunning
        lock_held = [bool]$lockHeld
        held_by_current_process = [bool]$heldByCurrentProcess
        other_process_active = [bool]$otherProcessActive
        stale_pid = [bool]$stalePid
        owner_name = $ownerName
        acquired_at = $acquiredAt
    }
}

function Acquire-WatchdogOwnerLock {
    param([string]$OwnerName)

    if ($null -ne $script:WatchdogOwnerLockStream) {
        return [pscustomobject]@{ claimed = $true; status = Get-WatchdogOwnerStatus }
    }

    $lockPath = Get-WatchdogOwnerLockPath
    $lockDir = Split-Path -Parent $lockPath
    New-Item -Path $lockDir -ItemType Directory -Force | Out-Null

    $status = Get-WatchdogOwnerStatus
    if ([bool]$status.other_process_active) {
        return [pscustomobject]@{ claimed = $false; status = $status }
    }
    if ([bool]$status.stale_pid -and (Test-Path $lockPath)) {
        try { Remove-Item -Path $lockPath -Force -ErrorAction Stop } catch {}
    }

    try {
        $stream = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::Read)
    } catch {
        return [pscustomobject]@{ claimed = $false; status = Get-WatchdogOwnerStatus }
    }

    $payload = [pscustomobject]@{
        pid = $PID
        owner_name = if ([string]::IsNullOrWhiteSpace($OwnerName)) { "watchdog.ps1" } else { $OwnerName }
        acquired_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress))
    $stream.SetLength(0)
    $stream.Position = 0
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()

    $script:WatchdogOwnerLockStream = $stream
    $script:WatchdogOwnerName = [string]$payload.owner_name
    $script:WatchdogOwnerAcquiredAt = [string]$payload.acquired_at
    return [pscustomobject]@{ claimed = $true; status = Get-WatchdogOwnerStatus }
}

function Release-WatchdogOwnerLock {
    if ($null -eq $script:WatchdogOwnerLockStream) { return }
    try {
        $script:WatchdogOwnerLockStream.SetLength(0)
    } catch {}
    try {
        $script:WatchdogOwnerLockStream.Dispose()
    } catch {}
    $script:WatchdogOwnerLockStream = $null
    $script:WatchdogOwnerName = $null
    $script:WatchdogOwnerAcquiredAt = $null
}

function Get-BotHealthSnapshot {
    param([string]$PythonPath)

    $script = @'
import json
from datetime import datetime, timezone

from forven.db import get_db, kv_get


def parse_ts(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


now = datetime.now(timezone.utc)
last_success = parse_ts(kv_get('scheduler:last_successful_tick'))
last_error = str(kv_get('scheduler:last_error') or '')
last_error_at = parse_ts(kv_get('scheduler:last_error_at'))

with get_db() as conn:
    stuck_job_count = int(conn.execute(
        "SELECT COUNT(*) FROM scheduler_jobs WHERE running_since IS NOT NULL AND TRIM(running_since) != ''"
    ).fetchone()[0])
    hard_timeout_job_count = int(conn.execute(
        "SELECT COUNT(*) FROM scheduler_jobs "
        "WHERE enabled = 1 AND last_status = 'error' AND COALESCE(last_error, '') LIKE 'Hard timeout exceeded (%'"
    ).fetchone()[0])

snapshot = {
    "last_successful_tick": last_success.isoformat() if last_success else None,
    "last_success_age_seconds": None if last_success is None else max(0, int((now - last_success).total_seconds())),
    "last_error": last_error,
    "last_error_at": last_error_at.isoformat() if last_error_at else None,
    "last_error_age_seconds": None if last_error_at is None else max(0, int((now - last_error_at).total_seconds())),
    "stuck_job_count": stuck_job_count,
    "hard_timeout_job_count": hard_timeout_job_count,
}
print(json.dumps(snapshot))
'@

    try {
        $json = $script | & $PythonPath -
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($json -join ""))) {
            return $null
        }
        return (($json -join "") | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-BotProcessIds {
    param([string]$LockFilePath)

    $ids = New-Object System.Collections.Generic.HashSet[int]
    if (Test-Path $LockFilePath) {
        try {
            $botPid = [int](Get-Content $LockFilePath -ErrorAction SilentlyContinue).Trim()
            if ($botPid -gt 0) { [void]$ids.Add($botPid) }
        } catch {}
    }

    try {
        $botProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "run_bot" }
        foreach ($proc in @($botProcs)) {
            if ($proc -and $proc.ProcessId) { [void]$ids.Add([int]$proc.ProcessId) }
        }
    } catch {}

    return @($ids)
}

function Stop-BotProcesses {
    param([int[]]$ProcessIds)

    $stoppedAll = $true
    foreach ($pid in @($ProcessIds | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)) {
        try {
            Stop-Process -Id $pid -Force -ErrorAction Stop
            Write-Log ("Stopped bot PID " + $pid)
        } catch {
            Write-Log ("WARN: Could not stop bot PID " + $pid + ": " + $_.Exception.Message)
            $stoppedAll = $false
        }
    }
    return $stoppedAll
}

$python = Find-Python
if (-not $python) {
    Write-Log "ERROR: Python not found."
    exit 1
}

if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $RepoRoot
} else {
    $env:PYTHONPATH = "$RepoRoot;$env:PYTHONPATH"
}
if ([string]::IsNullOrWhiteSpace($env:FORVEN_HOME)) {
    $env:FORVEN_HOME = Join-Path $env:USERPROFILE ".forven"
}

$logRoot = Join-Path (Join-Path $RepoRoot ".tmp") "logs"
$restarted = @()
$watchdogOwnerLockHeld = $false

try {
    $watchdogClaim = Acquire-WatchdogOwnerLock -OwnerName "watchdog.ps1"
    if ($null -eq $watchdogClaim -or -not [bool]$watchdogClaim.claimed) {
        $watchdogStatus = if ($null -ne $watchdogClaim) { $watchdogClaim.status } else { $null }
        $activeOwnerName = if ($null -ne $watchdogStatus -and $null -ne $watchdogStatus.owner_name) { [string]$watchdogStatus.owner_name } else { "watchdog" }
        $activeOwnerPid = if ($null -ne $watchdogStatus -and $null -ne $watchdogStatus.active_pid) { [int]$watchdogStatus.active_pid } else { 0 }
        if ($null -ne $watchdogStatus -and [bool]$watchdogStatus.other_process_active) {
            Write-Log ("Another watchdog owner is active (" + $activeOwnerName + " PID " + $activeOwnerPid + ") - exiting")
            exit 0
        }
        if ($null -ne $watchdogStatus -and [bool]$watchdogStatus.lock_held) {
            Write-Log "Another watchdog owner appears active, but owner metadata is unavailable - exiting"
            exit 0
        }
        Write-Log "ERROR: Could not acquire watchdog owner lock."
        exit 1
    }
    $watchdogOwnerLockHeld = $true

    # --- Restart sentinel (self-update / operator-requested bounce) ---
    # start_all's supervisor loop honors .tmp/restart.request; honor it here too
    # so a restart requested while only the scheduled-task watchdog is running
    # (no start_all console) still lands.
    $restartSentinel = Join-Path (Join-Path $RepoRoot ".tmp") "restart.request"
    $backendRestartForced = $false
    if (Test-Path $restartSentinel) {
        Write-Log "Restart sentinel found - bouncing backend to load new code."
        try { Remove-Item -Path $restartSentinel -Force -ErrorAction Stop } catch {
            Write-Log ("WARN: Could not remove restart sentinel: " + $_.Exception.Message)
        }
        $backendRestartForced = $true
    }

    # --- Check Backend ---
    [array]$backendListeners = @(Get-ListeningPids -Port $BackendPort)
    $backendHealthy = Test-HttpHealthy -Url $HealthUrl

    # A listening backend that misses ONE 5s health probe is almost always mid-job
    # (boot catch-up sweeps and gauntlet compute hold the GIL for seconds); killing
    # it on the first miss caused restart storms (2026-07-19 06:15/06:20/06:21 and
    # the 15:49 kill of a 4-minute-old backend). Each watchdog run is a separate
    # scheduled-task process, so tolerance must persist in a counter file. A true
    # zombie has NO listener and still restarts immediately.
    $probeFailFile = Join-Path (Join-Path $RepoRoot ".tmp") "watchdog.backend_probe_failures"
    $probeFailures = 0
    if (Test-Path $probeFailFile) {
        try { $probeFailures = [int]((Get-Content $probeFailFile -ErrorAction Stop) -join "").Trim() } catch {}
    }
    $probeFailureLimit = 3
    $backendNeedsRestart = $backendRestartForced -or $backendListeners.Count -eq 0
    if (-not $backendNeedsRestart) {
        if ($backendHealthy) {
            if ($probeFailures -ne 0) { Remove-Item -Path $probeFailFile -Force -ErrorAction SilentlyContinue }
        } else {
            $probeFailures += 1
            if ($probeFailures -ge $probeFailureLimit) {
                $backendNeedsRestart = $true
            } else {
                try { Set-Content -Path $probeFailFile -Value $probeFailures -ErrorAction Stop } catch {}
                Write-Log ("Backend health probe failed (" + $probeFailures + "/" + $probeFailureLimit + ") but a listener is up - likely a heavy job; not restarting yet.")
            }
        }
    }
    if ($backendNeedsRestart) {
        Remove-Item -Path $probeFailFile -Force -ErrorAction SilentlyContinue
        $msg = if ($backendRestartForced) {
            "Backend restart requested via sentinel - restarting"
        } elseif ($backendListeners.Count -eq 0) {
            "Backend DOWN (no listener, healthy=" + $backendHealthy + ") - restarting"
        } else {
            "Backend hung (" + $probeFailures + " consecutive failed probes with a live listener) - restarting"
        }
        Write-Log $msg
        Stop-BackendProcesses -ListenerPids $backendListeners
        $backendLog = Join-Path $logRoot "unified_backend.log"
        $backendErr = Join-Path $logRoot "unified_backend.err.log"
        $proc = Start-Process -FilePath $python `
            -ArgumentList @("-m","forven.api","--port",$BackendPort.ToString()) `
            -WorkingDirectory $RepoRoot -RedirectStandardOutput $backendLog -RedirectStandardError $backendErr `
            -WindowStyle Hidden -PassThru
        Write-Log ("Backend started as PID " + $proc.Id)
        $restarted += "backend"
        Start-Sleep -Seconds 5
    }

# --- Check Bot ---
$botAlive = $false
$botHealthy = $true
$botHealthReason = $null
# First check by lock file
$botLockFile = Join-Path $env:FORVEN_HOME "bot.lock"
if (Test-Path $botLockFile) {
    try {
        $botPid = [int](Get-Content $botLockFile -ErrorAction SilentlyContinue).Trim()
        $botAlive = $null -ne (Get-Process -Id $botPid -ErrorAction SilentlyContinue)
    } catch {}
}
# Fallback: check by command line pattern (lock file may be locked by active process)
if (-not $botAlive) {
    try {
        $botProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "run_bot" }
        if ($botProcs) { $botAlive = $true }
    } catch {}
}
$botHealth = Get-BotHealthSnapshot -PythonPath $python
if ($botAlive -and $null -ne $botHealth) {
    if ($botHealth.last_success_age_seconds -is [int] -and $botHealth.last_success_age_seconds -gt 300) {
        $botHealthy = $false
        $botHealthReason = "scheduler heartbeat stale"
    } elseif (
        -not [string]::IsNullOrWhiteSpace([string]$botHealth.last_error) -and
        [string]$botHealth.last_error -like "*Scheduler tick exceeded 25s hard timeout*" -and
        $botHealth.last_error_age_seconds -is [int] -and
        $botHealth.last_error_age_seconds -lt 900
    ) {
        $botHealthy = $false
        $botHealthReason = "scheduler stuck in 25s timeout loop"
    } elseif (
        $botHealth.stuck_job_count -is [int] -and
        $botHealth.hard_timeout_job_count -is [int] -and
        $botHealth.stuck_job_count -ge 3 -and
        $botHealth.hard_timeout_job_count -ge 3
    ) {
        $botHealthy = $false
        $botHealthReason = "scheduler jobs are stuck in hard-timeout state"
    }
}

if ($botAlive -and -not $botHealthy) {
    Write-Log ("Bot unhealthy (" + $botHealthReason + ") - restarting")
    $stopped = Stop-BotProcesses -ProcessIds (Get-BotProcessIds -LockFilePath $botLockFile)
    if (Test-Path $botLockFile) { Remove-Item $botLockFile -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    $botAlive = $false
    if (-not $stopped) {
        Write-Log "WARN: Some bot processes could not be stopped; attempting clean restart anyway."
    }
}

if (-not $botAlive) {
    $configPath = Join-Path $env:FORVEN_HOME "config.json"
    $tokenOk = $false
    if (Test-Path $configPath) {
        try {
            $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
            $tokenOk = -not [string]::IsNullOrWhiteSpace([string]$cfg.discord_token)
        } catch {}
    }
    if (-not [string]::IsNullOrWhiteSpace($env:DISCORD_TOKEN)) { $tokenOk = $true }

    if ($tokenOk) {
        if (Test-Path $botLockFile) { Remove-Item $botLockFile -Force -ErrorAction SilentlyContinue }
        $botLog = Join-Path $logRoot "forven_bot.log"
        $botErr = Join-Path $logRoot "forven_bot.err.log"
        $proc = Start-Process -FilePath $python -ArgumentList "-c `"from forven.bot import run_bot; run_bot()`"" `
            -WorkingDirectory $RepoRoot -RedirectStandardOutput $botLog -RedirectStandardError $botErr `
            -WindowStyle Hidden -PassThru
        Write-Log ("Bot started as PID " + $proc.Id)
        $restarted += "bot"
    }
}

# --- Check Daemon ---
$daemonAlive = $false
$daemonLockFile = Join-Path $env:FORVEN_HOME "daemon.lock"
if (Test-Path $daemonLockFile) {
    try {
        $daemonPid = [int](Get-Content $daemonLockFile -ErrorAction SilentlyContinue).Trim()
        $daemonAlive = $null -ne (Get-Process -Id $daemonPid -ErrorAction SilentlyContinue)
    } catch {}
}
if (-not $daemonAlive) {
    try {
        $daemonProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "forven.*daemon" }
        if ($daemonProcs) { $daemonAlive = $true }
    } catch {}
}
if (-not $daemonAlive -and (Test-Path $daemonLockFile)) {
    # The backend hosts the daemon in-process (thread_mode): no standalone daemon
    # process exists, but the live daemon keeps an open handle on the lock file.
    # If we cannot open it exclusively, a daemon is alive somewhere. Without this
    # probe the watchdog spawned a doomed duplicate daemon every cycle ("Another
    # daemon instance is already running").
    try {
        $probe = [System.IO.File]::Open($daemonLockFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $probe.Dispose()
    } catch {
        $daemonAlive = $true
    }
}
if (-not $daemonAlive) {
    if (Test-Path $daemonLockFile) { Remove-Item $daemonLockFile -Force -ErrorAction SilentlyContinue }
    $daemonLog = Join-Path $logRoot "forven_daemon.log"
    $daemonErr = Join-Path $logRoot "forven_daemon.err.log"
    $proc = Start-Process -FilePath $python -ArgumentList @("-m","forven","daemon","start") `
        -WorkingDirectory $RepoRoot -RedirectStandardOutput $daemonLog -RedirectStandardError $daemonErr `
        -WindowStyle Hidden -PassThru
    Write-Log ("Daemon started as PID " + $proc.Id)
    $restarted += "daemon"
}

# --- Check Lab Worker (only if Regime Lab feature flag is enabled) ---
$regimeLabFlag = if (-not [string]::IsNullOrWhiteSpace($env:FORVEN_ENABLE_REGIME_LAB)) { $env:FORVEN_ENABLE_REGIME_LAB.Trim().ToLowerInvariant() } else { "" }
$regimeLabEnabled = @("1", "true", "yes", "on") -contains $regimeLabFlag
$labWorkerAlive = $false
try {
    $labProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "lab.*worker" }
    if ($labProcs) { $labWorkerAlive = $true }
} catch {}
if (-not $labWorkerAlive -and $regimeLabEnabled) {
    # Clear stale PID lock so worker can start cleanly
    $labPidFile = Join-Path (Join-Path $env:FORVEN_HOME "lab") "lab_worker.pid"
    if (Test-Path $labPidFile) { Remove-Item $labPidFile -Force -ErrorAction SilentlyContinue }
    $labWorkerLog = Join-Path $logRoot "forven_lab_worker.log"
    $labWorkerErr = Join-Path $logRoot "forven_lab_worker.err.log"
    $proc = Start-Process -FilePath $python -ArgumentList @("-m","forven","lab","worker") `
        -WorkingDirectory $RepoRoot -RedirectStandardOutput $labWorkerLog -RedirectStandardError $labWorkerErr `
        -WindowStyle Hidden -PassThru
    Write-Log ("Lab worker started as PID " + $proc.Id)
    $restarted += "lab_worker"
}

# --- Check Frontend ---
[array]$frontendListeners = @(Get-ListeningPids -Port $FrontendPort)
if ($frontendListeners.Count -eq 0) {
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($npmCmd) {
        $frontendLog = Join-Path $logRoot "unified_frontend.log"
        $frontendErr = Join-Path $logRoot "unified_frontend.err.log"
        $frontendDir = Join-Path $RepoRoot "frontend"
        $proc = Start-Process -FilePath $npmCmd.Source `
            -ArgumentList @("run","dev","--","--host","0.0.0.0","--port",$FrontendPort.ToString()) `
            -WorkingDirectory $frontendDir -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErr `
            -WindowStyle Hidden -PassThru
        Write-Log ("Frontend started as PID " + $proc.Id)
        $restarted += "frontend"
    }
}

# --- Check Pipeline Progress (detect frozen-but-alive) ---
# $labWorkerAlive already set above; refresh $labProcs if worker was just started
if (-not $labProcs) {
    try {
        $labProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "lab.*worker" }
        if ($labProcs) { $labWorkerAlive = $true }
    } catch {}
}

if ($labWorkerAlive -and $backendHealthy) {
    try {
        $progressJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$RepoRoot')
from forven.lab_db import get_lab_meta
p = get_lab_meta('pipeline_progress', {})
print(json.dumps(p if isinstance(p, dict) else {}))
"@ 2>$null
        if ($progressJson) {
            $progress = $progressJson | ConvertFrom-Json
            $now = [DateTimeOffset]::UtcNow
            $stale = $false
            $reason = ""
            $lastCompleted = $null
            $lastClaimed = $null

            if ($progress -and $progress.PSObject -and $progress.PSObject.Properties) {
                $lastCompletedProp = $progress.PSObject.Properties['last_job_completed_at']
                if ($lastCompletedProp -and -not [string]::IsNullOrWhiteSpace([string]$lastCompletedProp.Value)) {
                    try {
                        $lastCompleted = [DateTimeOffset]::Parse([string]$lastCompletedProp.Value)
                    } catch {}
                }

                $lastClaimedProp = $progress.PSObject.Properties['last_job_claimed_at']
                if ($lastClaimedProp -and -not [string]::IsNullOrWhiteSpace([string]$lastClaimedProp.Value)) {
                    try {
                        $lastClaimed = [DateTimeOffset]::Parse([string]$lastClaimedProp.Value)
                    } catch {}
                }
            }

            # Check last job completed: stale if > 45 min ago
            if ($lastCompleted) {
                $completedAge = ($now - $lastCompleted).TotalMinutes
                if ($completedAge -gt 45) {
                    $stale = $true
                    $reason = "No job completed in $([math]::Round($completedAge)) min"
                }
            }

            # Check last job claimed: stale if > 15 min ago AND completed is also stale
            if ($stale -and $lastClaimed) {
                $claimedAge = ($now - $lastClaimed).TotalMinutes
                if ($claimedAge -le 15) {
                    $stale = $false  # Recently claimed, may still be processing
                }
            }

            # Also check scheduler health
            $schedJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$RepoRoot')
from forven.db import kv_get
tick = kv_get('scheduler:last_successful_tick', '')
errs = kv_get('scheduler:consecutive_errors', 0)
print(json.dumps({'tick': tick or '', 'errors': int(errs or 0)}))
"@ 2>$null
            if ($schedJson) {
                $sched = $schedJson | ConvertFrom-Json
                if ($sched.errors -ge 10) {
                    $stale = $true
                    $reason = "Scheduler has $($sched.errors) consecutive errors"
                }
            }

            if ($stale) {
                Write-Log "PIPELINE STALLED: $reason - force-restarting lab worker"
                # Kill lab worker processes
                try {
                    $labProcs | ForEach-Object {
                        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
                    }
                } catch {}
                # Clear PID lock so worker can restart
                $labPidFile = Join-Path (Join-Path $env:FORVEN_HOME "lab") "lab_worker.pid"
                if (Test-Path $labPidFile) { Remove-Item $labPidFile -Force -ErrorAction SilentlyContinue }
                # Worker will be restarted on next watchdog cycle or by daemon
                $restarted += "lab_worker(stalled)"
            }
        }
    } catch {
        Write-Log "Pipeline progress check failed: $_"
    }
}

# --- Summary ---
if ($restarted.Count -eq 0) {
    Write-Log "All services healthy."
} else {
    Write-Log ("Restarted: " + ($restarted -join ", "))
}
} finally {
    if ($watchdogOwnerLockHeld) {
        Release-WatchdogOwnerLock
    }
}
