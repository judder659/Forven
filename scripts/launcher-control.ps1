param(
    [ValidateSet('status','start','stop','restart')][string]$Action = 'status',
    [ValidateSet('all','backend','frontend')][string]$Service = 'all',
    [Parameter(Mandatory=$true)][string]$ResultPath
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher-services.ps1')
$backendPort = if ($env:FORVEN_PORT) { [int]$env:FORVEN_PORT } else { 8003 }
$frontendPort = if ($env:VITE_PORT) { [int]$env:VITE_PORT } else { 5173 }
function Get-ServiceProbe {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
        return [pscustomobject]@{ ok=($r.StatusCode -eq 200); body=$r.Content; error='' }
    } catch { return [pscustomobject]@{ ok=$false; body=''; error=$_.Exception.Message } }
}
function Format-HealthIssue {
    param([string]$Issue)
    if ($Issue -match '^agent_stale_pending=(\d+)') { return "$($Matches[1]) agent tasks have been waiting longer than expected." }
    if ($Issue -match '^agent_stale_running=(\d+)') { return "$($Matches[1]) agent tasks may be stuck." }
    if ($Issue -match '^overdue_due_scheduler_jobs=(\d+)') { return "$($Matches[1]) scheduled jobs are overdue." }
    return $Issue.Replace('_',' ')
}
$result = $null
$lock = $null
$ownerLock = $null
try {
    $owned = @(Get-LauncherOwnedProcesses -Root $root)
    if ($Action -eq 'status') {
        $api = Get-ServiceProbe "http://127.0.0.1:$backendPort/api/health"
        $web = Get-ServiceProbe "http://127.0.0.1:$frontendPort/"
        $health = if ($api.ok) { $api.body | ConvertFrom-Json } else { $null }
        $rows = @()
        foreach ($name in @('backend','frontend')) {
            $probe = if ($name -eq 'backend') { $api } else { $web }
            $running = @($owned | Where-Object { $_.Role -eq $name }).Count -gt 0
            $enabled = Test-LauncherServiceEnabled -Root $root -Service $name
            $state = if ($probe.ok) { 'Healthy' } elseif ($running) { 'Starting / unavailable' } elseif (-not $enabled) { 'Stopped by you' } else { 'Unavailable' }
            if ($name -eq 'backend' -and $health -and $health.status -ne 'ok') { $state = 'Needs attention' }
            $rows += [pscustomobject]@{ name=$name; state=$state; enabled=$enabled }
        }
        $issues = if ($health) { @($health.issues | ForEach-Object { Format-HealthIssue ([string]$_) }) -join ' ' } else { 'Backend health is unavailable. Check startup logs if it does not recover.' }
        $result = @{ ok=$true; services=$rows; issues=$issues; checked=(Get-Date).ToString('HH:mm:ss'); background=@($owned | Where-Object { $_.Role -in @('daemon','bot','lab') } | Select-Object -ExpandProperty Role -Unique) }
    } else {
        $temp = Join-Path $root '.tmp'
        New-Item -ItemType Directory -Path $temp -Force | Out-Null
        $lock = [IO.File]::Open((Join-Path $temp 'launcher.control.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        if ($Action -eq 'start' -and (Test-LauncherServiceEnabled -Root $root -Service $Service)) {
            $supervised = @($owned | Where-Object Role -eq supervisor).Count -gt 0
            $selectedRunning = $Service -ne 'all' -and @($owned | Where-Object Role -eq $Service).Count -gt 0
            $allEnabled = (Test-LauncherServiceEnabled -Root $root -Service backend) -and (Test-LauncherServiceEnabled -Root $root -Service frontend)
            if ($selectedRunning -or ($supervised -and ($Service -ne 'all' -or $allEnabled))) {
                $result = @{ok=$true;message='The selected service is already running or starting. Health checks will confirm readiness.'}
                return
            }
        }
        # Refuse control when a port belongs to an unverified installation.
        $roles = if ($Service -eq 'all') { @('backend','frontend','daemon','bot','lab') } else { @($Service) }
        foreach ($name in @('backend','frontend')) {
            # Startup reuses the full supervisor, so validate both service ports
            # even for a single-service action.
            $port = if ($name -eq 'backend') { $backendPort } else { $frontendPort }
            foreach ($listener in @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)) {
                if ([int]$listener.OwningProcess -notin @($owned | ForEach-Object { $_.Id })) {
                    throw "Port $port belongs to a process that cannot be verified as this Forven installation. No processes were stopped."
                }
            }
        }
        $marker = Join-Path $temp "launcher.$Service.stopped"
        # Persist intent BEFORE stopping, so the scheduled watchdog cannot undo it.
        Set-Content -LiteralPath $marker -Value 'Stopped by the desktop launcher' -Encoding UTF8
        Stop-LauncherOwnedProcesses -Root $root -Roles @('supervisor','bootstrap')
        # Finish any watchdog cycle already in progress before touching services.
        # Both supervisors use this lock; this closes the check-then-stop race.
        $deadline = (Get-Date).AddSeconds(60)
        do {
            try { $ownerLock = [IO.File]::Open((Join-Path $temp 'watchdog.owner.lock'), 'OpenOrCreate', 'ReadWrite', 'Read') }
            catch {
                if ((Get-Date) -ge $deadline) { throw 'The supervisor is busy. Stop intent is saved; retry after its current cycle finishes.' }
                Start-Sleep -Milliseconds 500
            }
        } until ($ownerLock)
        if ($Action -in @('stop','restart')) { Stop-LauncherOwnedProcesses -Root $root -Roles $roles }
        if ($Action -ne 'stop') {
            if ($Service -ne 'all' -and (Test-Path (Join-Path $temp 'launcher.all.stopped'))) {
                # Starting one service after Stop Forven must leave other services stopped.
                throw 'Forven is stopped as a whole. Use Start Forven first.'
            }
            Remove-Item -LiteralPath $marker -Force
            if ($Service -eq 'all') {
                foreach ($name in @('backend','frontend')) { Remove-Item -LiteralPath (Join-Path $temp "launcher.$name.stopped") -Force -ErrorAction SilentlyContinue }
            }
        }
        Remove-Item -LiteralPath (Join-Path $temp 'restart.request') -Force -ErrorAction SilentlyContinue
        if (Test-LauncherServiceEnabled -Root $root) {
            $env:FORCE_RESTART = '0'
            $env:SHOW_CHILD_WINDOWS = '0'
            if (-not $env:START_DAEMON) { $env:START_DAEMON = '1' }
            if (-not $env:START_BOT) { $env:START_BOT = '0' }
            if (-not $env:START_LAB_WORKER) { $env:START_LAB_WORKER = '0' }
            $logs = Join-Path $temp 'logs'
            New-Item -ItemType Directory -Path $logs -Force | Out-Null
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
            Start-Process powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', ('"' + (Join-Path $root 'start_all.ps1') + '"')) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs "launcher-start-$stamp.log") -RedirectStandardError (Join-Path $logs "launcher-start-$stamp.err.log") | Out-Null
        }
        $result = @{ ok=$true; message= if ($Action -eq 'stop') { 'Stop completed. Automatic restart is disabled for the selected services.' } else { 'Startup requested. Health checks will confirm when services are ready.' } }
    }
} catch {
    $result = @{ ok=$false; message=$_.Exception.Message }
} finally {
    if ($ownerLock) { $ownerLock.Dispose() }
    if ($lock) { $lock.Dispose() }
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath ($ResultPath+'.tmp') -Encoding UTF8
    Move-Item -LiteralPath ($ResultPath+'.tmp') -Destination $ResultPath -Force
}
