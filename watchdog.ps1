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

# OPS-3/OPS-10: `Start-Process -RedirectStandardOutput/-RedirectStandardError`
# TRUNCATES its target the instant the new process opens it, so relaunching a
# crashed service destroyed the only record of WHY it died - the 2026-07-07
# backend-crash triage ended in "environmental, logs truncate on restart"
# precisely because of this. Rename the file aside BEFORE relaunching, then
# prune: that accidental truncation was also the only thing bounding these
# logs, so rotating without retention would trade one bug for a disk leak.
$LogRetainCount = 10
$WatchdogLogMaxBytes = 5MB

function Move-LogAside {
    param([string]$Path, [int]$Keep = 10)

    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try {
        if (Test-Path $Path) {
            $item = Get-Item -Path $Path -ErrorAction Stop
            if ($item.Length -gt 0) {
                $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
                $rotated = "$Path.$stamp"
                # Two rotations inside the same second (a restart storm) must not
                # collide - the second one would silently lose its evidence.
                if (Test-Path $rotated) { $rotated = "$Path.$stamp-$PID" }
                Move-Item -Path $Path -Destination $rotated -Force -ErrorAction Stop
            } else {
                Remove-Item -Path $Path -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {}

    try {
        $dir = Split-Path -Parent $Path
        $leaf = Split-Path -Leaf $Path
        # -like (not -Filter): the Windows filter engine's legacy 8.3 matching
        # makes "name.log.*" also match "name.log", which would prune the LIVE log.
        $stale = @(Get-ChildItem -Path $dir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "$leaf.*" -and $_.Name -ne $leaf } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip ([Math]::Max(0, $Keep)))
        foreach ($old in $stale) {
            Remove-Item -Path $old.FullName -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

# OPS-10: the watchdog runs every 2 minutes forever, so any line it logs on
# EVERY cycle becomes the whole file. Throttle those to once per interval,
# persisting the stamp on disk because each cycle is a separate process.
function Write-LogThrottled {
    param([string]$Message, [string]$Key, [int]$IntervalSeconds = 3600)

    $stampPath = Join-Path (Join-Path $RepoRoot ".tmp") ("watchdog.notice." + $Key)
    $now = (Get-Date).ToUniversalTime()
    $last = $null
    if (Test-Path $stampPath) {
        try {
            $raw = (Get-Content -Path $stampPath -Raw -ErrorAction Stop).Trim()
            $last = [DateTime]::Parse($raw, [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind)
        } catch {}
    }
    if ($null -ne $last -and ($now - $last).TotalSeconds -lt $IntervalSeconds) { return }
    try { Set-Content -Path $stampPath -Value $now.ToString("o") -ErrorAction Stop } catch {}
    Write-Log $Message
}

# A condition the watchdog deliberately REFUSES to auto-remediate has to reach a
# human, or "we chose not to restart" is indistinguishable from "nothing was
# wrong". Mirrors start_all.ps1::Send-SupervisorNotification. Best-effort: a
# broken notification pipeline must never take the watchdog cycle down with it.
function Send-WatchdogNotification {
    param([string]$Title, [string]$Summary, [string]$Severity = "critical", [string]$DedupeKey)

    $pythonVar = Get-Variable -Name "python" -Scope Script -ErrorAction SilentlyContinue
    if ($null -eq $pythonVar -or [string]::IsNullOrWhiteSpace([string]$pythonVar.Value)) { return }
    $pythonExe = [string]$pythonVar.Value

    try {
        $payload = @{
            title = $Title; summary = $Summary; severity = $Severity; dedupe_key = $DedupeKey
        } | ConvertTo-Json -Compress
        $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($payload))
        # base64 argv, not inline interpolation: reason strings reach this
        # function unescaped and would otherwise break the -c literal.
        $code = "import base64, json, sys; from forven.notifications import emit_notification; p = json.loads(base64.b64decode(sys.argv[1]).decode('utf-8')); emit_notification('system_degraded', severity=p['severity'], source='watchdog', title=p['title'], summary=p['summary'], dedupe_key=(p.get('dedupe_key') or None))"
        & $pythonExe -c $code $encoded *> $null
    } catch {
        Write-Log ("WARN: Could not emit watchdog notification: " + $_.Exception.Message)
    }
}

if (-not (Test-Path $logDir)) { New-Item -Path $logDir -ItemType Directory -Force | Out-Null }

# OPS-10: watchdog.log itself has no natural bound - cap it the same way.
try {
    if ((Test-Path $LogFile) -and ((Get-Item -Path $LogFile -ErrorAction Stop).Length -gt $WatchdogLogMaxBytes)) {
        Move-LogAside -Path $LogFile -Keep 5
    }
} catch {}

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

function ConvertTo-NullableInt {
    param([object]$Value)

    # OPS-2: `ConvertFrom-Json` types JSON integers as Int32 on Windows
    # PowerShell 5.1 but Int64 on pwsh 7, so the original `-is [int]` guards on
    # this snapshot evaluated to $false under pwsh and EVERY scheduler rule was
    # dead code there - the same script silently supervised differently
    # depending on which host the scheduled task happened to launch. Coerce
    # instead of type-testing so both hosts behave identically.
    if ($null -eq $Value) { return $null }
    if ($Value -is [string] -and [string]::IsNullOrWhiteSpace($Value)) { return $null }
    try { return [int64]$Value } catch { return $null }
}

function Get-SnapshotValue {
    param([object]$Source, [string]$Name)

    # `Set-StrictMode -Version Latest` (watchdog.ps1:9) turns a MISSING property
    # into a terminating error, and this snapshot is produced by a separate
    # short-lived interpreter that can legitimately hand back a partial payload.
    # Missing must read as unknown ($null), not as a dead watchdog cycle.
    if ($null -eq $Source) { return $null }
    try {
        $prop = $Source.PSObject.Properties[$Name]
        if ($null -eq $prop) { return $null }
        return $prop.Value
    } catch { return $null }
}

# The stall gate must clear the scheduler's OWN tolerance before it means
# anything: scheduler.py's _SCHEDULER_TICK_WATCHDOG_SECONDS is 900s, and a single
# legitimate heavy tick (db_maintenance alone budgets 600s) can hold the
# heartbeat that long. The previous 300s gate turned that into "stalled":
# .tmp/logs/watchdog.log 2026-07-12 has NINE self-resolving 2-3-cycle bursts in
# one day (07:41, 07:57, 12:19, 14:33, 15:43, 16:53, 18:01, 19:27, 20:13), every
# one bracketed by "All services healthy." with no probe failure. 1800s is 2x
# the scheduler's own watchdog, and it is only ONE THIRD of the gate - see
# Resolve-SchedulerStallAction for the other two conditions.
$SchedulerStallThresholdSeconds = 1800
# Consecutive watchdog cycles the stall must survive. At a 120s cycle that is
# ~10 minutes of re-observation on top of the staleness threshold; none of the
# 2026-07-12 bursts (2-3 cycles, and none of them stale for 1800s) reaches it.
$SchedulerStallCycleLimit = 5

function Resolve-SchedulerStallReason {
    param([object]$Health, [int]$ThresholdSeconds = 1800)

    if ($null -eq $Health) { return $null }

    # scheduler_heartbeat_age_seconds, NOT last_success_age_seconds: the snapshot
    # folds last_tick_started / last_progress_at / MAX(running_since) in the way
    # forven/health_monitor.py::check_scheduler does, because every one of those
    # keys is written with kv_set_best_effort (0.25s timeout) and is SILENTLY
    # DROPPED under SQLite lock contention. Acting on the raw last_successful_tick
    # key is acting on a known false positive.
    $heartbeatAge = ConvertTo-NullableInt (Get-SnapshotValue $Health "scheduler_heartbeat_age_seconds")
    $lastErrorAge = ConvertTo-NullableInt (Get-SnapshotValue $Health "last_error_age_seconds")
    $stuckJobs = ConvertTo-NullableInt (Get-SnapshotValue $Health "stuck_job_count")
    $hardTimeoutJobs = ConvertTo-NullableInt (Get-SnapshotValue $Health "hard_timeout_job_count")
    $lastError = [string](Get-SnapshotValue $Health "last_error")

    if ($null -ne $heartbeatAge -and $heartbeatAge -gt $ThresholdSeconds) {
        return "scheduler heartbeat stale"
    }
    if (
        -not [string]::IsNullOrWhiteSpace($lastError) -and
        $lastError -like "*Scheduler tick exceeded 25s hard timeout*" -and
        $null -ne $lastErrorAge -and
        $lastErrorAge -lt 900
    ) {
        return "scheduler stuck in 25s timeout loop"
    }
    if (
        $null -ne $stuckJobs -and
        $null -ne $hardTimeoutJobs -and
        $stuckJobs -ge 3 -and
        $hardTimeoutJobs -ge 3
    ) {
        return "scheduler jobs are stuck in hard-timeout state"
    }
    return $null
}

function Resolve-SchedulerStallAction {
    param(
        [string]$RuntimeOwner,
        [string]$StallReason,
        [int]$StallCycles,
        [int]$StallLimit,
        [bool]$BackendHealthy,
        [bool]$CriticalLiveScannerOverdue = $false
    )

    # Attributing a scheduler stall to the backend (OPS-2) does NOT mean
    # auto-killing it. This is the process that owns the live trading loops;
    # bouncing it aborts in-flight jobs and open-order bookkeeping - the sibling
    # supervisor at start_all.ps1 makes exactly this call for exactly this
    # reason ("A live+listening backend that fails a few probes is almost always
    # mid-job - restarting it kills the job, so wait it out", 120 probes / ~20
    # min). So "restart-backend" needs ALL of:
    #   1. runtime_owner = api                    (the backend owns the state)
    #   2. staleness past $SchedulerStallThresholdSeconds (2x the scheduler's own watchdog)
    #   3. >= $StallLimit consecutive cycles      (~10 min of re-observation)
    #   4. either the backend is NOT serving /api/health (it is genuinely sick),
    #      OR the critical live execution scanner is itself overdue.
    # The second branch covers a narrower failure class observed on 2026-08-23:
    # /api/health and the event loop were healthy while the scheduler coroutine
    # was parked for hours and the live execution job stopped running. The
    # exact job's overdue next_run_at is required here; generic overdue counts,
    # old best-effort heartbeats, or a healthy API alone are not enough to kill
    # the trading process. Missing/ambiguous scanner evidence fails safe to the
    # existing human-notification path.
    if ([string]::IsNullOrWhiteSpace($StallReason)) { return "none" }
    if ($RuntimeOwner -ne "api") { return "none" }
    if ($StallCycles -lt $StallLimit) { return "observe" }
    if ($BackendHealthy -and -not $CriticalLiveScannerOverdue) { return "notify" }
    return "restart-backend"
}

function Get-RuntimeHealthSnapshot {
    param([string]$PythonPath)

    # OPS-2: this snapshot is scheduler state, and the scheduler is owned by the
    # API (backend) process unless FORVEN_BOT_OWNS_RUNTIME is set - see
    # forven/bot.py::_bot_owns_runtime_loops and control_plane/status.py, which
    # both report runtime_owner the same way. The snapshot therefore carries the
    # owner so the caller can remediate the process that actually owns the state
    # instead of bouncing the Discord bot forever.
    $script = @'
import json
import os
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


def newest(*values):
    best = None
    for value in values:
        if value is not None and (best is None or value > best):
            best = value
    return best


now = datetime.now(timezone.utc)
bot_owns_runtime = os.environ.get('FORVEN_BOT_OWNS_RUNTIME', '').strip().lower() in {
    '1', 'true', 'yes', 'on',
}

bot_worker_fresh = None
bot_worker_age = None
bot_worker_stale_seconds = None
if bot_owns_runtime:
    try:
        from forven.runtime_worker import get_bot_task_worker_status

        bot_worker = get_bot_task_worker_status()
        raw_stale = bot_worker.get('stale_seconds')
        bot_worker_stale_seconds = None if raw_stale is None else max(1, int(float(raw_stale)))
        raw_age = bot_worker.get('age_seconds')
        # A MISSING heartbeat is UNKNOWN, not unhealthy. runtime_worker.py seeds
        # the status dict with fresh=False and returns it UNCHANGED when
        # bot:task_worker:last_seen is absent or unparseable, so reporting
        # `fresh` verbatim would mark a bot that has simply never written a
        # heartbeat yet (e.g. one this very rule just relaunched) as stale.
        if raw_age is None:
            bot_worker_fresh = None
        else:
            bot_worker_age = max(0, int(float(raw_age)))
            bot_worker_fresh = bool(bot_worker.get('fresh'))
    except Exception:
        bot_worker_fresh = None

last_success = parse_ts(kv_get('scheduler:last_successful_tick'))
last_tick_started = parse_ts(kv_get('scheduler:last_tick_started'))
last_progress = parse_ts(kv_get('scheduler:last_progress_at'))
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
    running_since_max = parse_ts(conn.execute(
        "SELECT MAX(running_since) FROM scheduler_jobs "
        "WHERE running_since IS NOT NULL AND TRIM(running_since) != ''"
    ).fetchone()[0])
    critical_scanner_row = conn.execute(
        "SELECT enabled, next_run_at, running_since FROM scheduler_jobs WHERE id = ?",
        ('forven-scanner-hourly',),
    ).fetchone()

critical_scanner_next_run = (
    parse_ts(critical_scanner_row['next_run_at']) if critical_scanner_row is not None else None
)
critical_scanner_due_age = (
    None if critical_scanner_next_run is None
    else max(0, int((now - critical_scanner_next_run).total_seconds()))
)
# Five minutes matches control_plane.status's overdue-job grace. A running row
# still counts after this point: the live scanner's own hard timeout is 180s,
# while the outer PowerShell gate additionally requires >1800s of scheduler
# staleness plus five consecutive observations before it can restart anything.
critical_live_scanner_overdue = bool(
    critical_scanner_row is not None
    and int(critical_scanner_row['enabled'] or 0) == 1
    and critical_scanner_due_age is not None
    and critical_scanner_due_age > 300
)

# Scheduler freshness = the MAX of every heartbeat the loop writes, exactly as
# forven/health_monitor.py::check_scheduler computes it (health_monitor.py:343-370
# plus the running_since fold at :385-386). Reading
# scheduler:last_successful_tick ALONE is a known false-positive generator: it is
# only written after a whole tick returns (bounded at 900s), and every one of
# these keys goes through kv_set_best_effort, which has a 0.25s timeout and
# SILENTLY DROPS the write under SQLite lock contention. health_monitor's own
# comment: "would falsely flip the scheduler RED while the loop is fine".
#
# health_monitor additionally prefers scheduler.get_last_tick_at(), an
# in-process value that can never be dropped — unavailable here, because this
# snapshot runs in a SEPARATE short-lived interpreter. That missing compensation
# is why the PowerShell gate that consumes this uses a staleness threshold far
# above the scheduler's own 900s tick watchdog, and never acts on one reading.
scheduler_heartbeat = newest(last_success, last_tick_started, last_progress, running_since_max)

snapshot = {
    "runtime_owner": "bot" if bot_owns_runtime else "api",
    "bot_task_worker_fresh": bot_worker_fresh,
    "bot_task_worker_age_seconds": bot_worker_age,
    "bot_task_worker_stale_seconds": bot_worker_stale_seconds,
    "scheduler_heartbeat": scheduler_heartbeat.isoformat() if scheduler_heartbeat else None,
    "scheduler_heartbeat_age_seconds": (
        None if scheduler_heartbeat is None
        else max(0, int((now - scheduler_heartbeat).total_seconds()))
    ),
    "last_successful_tick": last_success.isoformat() if last_success else None,
    "last_success_age_seconds": None if last_success is None else max(0, int((now - last_success).total_seconds())),
    "last_tick_started": last_tick_started.isoformat() if last_tick_started else None,
    "last_progress_at": last_progress.isoformat() if last_progress else None,
    "last_error": last_error,
    "last_error_at": last_error_at.isoformat() if last_error_at else None,
    "last_error_age_seconds": None if last_error_at is None else max(0, int((now - last_error_at).total_seconds())),
    "stuck_job_count": stuck_job_count,
    "hard_timeout_job_count": hard_timeout_job_count,
    "critical_live_scanner_overdue": critical_live_scanner_overdue,
    "critical_live_scanner_due_age_seconds": critical_scanner_due_age,
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
    # OPS-1: the loop variable MUST NOT be $pid. $PID is a PowerShell *Constant*
    # automatic variable, so `foreach ($pid in ...)` throws a terminating error -
    # and because this function sits at the top of the bot-restart path, the
    # whole watchdog run died here. .tmp/logs/watchdog.log 2026-07-20 15:11-15:53
    # shows 22 consecutive cycles that logged "Bot unhealthy - restarting" and
    # nothing else: 42 minutes with no daemon/lab-worker/frontend/pipeline
    # supervision, and the bot was never actually restarted either.
    foreach ($procId in @($ProcessIds | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Log ("Stopped bot PID " + $procId)
        } catch {
            Write-Log ("WARN: Could not stop bot PID " + $procId + ": " + $_.Exception.Message)
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
$script:WatchdogCycleFailed = $false

function Write-WatchdogSummary {
    if (@($script:restarted).Count -eq 0) {
        Write-Log "All services healthy."
    } else {
        Write-Log ("Restarted: " + ($script:restarted -join ", "))
    }
}

try {
    $watchdogClaim = Acquire-WatchdogOwnerLock -OwnerName "watchdog.ps1"
    if ($null -eq $watchdogClaim -or -not [bool]$watchdogClaim.claimed) {
        $watchdogStatus = if ($null -ne $watchdogClaim) { $watchdogClaim.status } else { $null }
        $activeOwnerName = if ($null -ne $watchdogStatus -and $null -ne $watchdogStatus.owner_name) { [string]$watchdogStatus.owner_name } else { "watchdog" }
        $activeOwnerPid = if ($null -ne $watchdogStatus -and $null -ne $watchdogStatus.active_pid) { [int]$watchdogStatus.active_pid } else { 0 }
        # OPS-10: start_all normally holds the owner lock for days, so this branch
        # fires on EVERY 2-minute cycle and was the single largest contributor to
        # watchdog.log. It is a normal steady state, not an event - log hourly.
        if ($null -ne $watchdogStatus -and [bool]$watchdogStatus.other_process_active) {
            Write-LogThrottled -Key "owner_active" -Message ("Another watchdog owner is active (" + $activeOwnerName + " PID " + $activeOwnerPid + ") - exiting")
            exit 0
        }
        if ($null -ne $watchdogStatus -and [bool]$watchdogStatus.lock_held) {
            Write-LogThrottled -Key "owner_unknown" -Message "Another watchdog owner appears active, but owner metadata is unavailable - exiting"
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

    # --- Runtime health snapshot (scheduler state + who owns it) ---
    # OPS-2: taken BEFORE the backend decision because these rules remediate the
    # scheduler's OWNER, which is the backend by default.
    $runtimeHealth = Get-RuntimeHealthSnapshot -PythonPath $python
    $runtimeOwnerRaw = [string](Get-SnapshotValue $runtimeHealth "runtime_owner")
    $runtimeOwner = if (-not [string]::IsNullOrWhiteSpace($runtimeOwnerRaw)) { $runtimeOwnerRaw } else { "api" }

    # The three scheduler conditions, evaluated once and attributed to the owner
    # below. Before OPS-2 these all bounced the Discord bot: a stalled BACKEND
    # scheduler was detected correctly and "fixed" by restarting a process that
    # does not run the scheduler, forever.
    $schedulerStallReason = Resolve-SchedulerStallReason -Health $runtimeHealth -ThresholdSeconds $SchedulerStallThresholdSeconds

    # Persisted consecutive-cycle counter, shared by the backend rule below and
    # the bot rule further down. Each watchdog run is a separate scheduled-task
    # process, so tolerance has to live on disk.
    $schedulerStallFile = Join-Path (Join-Path $RepoRoot ".tmp") "watchdog.scheduler_stall_cycles"
    $schedulerStallLimit = $SchedulerStallCycleLimit
    $schedulerStallCycles = 0
    if ($null -ne $schedulerStallReason) {
        if (Test-Path $schedulerStallFile) {
            try { $schedulerStallCycles = [int]((Get-Content $schedulerStallFile -ErrorAction Stop) -join "").Trim() } catch {}
        }
        $schedulerStallCycles += 1
        # Persisted every cycle INCLUDING past the limit, so the "-eq limit"
        # transition below notifies exactly once per stall episode.
        try { Set-Content -Path $schedulerStallFile -Value $schedulerStallCycles -ErrorAction Stop } catch {}
    } else {
        Remove-Item -Path $schedulerStallFile -Force -ErrorAction SilentlyContinue
    }
    $schedulerStallConfirmed = ($null -ne $schedulerStallReason -and $schedulerStallCycles -ge $schedulerStallLimit)

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
    # OPS-2: when the API owns the runtime loops (the default), a stalled
    # scheduler is a BACKEND fault - the pre-OPS-2 code detected it correctly and
    # then bounced the Discord bot, which does not run the scheduler, forever.
    # Resolve-SchedulerStallAction holds the four conditions and the reasoning.
    $backendSchedulerStall = $null
    $schedulerStallAction = Resolve-SchedulerStallAction `
        -RuntimeOwner $runtimeOwner -StallReason ([string]$schedulerStallReason) `
        -StallCycles $schedulerStallCycles -StallLimit $schedulerStallLimit `
        -BackendHealthy ([bool]$backendHealthy) `
        -CriticalLiveScannerOverdue ([bool](Get-SnapshotValue $runtimeHealth "critical_live_scanner_overdue"))
    if ($schedulerStallAction -eq "observe") {
        Write-Log ("Backend scheduler stalled (" + $schedulerStallReason + ", " + $schedulerStallCycles + "/" + $schedulerStallLimit + " cycles) - not restarting yet.")
    } elseif ($schedulerStallAction -eq "notify") {
        if ($schedulerStallCycles -eq $schedulerStallLimit) {
            Send-WatchdogNotification `
                -Title "Scheduler stalled on a healthy backend" `
                -DedupeKey "watchdog:scheduler_stall:backend" `
                -Summary ("The backend scheduler has been stalled (" + $schedulerStallReason + ") for " + $schedulerStallCycles + " consecutive watchdog cycles, but the backend is still serving /api/health and the critical live execution scanner is not provably overdue. The watchdog will NOT restart it automatically - a restart aborts in-flight jobs on the live trading process. Investigate the scheduler loop.")
        }
        Write-LogThrottled -Key "scheduler_stall_healthy" -IntervalSeconds 3600 `
            -Message ("Backend scheduler stalled (" + $schedulerStallReason + ", " + $schedulerStallCycles + " cycles) but /api/health is passing - NOT restarting; operator action required.")
    } elseif ($schedulerStallAction -eq "restart-backend") {
        $backendSchedulerStall = $schedulerStallReason
        $backendNeedsRestart = $true
    }

    if ($backendNeedsRestart) {
        Remove-Item -Path $probeFailFile -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $schedulerStallFile -Force -ErrorAction SilentlyContinue
        $msg = if ($backendRestartForced) {
            "Backend restart requested via sentinel - restarting"
        } elseif ($backendListeners.Count -eq 0) {
            "Backend DOWN (no listener, healthy=" + $backendHealthy + ") - restarting"
        } elseif ($null -ne $backendSchedulerStall) {
            $scannerEvidence = [bool](Get-SnapshotValue $runtimeHealth "critical_live_scanner_overdue")
            if ($backendHealthy -and $scannerEvidence) {
                "Backend scheduler stalled (" + $backendSchedulerStall + " for " + $schedulerStallCycles + " cycles; critical live execution scanner overdue despite healthy /api/health; runtime_owner=api) - restarting"
            } else {
                "Backend unhealthy (" + $backendSchedulerStall + " for " + $schedulerStallCycles + " cycles AND /api/health failing; runtime_owner=api) - restarting"
            }
        } else {
            "Backend hung (" + $probeFailures + " consecutive failed probes with a live listener) - restarting"
        }
        Write-Log $msg
        Stop-BackendProcesses -ListenerPids $backendListeners
        $backendLog = Join-Path $logRoot "unified_backend.log"
        $backendErr = Join-Path $logRoot "unified_backend.err.log"
        Move-LogAside -Path $backendLog -Keep $LogRetainCount
        Move-LogAside -Path $backendErr -Keep $LogRetainCount
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
# OPS-2: the bot rule may only key on BOT-owned state. With the default
# runtime_owner=api the scheduler conditions belong to the backend (handled
# above) and there is no bot-owned degradation signal here - liveness alone
# governs. With FORVEN_BOT_OWNS_RUNTIME=1 the bot legitimately runs the
# scheduler and its queue loops, so the same scheduler conditions plus the
# bot task-worker heartbeat (bot:task_worker:last_seen) do target the bot.
#
# The task-worker signal needs its own debounce for two reasons the backend
# signal does not have: BOT_TASK_WORKER_STALE_SECONDS is 120s and this watchdog
# also cycles every 120s, so a bot this rule just relaunched has essentially no
# grace before the next cycle reads the still-stale key and kills it again; and
# a heartbeat that was NEVER written reads as age=null, which is UNKNOWN, not
# stale. Require a measured age at least 3x past the stale window, then require
# that to hold for 3 consecutive cycles.
$botWorkerStallFile = Join-Path (Join-Path $RepoRoot ".tmp") "watchdog.bot_worker_stall_cycles"
$botWorkerStallLimit = 3
$botWorkerStallCycles = 0
$botWorkerStale = $false
if ($botAlive -and $runtimeOwner -eq "bot" -and $null -ne $runtimeHealth) {
    $botWorkerAge = ConvertTo-NullableInt (Get-SnapshotValue $runtimeHealth "bot_task_worker_age_seconds")
    $botWorkerStaleWindow = ConvertTo-NullableInt (Get-SnapshotValue $runtimeHealth "bot_task_worker_stale_seconds")
    if ($null -eq $botWorkerStaleWindow -or $botWorkerStaleWindow -lt 1) { $botWorkerStaleWindow = 120 }
    if ($null -ne $botWorkerAge -and $botWorkerAge -gt (3 * $botWorkerStaleWindow)) {
        $botWorkerStale = $true
    }
}
if ($botWorkerStale) {
    if (Test-Path $botWorkerStallFile) {
        try { $botWorkerStallCycles = [int]((Get-Content $botWorkerStallFile -ErrorAction Stop) -join "").Trim() } catch {}
    }
    $botWorkerStallCycles += 1
    try { Set-Content -Path $botWorkerStallFile -Value $botWorkerStallCycles -ErrorAction Stop } catch {}
} else {
    Remove-Item -Path $botWorkerStallFile -Force -ErrorAction SilentlyContinue
}

if ($botAlive -and $runtimeOwner -eq "bot" -and $null -ne $runtimeHealth) {
    if ($schedulerStallConfirmed) {
        $botHealthy = $false
        $botHealthReason = $schedulerStallReason
    } elseif ($botWorkerStallCycles -ge $botWorkerStallLimit) {
        $botHealthy = $false
        $botHealthReason = "bot task-worker heartbeat stale"
    } elseif ($null -ne $schedulerStallReason -or $botWorkerStale) {
        $observed = if ($null -ne $schedulerStallReason) { $schedulerStallReason } else { "bot task-worker heartbeat stale" }
        Write-Log ("Bot degradation observed (" + $observed + ") - below the consecutive-cycle threshold, not restarting yet.")
    }
}

if ($botAlive -and -not $botHealthy) {
    Write-Log ("Bot unhealthy (" + $botHealthReason + ") - restarting")
    $stopped = Stop-BotProcesses -ProcessIds (Get-BotProcessIds -LockFilePath $botLockFile)
    # Restarting resets both debounce counters: the replacement process must be
    # given the full grace window before it can be judged again.
    Remove-Item -Path $botWorkerStallFile -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $schedulerStallFile -Force -ErrorAction SilentlyContinue
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
        Move-LogAside -Path $botLog -Keep $LogRetainCount
        Move-LogAside -Path $botErr -Keep $LogRetainCount
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
    Move-LogAside -Path $daemonLog -Keep $LogRetainCount
    Move-LogAside -Path $daemonErr -Keep $LogRetainCount
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
    Move-LogAside -Path $labWorkerLog -Keep $LogRetainCount
    Move-LogAside -Path $labWorkerErr -Keep $LogRetainCount
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
        Move-LogAside -Path $frontendLog -Keep $LogRetainCount
        Move-LogAside -Path $frontendErr -Keep $LogRetainCount
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
Write-WatchdogSummary
} catch {
    # OPS-1: the outer try had NO catch, so ANY terminating error (the $pid
    # constant-assignment bug being the one that actually bit us) killed the run
    # silently after whatever it had already logged - the operator saw "Bot
    # unhealthy - restarting" 22 times and no summary, with no clue the script
    # had died. Log it loudly and still emit the summary so the failure is
    # visible in watchdog.log and every remaining check is known to be skipped.
    $failure = if ($null -ne $_.Exception) { $_.Exception.Message } else { [string]$_ }
    $where = if ($null -ne $_.InvocationInfo) { " at " + $_.InvocationInfo.ScriptName + ":" + $_.InvocationInfo.ScriptLineNumber } else { "" }
    Write-Log ("ERROR: watchdog cycle aborted$where - " + $failure)
    Write-Log "ERROR: remaining service checks were SKIPPED this cycle."
    Write-WatchdogSummary
    $script:WatchdogCycleFailed = $true
} finally {
    if ($watchdogOwnerLockHeld) {
        Release-WatchdogOwnerLock
    }
}

if ($script:WatchdogCycleFailed) { exit 1 }
