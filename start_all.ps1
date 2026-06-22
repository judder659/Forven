# Requires PowerShell 5.1+
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:WatchdogOwnerLockStream = $null
$script:WatchdogOwnerName = $null
$script:WatchdogOwnerAcquiredAt = $null
Set-Location $script:RepoRoot


function Write-Info { param([string]$m) Write-Host "[start_all] $m" }
function Write-WarnMessage { param([string]$m) Write-Host "[start_all][warn] $m" }
function Throw-StartAllError { param([string]$m) throw "[start_all][error] $m" }

# Safe property lookup: under `Set-StrictMode -Version Latest`, reading a
# non-existent property on a PSCustomObject throws. This returns $null instead
# so callers can use `$null -ne` guards on JSON-derived objects.
function Get-OptionalProperty {
    param([object]$Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $psobj = $Object.PSObject
    if ($null -eq $psobj) { return $null }
    $prop = $psobj.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Add-DirectoryToPath {
    param([string]$Directory)
    if ([string]::IsNullOrWhiteSpace($Directory) -or -not (Test-Path $Directory)) { return }
    $pathEntries = @($env:Path -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($pathEntries -contains $Directory) { return }
    $env:Path = "$Directory;$($env:Path)"
}

function Get-ExistingExecutablePath {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
        if (Test-Path $candidate) { return (Resolve-Path $candidate).Path }
    }
    return $null
}

function Get-PythonLauncherCommand {
    $launcherPath = Get-ExistingExecutablePath -Candidates @(
        "py",
        (Join-Path $env:WINDIR "py.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe")
    )
    if ($launcherPath) { Add-DirectoryToPath -Directory (Split-Path $launcherPath -Parent) }
    return $launcherPath
}

function Get-SystemPythonCommand {
    $pythonPath = Get-ExistingExecutablePath -Candidates @(
        # Prefer the repo virtualenv: it has the app's dependencies (yaml, pandas,
        # etc.). A bare system Python on PATH may be missing them and will serve a
        # crippled/partial backend, so it must NOT win the resolution.
        (Join-Path $script:RepoRoot ".venv\Scripts\python.exe"),
        "python",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python311\python.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Python312\python.exe")
    )
    if ($pythonPath) { Add-DirectoryToPath -Directory (Split-Path $pythonPath -Parent) }
    return $pythonPath
}

function Invoke-Checked {
    param([string]$FilePath, [string[]]$CommandArgs)
    Write-Info "Running: $FilePath $($CommandArgs -join ' ')"
    & $FilePath @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        Throw-StartAllError "Command failed (exit $LASTEXITCODE): $FilePath $($CommandArgs -join ' ')"
    }
}

function Get-NpmCommand {
    $npmPath = Get-ExistingExecutablePath -Candidates @(
        "npm.cmd",
        "npm",
        (Join-Path $env:ProgramFiles "nodejs\npm.cmd"),
        (Join-Path ${env:ProgramFiles(x86)} "nodejs\npm.cmd"),
        (Join-Path $env:LOCALAPPDATA "Programs\nodejs\npm.cmd")
    )
    if ($npmPath) {
        Add-DirectoryToPath -Directory (Split-Path $npmPath -Parent)
        return $npmPath
    }
    Throw-StartAllError "npm was not found in PATH. Install Node.js 18+."
}

function Test-ModuleAvailable {
    param([string]$PythonCommand, [string]$ModuleName)
    & $PythonCommand -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)" $ModuleName
    return ($LASTEXITCODE -eq 0)
}

function Wait-ForHttp {
    param([string]$Url,[string]$Label,[int]$Attempts = 90)
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            $requestArgs = @{
                Uri        = $Url
                Method     = "Get"
                TimeoutSec = 3
            }
            # Windows PowerShell can prompt for script parsing unless basic parsing is forced.
            if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey("UseBasicParsing")) {
                $requestArgs["UseBasicParsing"] = $true
            }
            $r = Invoke-WebRequest @requestArgs
            Write-Info "$Label is healthy ($Url) status=$($r.StatusCode)"
            return $true
        } catch { Start-Sleep -Seconds 1 }
    }
    return $false
}

function Wait-ForLabWorker {
    param([int]$Attempts = 20)
    for ($i = 1; $i -le $Attempts; $i++) {
        $status = Get-LabWorkerStatus
        if (($null -ne $status) -and [bool]$status.active) {
            return $status
        }
        Start-Sleep -Seconds 1
    }
    return $null
}

function Stop-PortListeners {
    param([int]$Port)
    $listenerPids = @(Get-ListeningProcessIds -Port $Port)
    foreach ($procId in $listenerPids) {
        if (-not $procId -or $procId -eq $PID) { continue }
        try {
            Write-WarnMessage "Stopping PID $procId listening on port $Port"
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            Write-WarnMessage "PID $procId could not be stopped directly: $($_.Exception.Message)"
        }
    }

    $remainingPids = @(Get-ListeningProcessIds -Port $Port)
    if ($remainingPids.Count -eq 0) { return $true }

    Write-WarnMessage "Port $Port still in use by PID(s): $($remainingPids -join ', ') - will attempt to reuse if healthy."
    return $false
}

function Get-ListeningProcessIds {
    param([int]$Port)
    $lines = @(
        cmd /c "netstat -ano -p tcp | findstr LISTENING" 2>$null
    )
    if ($lines.Count -eq 0) { return @() }

    $pids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($line in $lines) {
        if ($line -match "^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$") {
            $linePort = [int]$matches[1]
            $linePid = [int]$matches[2]
            if ($linePort -ne $Port -or $linePid -le 0) { continue }
            [void]$pids.Add($linePid)
        }
    }
    return @($pids)
}

function Get-ListeningProcess {
    param([int]$Port)
    foreach ($procId in @(Get-ListeningProcessIds -Port $Port)) {
        try {
            return Get-Process -Id $procId -ErrorAction Stop
        } catch {
            continue
        }
    }
    return $null
}

function Get-BotProcessIds {
    $escapedRepoRoot = [Regex]::Escape($script:RepoRoot)
    try {
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
                Where-Object {
                    $cmd = [string]$_.CommandLine
                    $cmd -match $escapedRepoRoot -and (
                        $cmd -match 'from forven\.bot import run_bot' -or
                        $cmd -match '-m\s+forven(\.cli)?\s+bot' -or
                        $cmd -match '-m\s+forven\s+bot'
                    )
                } |
                Select-Object -ExpandProperty ProcessId -Unique
        )
    } catch {
        Write-WarnMessage "Bot process discovery fallback failed: $($_.Exception.Message)"
        return @()
    }
}

function Get-DescendantProcessIds {
    param([int[]]$RootProcessIds)
    $roots = @($RootProcessIds | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    if ($roots.Count -eq 0) { return @() }

    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId,ParentProcessId)
    } catch {
        Write-WarnMessage "Descendant process discovery fallback failed: $($_.Exception.Message)"
        return @()
    }

    $remaining = New-Object System.Collections.Generic.Queue[int]
    foreach ($rootPid in $roots) { $remaining.Enqueue([int]$rootPid) }
    $seen = New-Object System.Collections.Generic.HashSet[int]
    $descendants = New-Object System.Collections.Generic.List[int]

    while ($remaining.Count -gt 0) {
        $current = $remaining.Dequeue()
        foreach ($proc in $all) {
            $parentPid = [int]$proc.ParentProcessId
            $childPid = [int]$proc.ProcessId
            if ($parentPid -ne $current -or $roots -contains $childPid) { continue }
            if ($seen.Add($childPid)) {
                $descendants.Add($childPid)
                $remaining.Enqueue($childPid)
            }
        }
    }

    return @($descendants | Select-Object -Unique)
}

function Stop-ProcessTree {
    param(
        [int[]]$RootProcessIds,
        [string]$Label
    )

    $roots = @($RootProcessIds | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    if ($roots.Count -eq 0) { return }

    $descendants = @(Get-DescendantProcessIds -RootProcessIds $roots)
    $allPids = @($descendants + $roots | Select-Object -Unique)
    $orderedPids = @($allPids | Sort-Object -Descending)

    foreach ($procId in $orderedPids) {
        try {
            Write-WarnMessage "Stopping existing $Label PID $procId"
            Stop-Process -Id $procId -Force -ErrorAction Stop
        } catch {
            try {
                $stillRunning = Get-Process -Id $procId -ErrorAction SilentlyContinue
            } catch {
                $stillRunning = $null
            }
            if ($null -ne $stillRunning) {
                Throw-StartAllError "Existing $Label PID $procId could not be stopped: $($_.Exception.Message)"
            }
        }
    }

    foreach ($procId in $orderedPids) {
        try { Wait-Process -Id $procId -Timeout 10 -ErrorAction SilentlyContinue } catch {}
    }
}

function Get-OrphanPythonMultiprocessingProcessIds {
    try {
        $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        Write-WarnMessage "Orphan Python worker discovery failed: $($_.Exception.Message)"
        return @()
    }

    $liveProcessIds = New-Object System.Collections.Generic.HashSet[int]
    foreach ($proc in $allProcesses) {
        [void]$liveProcessIds.Add([int]$proc.ProcessId)
    }

    return @(
        $allProcesses |
            Where-Object {
                $cmd = [string]$_.CommandLine
                $cmd -match 'multiprocessing\.spawn' -and
                    $cmd -match 'spawn_main' -and
                    -not $liveProcessIds.Contains([int]$_.ParentProcessId)
            } |
            Select-Object -ExpandProperty ProcessId -Unique
    )
}

function Stop-OrphanPythonMultiprocessingProcesses {
    $orphanPids = @(Get-OrphanPythonMultiprocessingProcessIds)
    if ($orphanPids.Count -eq 0) { return }

    Write-WarnMessage "Stopping orphan Python multiprocessing worker PID(s): $($orphanPids -join ', ')"
    foreach ($procId in $orphanPids) {
        try {
            Stop-Process -Id ([int]$procId) -Force -ErrorAction Stop
        } catch {
            Write-WarnMessage "Orphan Python worker PID $procId could not be stopped: $($_.Exception.Message)"
        }
    }
}

function Stop-ExistingBotProcesses {
    param([int[]]$AdditionalProcessIds = @())

    $botPids = @((@(Get-BotProcessIds) + @($AdditionalProcessIds)) | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    if ($botPids.Count -eq 0) { return }
    Stop-ProcessTree -RootProcessIds $botPids -Label "bot"

    $remaining = @(Get-BotProcessIds)
    if ($remaining.Count -gt 0) {
        Throw-StartAllError "Bot restart guard failed; existing bot PID(s) still running: $($remaining -join ', ')."
    }
}

function Get-DaemonProcessIds {
    $escapedRepoRoot = [Regex]::Escape($script:RepoRoot)
    try {
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
                Where-Object {
                    $cmd = [string]$_.CommandLine
                    $cmd -match $escapedRepoRoot -and (
                        $cmd -match '-m\s+forven\s+daemon\s+start' -or
                        $cmd -match '-m\s+forven(\.cli)?\s+daemon\s+start'
                    )
                } |
                Select-Object -ExpandProperty ProcessId -Unique
        )
    } catch {
        Write-WarnMessage "Daemon process discovery fallback failed: $($_.Exception.Message)"
        return @()
    }
}

function Stop-ExistingDaemonProcesses {
    param([int[]]$AdditionalProcessIds = @())

    $daemonPids = @((@(Get-DaemonProcessIds) + @($AdditionalProcessIds)) | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    if ($daemonPids.Count -eq 0) { return }
    Stop-ProcessTree -RootProcessIds $daemonPids -Label "daemon"

    $remaining = @(Get-DaemonProcessIds)
    if ($remaining.Count -gt 0) {
        Throw-StartAllError "Daemon restart guard failed; existing daemon PID(s) still running: $($remaining -join ', ')."
    }
}

function Ensure-Venv {
    $venvDir = Join-Path $script:RepoRoot ".venv"
    $venvPython = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        & $venvPython -c "import sys" *> $null
        if ($LASTEXITCODE -eq 0) { return $venvPython }
        Write-WarnMessage "Existing .venv is not runnable; recreating it."
        Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
    }

    Write-Info "Creating .venv ..."
    $created = $false
    $pythonLauncher = Get-PythonLauncherCommand
    if ($pythonLauncher) {
        & $pythonLauncher -3.11 -m venv $venvDir
        if ($LASTEXITCODE -eq 0) { $created = $true }
        if (-not $created) {
            & $pythonLauncher -3 -m venv $venvDir
            if ($LASTEXITCODE -eq 0) { $created = $true }
        }
    }
    if (-not $created) {
        $systemPython = Get-SystemPythonCommand
        if ($systemPython) {
            & $systemPython -m venv $venvDir
            if ($LASTEXITCODE -eq 0) { $created = $true }
        }
    }
    if (-not $created -and (Get-Command python -ErrorAction SilentlyContinue)) {
        & python -m venv $venvDir
        if ($LASTEXITCODE -eq 0) { $created = $true }
    }
    if (-not $created) {
        Throw-StartAllError "Python not found. Install Python 3.11+."
    }

    if (-not (Test-Path $venvPython)) {
        Throw-StartAllError "Failed to create virtual environment at .venv"
    }
    return $venvPython
}

function Install-BackendDeps {
    param([string]$Python)
    Invoke-Checked -FilePath $Python -CommandArgs @("-m","pip","install","--upgrade","pip")
    try {
        Invoke-Checked -FilePath $Python -CommandArgs @("-m","pip","install","-e",".")
    } catch {
        Write-WarnMessage "pip install -e . failed; installing runtime deps directly."
        & $Python -m pip uninstall -y forven | Out-Null
        Invoke-Checked -FilePath $Python -CommandArgs @(
            "-m","pip","install",
            "click>=8.0","rich>=13.0","httpx>=0.25","PyJWT>=2.8","cryptography>=41.0","croniter>=2.0",
            "filelock>=3.13","discord.py>=2.3","chromadb>=0.5","numpy>=1.26","pandas>=2.2",
            "python-multipart>=0.0.9","fastapi>=0.111.0","uvicorn>=0.30.0","websockets>=13.0,<17","slowapi>=0.1.9","alembic>=1.13.0","pyarrow>=16.0.0","ccxt>=4.5.0",
            "eth-account>=0.13.0","hyperliquid-python-sdk>=0.22.0"
        )
    }
}

function Ensure-FrontendDeps {
    param([string]$Npm)
    $frontend = Join-Path $script:RepoRoot "frontend"
    if (-not (Test-Path $frontend)) { Throw-StartAllError "Frontend directory missing: $frontend" }
    if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
        Push-Location $frontend
        try { Invoke-Checked -FilePath $Npm -CommandArgs @("install") }
        finally { Pop-Location }
    }
}

function Start-LoggedProcess {
    param([string]$FilePath,[string[]]$CommandArgs,[string]$WorkingDirectory,[string]$StdOutPath,[string]$StdErrPath)
    Remove-Item $StdOutPath -Force -ErrorAction SilentlyContinue
    Remove-Item $StdErrPath -Force -ErrorAction SilentlyContinue
    Write-Info "Launching: $FilePath $($CommandArgs -join ' ')"
    $windowStyle = if ($script:ShowChildWindows -eq "1") { "Normal" } else { "Hidden" }
    return Start-Process -FilePath $FilePath -ArgumentList $CommandArgs -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdOutPath -RedirectStandardError $StdErrPath -WindowStyle $windowStyle -PassThru
}

function Test-BotTokenConfigured {
    if (-not [string]::IsNullOrWhiteSpace($env:DISCORD_TOKEN)) {
        return $true
    }

    $configPath = Join-Path $env:FORVEN_HOME "config.json"
    if (-not (Test-Path $configPath)) {
        return $false
    }

    try {
        $config = Get-Content -Path $configPath -Raw | ConvertFrom-Json
        return (-not [string]::IsNullOrWhiteSpace([string]$config.discord_token))
    } catch {
        return $false
    }
}

function Stop-StartedProcessIfRunning { param([System.Diagnostics.Process]$Process)
    if ($null -eq $Process) { return }
    try { if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue } } catch {}
}

function Get-PythonLockStatus {
    param([string]$ModuleName, [string]$FunctionName)

    try {
        $json = & $python -c "import importlib, json; module = importlib.import_module('$ModuleName'); print(json.dumps(getattr(module, '$FunctionName')()))" 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($json -join ""))) {
            return @{}
        }
        $parsed = ($json -join "" | ConvertFrom-Json)
        return @{
            "lock_path" = [string]$parsed.lock_path
            "active_pid" = $parsed.active_pid
            "active_pid_running" = [bool]$parsed.active_pid_running
            "lock_held" = [bool]$parsed.lock_held
            "stale_pid" = $parsed.stale_pid
            "singleton_supported" = [bool]$parsed.singleton_supported
            "singleton_enforced" = [bool]$parsed.singleton_enforced
            "held_by_current_process" = [bool]$parsed.held_by_current_process
            "other_process_active" = [bool]$parsed.other_process_active
        }
    } catch {
        return @{}
    }
}

function Get-BotLockStatus {
    return Get-PythonLockStatus -ModuleName "forven.bot" -FunctionName "get_bot_lock_status"
}

function Get-DaemonLockStatus {
    return Get-PythonLockStatus -ModuleName "forven.daemon" -FunctionName "get_daemon_lock_status"
}

function Get-WatchdogOwnerLockPath {
    return Join-Path $localTemp "watchdog.owner.lock"
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
    $payloadPid = Get-OptionalProperty -Object $payload -Name 'pid'
    $payloadOwner = Get-OptionalProperty -Object $payload -Name 'owner_name'
    $payloadAcquired = Get-OptionalProperty -Object $payload -Name 'acquired_at'
    $activePid = if ($heldByCurrentProcess) { $PID } elseif ($null -ne $payloadPid) { [int]$payloadPid } else { 0 }
    $ownerName = if ($heldByCurrentProcess) { $script:WatchdogOwnerName } elseif ($null -ne $payloadOwner) { [string]$payloadOwner } else { $null }
    $acquiredAt = if ($heldByCurrentProcess) { $script:WatchdogOwnerAcquiredAt } elseif ($null -ne $payloadAcquired) { [string]$payloadAcquired } else { $null }
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
        owner_name = if ([string]::IsNullOrWhiteSpace($OwnerName)) { "start_all" } else { $OwnerName }
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

function Get-LabWorkerStatus {
    try {
        $json = & $python -c "import json; from forven.lab_worker_service import get_lab_worker_status; print(json.dumps(get_lab_worker_status()))" 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($json -join ""))) {
            return $null
        }
        return (($json -join "") | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Clear-StaleLockFile {
    param([string]$Path, [string]$Label)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path $Path)) { return }
    try {
        Remove-Item -Path $Path -Force -ErrorAction Stop
        Write-Info "Cleared stale $Label lock file: $Path"
    } catch {
        Write-WarnMessage "Could not clear stale $Label lock file ${Path}: $($_.Exception.Message)"
    }
}

function Test-HttpHealthy {
    param([string]$Url)
    try {
        $requestArgs = @{
            Uri        = $Url
            Method     = "Get"
            TimeoutSec = 3
        }
        if ((Get-Command Invoke-WebRequest).Parameters.ContainsKey("UseBasicParsing")) {
            $requestArgs["UseBasicParsing"] = $true
        }
        $null = Invoke-WebRequest @requestArgs
        return $true
    } catch {
        return $false
    }
}

function Get-RestartDelaySeconds {
    param([int]$FailureCount)
    $normalizedFailures = [Math]::Max([int]$FailureCount, 0)
    $boundedFailures = [Math]::Min($normalizedFailures, 5)
    return [int][Math]::Min([Math]::Pow(2, $boundedFailures), 30)
}

function Add-StartupSummary {
    param([string]$Service,[string]$Action,[string]$Details = "")
    $script:StartupSummary += [pscustomobject]@{
        Service = $Service
        Action = $Action
        Details = $Details
    }
}

function Write-StartupSummary {
    if (-not $script:StartupSummary -or $script:StartupSummary.Count -eq 0) { return }
    Write-Info "Startup summary:"
    foreach ($entry in $script:StartupSummary) {
        $suffix = if ([string]::IsNullOrWhiteSpace([string]$entry.Details)) { "" } else { " ($($entry.Details))" }
        Write-Info "  $($entry.Service): $($entry.Action)$suffix"
    }
}

function Start-BackendService {
    $portFreed = Stop-PortListeners -Port $backendPort
    if (-not $portFreed) {
        if (Test-HttpHealthy -Url $backendHealth) {
            Write-Info "Port $backendPort still occupied but backend is healthy - reusing existing service."
            return $null
        }
        Throw-StartAllError "Port $backendPort occupied by unkillable process and service is not healthy."
    }
    Write-Info "Starting backend on ${backendHost}:$backendPort ..."
    $proc = Start-LoggedProcess -FilePath $python -CommandArgs @("-m","uvicorn","--app-dir",$script:RepoRoot,"forven.api:app","--host",$backendHost,"--port",$backendPort.ToString(),"--workers",$backendWorkers.ToString()) `
        -WorkingDirectory $script:RepoRoot -StdOutPath $backendLog -StdErrPath $backendErr
    if (-not (Wait-ForHttp -Url $backendHealth -Label "Backend")) {
        if (Test-Path $backendErr) { Get-Content $backendErr -Tail 120 }
        Stop-StartedProcessIfRunning -Process $proc
        Throw-StartAllError "Backend failed health check."
    }
    return $proc
}

function Start-FrontendService {
    $portFreed = Stop-PortListeners -Port $frontendPort
    if (-not $portFreed) {
        if (Test-HttpHealthy -Url $frontendRoot) {
            Write-Info "Port $frontendPort still occupied but frontend is healthy - reusing existing service."
            return $null
        }
        # Port stuck and unhealthy - try alternate ports
        for ($altPort = $frontendPort + 1; $altPort -le $frontendPort + 5; $altPort++) {
            $altUrl = "http://127.0.0.1:$altPort/"
            $altFree = @(Get-ListeningProcessIds -Port $altPort).Count -eq 0
            if ($altFree) {
                Write-WarnMessage "Port $frontendPort occupied and unhealthy - using alternate port $altPort."
                $frontendPort = $altPort
                $frontendRoot = $altUrl
                break
            }
        }
    }
    Write-Info "Starting frontend on port $frontendPort ..."
    # Clear Vite's dependency-optimization cache before a fresh dev-server start.
    # A stale .vite cache from a prior run makes the loaded page request dead chunk
    # hashes (404) -> blank white screen. Clearing it forces one clean re-optimize.
    $viteCache = Join-Path $script:RepoRoot "frontend\node_modules\.vite"
    if (Test-Path $viteCache) {
        try {
            Remove-Item -Recurse -Force $viteCache -ErrorAction Stop
            Write-Info "Cleared stale Vite dep cache ($viteCache)"
        } catch {
            Write-WarnMessage "Could not clear Vite cache ${viteCache}: $($_.Exception.Message)"
        }
    }
    # Bind Vite on the IPv6 unspecified address so both localhost (::1) and 127.0.0.1 work on Windows.
    $proc = Start-LoggedProcess -FilePath $npm -CommandArgs @("run","dev","--","--host","::","--port",$frontendPort.ToString()) `
        -WorkingDirectory (Join-Path $script:RepoRoot "frontend") -StdOutPath $frontendLog -StdErrPath $frontendErr
    if (-not (Wait-ForHttp -Url $frontendRoot -Label "Frontend")) {
        if (Test-Path $frontendErr) { Get-Content $frontendErr -Tail 120 }
        Stop-StartedProcessIfRunning -Process $proc
        Throw-StartAllError "Frontend failed health check."
    }
    return $proc
}

function Ensure-BackendService {
    $listenerPids = @(Get-ListeningProcessIds -Port $backendPort)
    $healthy = Test-HttpHealthy -Url $backendHealth
    if ($forceRestart -eq "0" -and $listenerPids.Count -gt 0 -and $healthy) {
        Write-Info "Reusing healthy backend on port $backendPort."
        Add-StartupSummary -Service "backend" -Action "reused" -Details "pids=$($listenerPids -join ',')"
        return $null
    }

    $action = if ($listenerPids.Count -gt 0) { "restarted" } else { "started" }
    $proc = Start-BackendService
    if ($null -eq $proc) {
        Add-StartupSummary -Service "backend" -Action "reused" -Details "port occupied, service healthy"
        return $null
    }
    Add-StartupSummary -Service "backend" -Action $action -Details "pid=$($proc.Id)"
    return $proc
}

function Ensure-FrontendService {
    $listenerPids = @(Get-ListeningProcessIds -Port $frontendPort)
    $healthy = Test-HttpHealthy -Url $frontendRoot
    if ($forceRestart -eq "0" -and $listenerPids.Count -gt 0 -and $healthy) {
        Write-Info "Reusing healthy frontend on port $frontendPort."
        Add-StartupSummary -Service "frontend" -Action "reused" -Details "pids=$($listenerPids -join ',')"
        return $null
    }

    $action = if ($listenerPids.Count -gt 0) { "restarted" } else { "started" }
    $proc = Start-FrontendService
    if ($null -eq $proc) {
        Add-StartupSummary -Service "frontend" -Action "reused" -Details "port occupied, service healthy"
        return $null
    }
    Add-StartupSummary -Service "frontend" -Action $action -Details "pid=$($proc.Id)"
    return $proc
}

function Ensure-BotService {
    if ($startBot -ne "1") {
        Add-StartupSummary -Service "bot" -Action "skipped" -Details "START_BOT=0"
        Write-Info "Skipping bot startup (START_BOT=0)"
        return $null
    }

    if (-not (Test-BotTokenConfigured)) {
        Add-StartupSummary -Service "bot" -Action "skipped" -Details "missing Discord token"
        Write-WarnMessage "START_BOT=1 but Discord token is missing; skipping bot startup."
        return $null
    }

    $lockStatus = @(Get-BotLockStatus)[0]
    $lockPath = [string]($lockStatus["lock_path"])
    $lockPid = if ($lockStatus.ContainsKey("active_pid") -and $lockStatus["active_pid"]) { [int]$lockStatus["active_pid"] } else { 0 }
    $rootPids = @((@(Get-BotProcessIds) + @($lockPid)) | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    $lockHealthy = [bool]$lockStatus["lock_held"] -and [bool]$lockStatus["active_pid_running"]

    if (-not [string]::IsNullOrWhiteSpace($lockPath) -and -not [bool]$lockStatus["lock_held"] -and (Test-Path $lockPath) -and $rootPids.Count -eq 0) {
        Clear-StaleLockFile -Path $lockPath -Label "bot"
    }

    if ($forceRestart -eq "0" -and $lockHealthy) {
        Write-Info "Reusing healthy Forven bot."
        Add-StartupSummary -Service "bot" -Action "reused" -Details "pid=$lockPid"
        return $null
    }

    if ($rootPids.Count -gt 0) {
        try {
            Stop-ExistingBotProcesses -AdditionalProcessIds $rootPids
            if (-not [string]::IsNullOrWhiteSpace($lockPath) -and -not [bool]$lockStatus["lock_held"] -and (Test-Path $lockPath)) {
                Clear-StaleLockFile -Path $lockPath -Label "bot"
            }
            $action = "restarted"
        } catch {
            Write-WarnMessage "Could not stop existing Forven bot; leaving it alone so the app can still start. $($_.Exception.Message)"
            Add-StartupSummary -Service "bot" -Action "skipped" -Details "existing process unavailable"
            return $null
        }
    } else {
        $action = "started"
    }

    Write-Info "Starting Forven bot ..."
    $proc = Start-LoggedProcess -FilePath $python -CommandArgs @("-m","forven","bot","start") `
        -WorkingDirectory $script:RepoRoot -StdOutPath $botLog -StdErrPath $botErr
    Add-StartupSummary -Service "bot" -Action $action -Details "pid=$($proc.Id)"
    return $proc
}

function Ensure-DaemonService {
    if ($startDaemon -ne "1") {
        Add-StartupSummary -Service "daemon" -Action "skipped" -Details "START_DAEMON=0"
        Write-Info "Skipping daemon startup (START_DAEMON=0)"
        return $null
    }

    $lockStatus = @(Get-DaemonLockStatus)[0]
    $lockPath = [string]($lockStatus["lock_path"])
    $lockPid = if ($lockStatus.ContainsKey("active_pid") -and $lockStatus["active_pid"]) { [int]$lockStatus["active_pid"] } else { 0 }
    $rootPids = @((@(Get-DaemonProcessIds) + @($lockPid)) | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)
    $lockHealthy = [bool]$lockStatus["lock_held"] -and [bool]$lockStatus["active_pid_running"]

    if (-not [string]::IsNullOrWhiteSpace($lockPath) -and -not [bool]$lockStatus["lock_held"] -and (Test-Path $lockPath) -and $rootPids.Count -eq 0) {
        Clear-StaleLockFile -Path $lockPath -Label "daemon"
    }

    if ($forceRestart -eq "0" -and $lockHealthy) {
        Write-Info "Reusing healthy Forven daemon."
        Add-StartupSummary -Service "daemon" -Action "reused" -Details "pid=$lockPid"
        return $null
    }

    if ($rootPids.Count -gt 0) {
        try {
            Stop-ExistingDaemonProcesses -AdditionalProcessIds $rootPids
            if (-not [string]::IsNullOrWhiteSpace($lockPath) -and -not [bool]$lockStatus["lock_held"] -and (Test-Path $lockPath)) {
                Clear-StaleLockFile -Path $lockPath -Label "daemon"
            }
            $action = "restarted"
        } catch {
            Write-WarnMessage "Could not stop existing Forven daemon; leaving it alone so the app can still start. $($_.Exception.Message)"
            Add-StartupSummary -Service "daemon" -Action "skipped" -Details "existing process unavailable"
            return $null
        }
    } else {
        $action = "started"
    }

    Write-Info "Starting Forven daemon (data/risk loop) ..."
    $proc = Start-LoggedProcess -FilePath $python -CommandArgs @("-m","forven","daemon","start") `
        -WorkingDirectory $script:RepoRoot -StdOutPath $daemonLog -StdErrPath $daemonErr
    Start-Sleep -Seconds 2
    if ($null -ne $proc -and $proc.HasExited -and (Test-Path $daemonErr)) {
        Get-Content -Path $daemonErr -Tail 80
    }
    Add-StartupSummary -Service "daemon" -Action $action -Details "pid=$($proc.Id)"
    return $proc
}

function Ensure-LabWorkerService {
    if ($startLabWorker -ne "1") {
        Add-StartupSummary -Service "lab-worker" -Action "skipped" -Details "START_LAB_WORKER=0"
        Write-Info "Skipping Regime Lab worker startup (START_LAB_WORKER=0)"
        return $null
    }

    $status = Get-LabWorkerStatus
    $statusActive = Get-OptionalProperty -Object $status -Name 'active'
    $active = ($null -ne $status) -and [bool]$statusActive
    $statusWorker = Get-OptionalProperty -Object $status -Name 'worker'
    $statusWorkerPid = Get-OptionalProperty -Object $statusWorker -Name 'pid'
    $workerPid = if ($null -ne $statusWorkerPid) { [int]$statusWorkerPid } else { 0 }

    if ($forceRestart -eq "0" -and $active -and $workerPid -gt 0) {
        Write-Info "Reusing healthy Regime Lab worker."
        Add-StartupSummary -Service "lab-worker" -Action "reused" -Details "pid=$workerPid"
        return $null
    }

    if ($workerPid -gt 0 -and $workerPid -ne $PID) {
        try {
            Write-WarnMessage "Stopping existing Regime Lab worker PID $workerPid"
            Stop-Process -Id $workerPid -Force -ErrorAction Stop
        } catch {
            try {
                $stillRunning = Get-Process -Id $workerPid -ErrorAction SilentlyContinue
            } catch {
                $stillRunning = $null
            }
            if ($null -ne $stillRunning) {
                Write-WarnMessage "Could not stop existing Regime Lab worker; leaving it alone so the app can still start. $($_.Exception.Message)"
                Add-StartupSummary -Service "lab-worker" -Action "skipped" -Details "existing process unavailable"
                return $null
            }
        }
        Start-Sleep -Seconds 1
    }

    $action = if ($workerPid -gt 0) { "restarted" } else { "started" }
    Write-Info "Starting Regime Lab worker ..."
    $proc = Start-LoggedProcess -FilePath $python -CommandArgs @("-m","forven","lab","worker") `
        -WorkingDirectory $script:RepoRoot -StdOutPath $labWorkerLog -StdErrPath $labWorkerErr
    $status = Wait-ForLabWorker -Attempts 20
    $active = ($null -ne $status) -and [bool]$status.active
    if (-not $active) {
        if (Test-Path $labWorkerErr) { Get-Content $labWorkerErr -Tail 120 }
        if ($null -ne $proc -and $proc.HasExited -and (Test-Path $labWorkerLog)) { Get-Content $labWorkerLog -Tail 120 }
        Stop-StartedProcessIfRunning -Process $proc
        Write-WarnMessage "Regime Lab worker failed startup check; continuing without it so the app can still start."
        Add-StartupSummary -Service "lab-worker" -Action "skipped" -Details "startup check failed"
        return $null
    }
    Add-StartupSummary -Service "lab-worker" -Action $action -Details "pid=$($proc.Id)"
    return $proc
}

function Stop-AllForvenProcesses {
    # Kill live Forven services tied to this repo before clearing stale lock files.
    Stop-PortListeners -Port $backendPort
    Stop-PortListeners -Port $frontendPort

    $botStatus = @(Get-BotLockStatus)[0]
    $botPid = if ($botStatus.ContainsKey("active_pid") -and $botStatus["active_pid"]) { [int]$botStatus["active_pid"] } else { 0 }
    $botProcessIds = @((@(Get-BotProcessIds) + @($botPid)) | Where-Object { $_ -and $_ -gt 0 -and $_ -ne $PID } | Select-Object -Unique)
    if ($botProcessIds.Count -gt 0) {
        try {
            Stop-ExistingBotProcesses -AdditionalProcessIds $botProcessIds
        } catch {
            Write-WarnMessage "Existing bot cleanup could not complete: $($_.Exception.Message)"
        }
    }

    $daemonStatus = @(Get-DaemonLockStatus)[0]
    $daemonPid = if ($daemonStatus.ContainsKey("active_pid") -and $daemonStatus["active_pid"]) { [int]$daemonStatus["active_pid"] } else { 0 }
    $daemonProcessIds = @((@(Get-DaemonProcessIds) + @($daemonPid)) | Where-Object { $_ -and $_ -gt 0 -and $_ -ne $PID } | Select-Object -Unique)
    if ($daemonProcessIds.Count -gt 0) {
        try {
            Stop-ExistingDaemonProcesses -AdditionalProcessIds $daemonProcessIds
        } catch {
            Write-WarnMessage "Existing daemon cleanup could not complete: $($_.Exception.Message)"
        }
    }

    $labWorkerStatus = Get-LabWorkerStatus
    $labWorkerStatusWorker = Get-OptionalProperty -Object $labWorkerStatus -Name 'worker'
    $labWorkerStatusPid = Get-OptionalProperty -Object $labWorkerStatusWorker -Name 'pid'
    $labWorkerPid = if ($null -ne $labWorkerStatusPid) { [int]$labWorkerStatusPid } else { 0 }
    if ($labWorkerPid -gt 0 -and $labWorkerPid -ne $PID) {
        try {
            Write-WarnMessage "Stopping existing Regime Lab worker PID $labWorkerPid"
            Stop-Process -Id $labWorkerPid -Force -ErrorAction Stop
        } catch {
            Write-WarnMessage "Regime Lab worker PID $labWorkerPid could not be stopped: $($_.Exception.Message)"
        }
    }

    Stop-OrphanPythonMultiprocessingProcesses

    # Clear stale lock files
    $forvenHome = if ($env:FORVEN_HOME) { $env:FORVEN_HOME } else { Join-Path $env:USERPROFILE ".forven" }
    foreach ($lockName in @("bot.lock", "daemon.lock")) {
        $lockPath = Join-Path $forvenHome $lockName
        if (Test-Path $lockPath) {
            try {
                Remove-Item -Path $lockPath -Force -ErrorAction Stop
                Write-Info "Cleared lock file: $lockPath"
            } catch {
                Write-WarnMessage "Could not clear lock file ${lockPath}: $($_.Exception.Message)"
            }
        }
    }
}

$backendPort = if ([string]::IsNullOrWhiteSpace($env:FORVEN_PORT)) { 8003 } else { [int]$env:FORVEN_PORT }
$backendHost = if (-not [string]::IsNullOrWhiteSpace($env:FORVEN_BIND_HOST)) {
    $env:FORVEN_BIND_HOST.Trim()
} elseif (-not [string]::IsNullOrWhiteSpace($env:FORVEN_HOST)) {
    $env:FORVEN_HOST.Trim()
} else {
    "127.0.0.1"
}
$backendWorkers = if ([string]::IsNullOrWhiteSpace($env:BACKEND_WORKERS)) { 1 } else { [int]$env:BACKEND_WORKERS }
$frontendPort = 5173
$startBot = if ($env:START_BOT) { $env:START_BOT.Trim() } else { "0" }
$regimeLabEnabledRaw = if ($env:FORVEN_ENABLE_REGIME_LAB) { $env:FORVEN_ENABLE_REGIME_LAB.Trim() } else { "0" }
$regimeLabEnabled = @("1", "true", "yes", "on") -contains $regimeLabEnabledRaw.ToLowerInvariant()
$env:FORVEN_ENABLE_REGIME_LAB = if ($regimeLabEnabled) { "1" } else { "0" }
if ([string]::IsNullOrWhiteSpace($env:VITE_ENABLE_REGIME_LAB)) {
    $env:VITE_ENABLE_REGIME_LAB = $env:FORVEN_ENABLE_REGIME_LAB
}
$defaultStartLabWorker = if ($regimeLabEnabled) { "1" } else { "0" }
$startLabWorker = if ($env:START_LAB_WORKER) { $env:START_LAB_WORKER.Trim() } else { $defaultStartLabWorker }
$script:ShowChildWindows = if ($env:SHOW_CHILD_WINDOWS) { $env:SHOW_CHILD_WINDOWS.Trim() } else { "0" }
$forceRestart = if ($env:FORCE_RESTART) { $env:FORCE_RESTART.Trim() } else { "0" }
$detachServices = if ($env:DETACH_SERVICES) { $env:DETACH_SERVICES.Trim() } else { "0" }
if ($startBot -notin @("0","1")) { Throw-StartAllError "START_BOT must be 0 or 1." }
if ($startLabWorker -notin @("0","1")) { Throw-StartAllError "START_LAB_WORKER must be 0 or 1." }
if ($script:ShowChildWindows -notin @("0","1")) { Throw-StartAllError "SHOW_CHILD_WINDOWS must be 0 or 1." }
if ($forceRestart -notin @("0","1")) { Throw-StartAllError "FORCE_RESTART must be 0 or 1." }
if ($detachServices -notin @("0","1")) { Throw-StartAllError "DETACH_SERVICES must be 0 or 1." }

if ([string]::IsNullOrWhiteSpace($env:FORVEN_HOME)) {
    $env:FORVEN_HOME = Join-Path $env:USERPROFILE ".forven"
    Write-Info "FORVEN_HOME not set. Defaulting to $($env:FORVEN_HOME)"
}
try {
    New-Item -Path $env:FORVEN_HOME -ItemType Directory -Force -ErrorAction Stop | Out-Null
    $homeProbe = Join-Path $env:FORVEN_HOME ".forven_write_probe"
    Set-Content -Path $homeProbe -Value "ok" -Encoding UTF8 -ErrorAction Stop
    Remove-Item -Path $homeProbe -Force -ErrorAction SilentlyContinue
} catch {
    $fallbackHome = Join-Path $script:RepoRoot ".forven_home"
    Write-WarnMessage "Could not use FORVEN_HOME '$($env:FORVEN_HOME)': $($_.Exception.Message)"
    Write-WarnMessage "Falling back to $fallbackHome"
    $env:FORVEN_HOME = $fallbackHome
    New-Item -Path $env:FORVEN_HOME -ItemType Directory -Force -ErrorAction Stop | Out-Null
}

$startDaemon = if ($env:START_DAEMON) { $env:START_DAEMON.Trim() } else { "0" }
if ($startDaemon -notin @("0","1")) { Throw-StartAllError "START_DAEMON must be 0 or 1." }

$localTemp = Join-Path $script:RepoRoot ".tmp"
$logRoot = Join-Path $localTemp "logs"
New-Item -Path $localTemp -ItemType Directory -Force | Out-Null
New-Item -Path $logRoot -ItemType Directory -Force | Out-Null
$env:TEMP = $localTemp
$env:TMP = $localTemp

$backendLog = Join-Path $logRoot "unified_backend.log"
$backendErr = Join-Path $logRoot "unified_backend.err.log"
$frontendLog = Join-Path $logRoot "unified_frontend.log"
$frontendErr = Join-Path $logRoot "unified_frontend.err.log"
$botLog = Join-Path $logRoot "forven_bot.log"
$botErr = Join-Path $logRoot "forven_bot.err.log"
$labWorkerLog = Join-Path $logRoot "forven_lab_worker.log"
$labWorkerErr = Join-Path $logRoot "forven_lab_worker.err.log"
$daemonLog = Join-Path $logRoot "forven_daemon.log"
$daemonErr = Join-Path $logRoot "forven_daemon.err.log"

$backendHealth = "http://127.0.0.1:$backendPort/api/health"
$frontendRoot = "http://127.0.0.1:$frontendPort/"
if ([string]::IsNullOrWhiteSpace($env:FORVEN_CLIENT_BASE)) { $env:FORVEN_CLIENT_BASE = "http://127.0.0.1:$backendPort" }
$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) { $script:RepoRoot } else { "$script:RepoRoot;$($env:PYTHONPATH)" }

$npm = Get-NpmCommand
$python = [string](Ensure-Venv | Select-Object -Last 1)
if (-not (Test-ModuleAvailable -PythonCommand $python -ModuleName "forven.api") -or
    -not (Test-ModuleAvailable -PythonCommand $python -ModuleName "uvicorn") -or
    -not (Test-ModuleAvailable -PythonCommand $python -ModuleName "websockets") -or
    -not (Test-ModuleAvailable -PythonCommand $python -ModuleName "multipart")) {
    Install-BackendDeps -Python $python
}
Ensure-FrontendDeps -Npm $npm

# Bootstrap DB schema before API import path that reads kv.
Invoke-Checked -FilePath $python -CommandArgs @("-c","from forven.db import init_db; init_db(); print('db_initialized')")

# Seed core agents so the UI works even if the Discord bot can't connect.
# Idempotent; updates existing rows with latest role/instructions.
Invoke-Checked -FilePath $python -CommandArgs @("-c","from forven.bot import seed_default_agents; r = seed_default_agents(); print('agents_seeded created=' + str(len(r['created'])) + ' updated=' + str(len(r['updated'])) + ' total=' + str(r['total']))")

$backendProc = $null
$frontendProc = $null
$botProc = $null
$labWorkerProc = $null
$daemonProc = $null
$script:StartupSummary = @()
$startupCompleted = $false
$skipCleanup = $false
$script:IntentionalShutdown = $false
$script:WatchdogOwnerLockHeld = $false

# Sentinel file: written when the watchdog loop starts. If the process dies unexpectedly,
# the file remains and the next start_all / standalone watchdog knows services may be orphaned.
$script:WatchdogSentinel = Join-Path $localTemp "watchdog.pid"

try {
    $watchdogClaim = Acquire-WatchdogOwnerLock -OwnerName "start_all"
    $watchdogStatus = $watchdogClaim.status
    if (-not [bool]$watchdogClaim.claimed) {
        $activeOwnerName = if ($null -ne $watchdogStatus -and $null -ne $watchdogStatus.owner_name) { [string]$watchdogStatus.owner_name } else { "watchdog" }
        $activeOwnerPid = if ($null -ne $watchdogStatus -and $null -ne $watchdogStatus.active_pid) { [int]$watchdogStatus.active_pid } else { 0 }
        if ($null -ne $watchdogStatus -and [bool]$watchdogStatus.other_process_active) {
            Write-WarnMessage "Another Forven watchdog owner is already active ($activeOwnerName PID $activeOwnerPid); leaving services untouched."
            Add-StartupSummary -Service "watchdog" -Action "skipped" -Details "owner=$activeOwnerName pid=$activeOwnerPid"
            Write-StartupSummary
            $startupCompleted = $true
            return
        }
        if ($null -ne $watchdogStatus -and [bool]$watchdogStatus.lock_held) {
            Write-WarnMessage "Another Forven watchdog owner appears to be active, but its owner metadata is unavailable; leaving services untouched."
            Add-StartupSummary -Service "watchdog" -Action "skipped" -Details "owner=unknown pid=unknown"
            Write-StartupSummary
            $startupCompleted = $true
            return
        }
        Throw-StartAllError "Could not acquire the Forven watchdog owner lock."
    }
    $script:WatchdogOwnerLockHeld = $true
    Add-StartupSummary -Service "watchdog" -Action "claimed" -Details "owner=start_all pid=$PID"

    # Always kill all existing Forven processes for a clean start
    Write-Info "Stopping all existing Forven processes..."
    Stop-AllForvenProcesses

    Write-Info "Starting Forven services..."

    $backendProc = Ensure-BackendService
    $labWorkerProc = Ensure-LabWorkerService
    $botProc = Ensure-BotService
    $daemonProc = Ensure-DaemonService
    $frontendProc = Ensure-FrontendService

    Write-Info "Ready:"
    Write-Info "  Frontend: http://127.0.0.1:$frontendPort"
    Write-Info "  Backend:  http://127.0.0.1:$backendPort"
    if ($startLabWorker -eq "1") { Write-Info "  Lab Worker: running (Regime Lab queue processor)" }
    if ($startDaemon -eq "1") { Write-Info "  Daemon:   running (data/risk loop)" }
    Write-StartupSummary
    $startupCompleted = $true

    if ($detachServices -eq "1") {
        Write-Info "Detached mode is enabled (DETACH_SERVICES=1); services will keep running after this script exits."
        $skipCleanup = $true
        return
    }

    Write-Info "Press Ctrl+C to stop all started services."
    Write-Info "Service watchdog active - crashed services will be auto-restarted."
    Write-Info "If this window closes unexpectedly, services will keep running."

    # Write sentinel so standalone watchdog knows we're alive
    Set-Content -Path $script:WatchdogSentinel -Value "$PID" -Force

    $watchdogInterval = 10
    $restartCooldown = [TimeSpan]::FromSeconds(30)
    $rapidFailureWindow = [TimeSpan]::FromMinutes(2)
    $rapidFailureThreshold = 5
    $rapidFailureBackoffSeconds = 300

    # Per-service failure tracking. Protects against crash-loops (e.g. invalid
    # Discord token → LoginFailure → exit → restart → repeat).
    # Exit code 78 from a managed service is treated as "config error, do not
    # restart this session" — see forven/bot.py run_bot() LoginFailure handler.
    $script:ServiceState = @{}
    foreach ($svc in @("backend","bot","daemon","frontend")) {
        $script:ServiceState[$svc] = @{
            lastRestart        = [DateTime]::MinValue
            failures           = 0
            unhealthyChecks    = 0
            firstFailureInWin  = [DateTime]::MinValue
            nextAllowedRestart = [DateTime]::MinValue
            permaDisabled      = $false
            disabledReason     = $null
        }
    }

    function Update-ServiceFailure {
        param([string]$Name, [int]$ExitCode)
        $s = $script:ServiceState[$Name]
        if ($s.permaDisabled) { return }

        if ($ExitCode -eq 78) {
            $s.permaDisabled = $true
            $s.disabledReason = "config error (exit 78)"
            Write-WarnMessage "$Name exited with config-error code 78; not restarting this session. See $Name error log for details, fix the configuration, then re-run start_all."
            return
        }

        $now = [DateTime]::Now
        if (($now - $s.firstFailureInWin) -gt $rapidFailureWindow) {
            $s.failures = 0
            $s.firstFailureInWin = $now
        }
        $s.failures += 1

        if ($s.failures -ge $rapidFailureThreshold) {
            $s.nextAllowedRestart = $now.AddSeconds($rapidFailureBackoffSeconds)
            Write-WarnMessage "$Name has failed $($s.failures) times in the last $($rapidFailureWindow.TotalMinutes) minutes; backing off for $rapidFailureBackoffSeconds seconds before the next restart attempt."
        }
    }

    function Test-CanRestartService {
        param([string]$Name)
        $s = $script:ServiceState[$Name]
        if ($s.permaDisabled) { return $false }
        $now = [DateTime]::Now
        if ($now -lt $s.nextAllowedRestart) { return $false }
        if (($now - $s.lastRestart) -lt $restartCooldown) { return $false }
        return $true
    }

    function Get-SafeExitCode {
        param([System.Diagnostics.Process]$Process)
        if ($null -eq $Process) { return 0 }
        try { return [int]$Process.ExitCode } catch { return 0 }
    }

    while ($true) {
        Start-Sleep -Seconds $watchdogInterval

        # In-app self-update: the "Update & restart" action fast-forwards the
        # checkout and drops this sentinel. Bounce the backend so it reloads the
        # pulled code, then clear the sentinel. (The frontend is served by Vite,
        # which hot-reloads source changes on its own.)
        $restartSentinel = Join-Path $script:RepoRoot ".tmp\restart.request"
        if (Test-Path $restartSentinel) {
            Write-Info "Self-update restart requested - bouncing backend to load new code..."
            try { Remove-Item -Force $restartSentinel -ErrorAction Stop } catch { Write-WarnMessage "Could not remove restart sentinel: $($_.Exception.Message)" }
            try {
                Stop-StartedProcessIfRunning -Process $backendProc
                Stop-PortListeners -Port $backendPort
                $backendProc = Start-BackendService
                $script:ServiceState["backend"].lastRestart = [DateTime]::Now
                $script:ServiceState["backend"].unhealthyChecks = 0
                if ($null -ne $backendProc) {
                    Write-Info "Backend restarted (self-update) as PID $($backendProc.Id)"
                } else {
                    Write-Info "Backend healthy after self-update restart."
                }
            } catch {
                Write-WarnMessage "Self-update restart failed: $($_.Exception.Message)"
            }
            continue
        }

        # Watchdog: Backend (critical - check HTTP health)
        $backendHealthy = Test-HttpHealthy -Url $backendHealth
        $backendExited = (($null -ne $backendProc) -and $backendProc.HasExited)
        if ($backendHealthy) {
            $script:ServiceState["backend"].unhealthyChecks = 0
            if ($backendExited) {
                Write-Info "Backend parent PID $($backendProc.Id) exited but the service is still healthy; keeping the current backend."
                $backendProc = $null
            }
        } else {
            $script:ServiceState["backend"].unhealthyChecks += 1
            $backendListenerPids = @(Get-ListeningProcessIds -Port $backendPort)
            $backendProbeCount = [int]$script:ServiceState["backend"].unhealthyChecks
            $shouldRestartBackend = $backendExited -or $backendListenerPids.Count -eq 0 -or $backendProbeCount -ge 2
            if ($shouldRestartBackend) {
                $exitCode = if ($backendExited) { Get-SafeExitCode -Process $backendProc } else { 1 }
                Update-ServiceFailure -Name "backend" -ExitCode $exitCode
                if (Test-CanRestartService -Name "backend") {
                    $backendReason = if ($backendExited) {
                        "exited"
                    } elseif ($backendListenerPids.Count -eq 0) {
                        "no listener"
                    } else {
                        "health check failed $backendProbeCount consecutive time(s); listener PID(s): $($backendListenerPids -join ',')"
                    }
                    Write-WarnMessage "Backend unhealthy ($backendReason) - restarting..."
                    try {
                        $backendProc = Start-BackendService
                        $script:ServiceState["backend"].lastRestart = [DateTime]::Now
                        $script:ServiceState["backend"].unhealthyChecks = 0
                        if ($null -ne $backendProc) {
                            Write-Info "Backend restarted as PID $($backendProc.Id)"
                        } else {
                            Write-Info "Backend service is healthy after restart request."
                        }
                    } catch {
                        Write-WarnMessage "Backend restart failed: $($_.Exception.Message)"
                    }
                }
            } else {
                Write-WarnMessage "Backend health check failed ($backendProbeCount/2); waiting one more watchdog cycle before restart."
            }
        }

        # Watchdog: Bot
        if (($null -ne $botProc) -and $botProc.HasExited) {
            $exitCode = Get-SafeExitCode -Process $botProc
            Update-ServiceFailure -Name "bot" -ExitCode $exitCode
            if (Test-CanRestartService -Name "bot") {
                Write-WarnMessage "Bot (PID $($botProc.Id), exit $exitCode) exited - restarting..."
                try {
                    $botProc = Start-LoggedProcess -FilePath $python `
                        -CommandArgs @("-m","forven","bot","start") `
                        -WorkingDirectory $script:RepoRoot -StdOutPath $botLog -StdErrPath $botErr
                    $script:ServiceState["bot"].lastRestart = [DateTime]::Now
                    Write-Info "Bot restarted as PID $($botProc.Id)"
                } catch {
                    Write-WarnMessage "Bot restart failed: $($_.Exception.Message)"
                }
            } elseif ($script:ServiceState["bot"].permaDisabled) {
                # Stop watching this process once we've decided not to restart it.
                $botProc = $null
            }
        }

        # Watchdog: Daemon
        if (($null -ne $daemonProc) -and $daemonProc.HasExited) {
            $lockStatus = @(Get-DaemonLockStatus)[0]
            $activeDaemonPid = Get-OptionalProperty -Object $lockStatus -Name "active_pid"
            $activeDaemonRunning = [bool](Get-OptionalProperty -Object $lockStatus -Name "active_pid_running")
            if ($activeDaemonPid -and $activeDaemonRunning) {
                try {
                    $daemonProc = Get-Process -Id ([int]$activeDaemonPid) -ErrorAction Stop
                    Write-Info "Daemon parent PID $($daemonProc.Id) exited but daemon lock is held by live PID $activeDaemonPid; tracking the live daemon process."
                } catch {
                    Write-Info "Daemon parent PID exited but daemon lock is held by live PID $activeDaemonPid; keeping current daemon."
                    $daemonProc = $null
                }
            } else {
                $exitCode = Get-SafeExitCode -Process $daemonProc
                Update-ServiceFailure -Name "daemon" -ExitCode $exitCode
                if (Test-CanRestartService -Name "daemon") {
                    Write-WarnMessage "Daemon (PID $($daemonProc.Id), exit $exitCode) exited - restarting..."
                    try {
                        $daemonProc = Start-LoggedProcess -FilePath $python `
                            -CommandArgs @("-m","forven","daemon","start") `
                            -WorkingDirectory $script:RepoRoot -StdOutPath $daemonLog -StdErrPath $daemonErr
                        $script:ServiceState["daemon"].lastRestart = [DateTime]::Now
                        Write-Info "Daemon restarted as PID $($daemonProc.Id)"
                    } catch {
                        Write-WarnMessage "Daemon restart failed: $($_.Exception.Message)"
                    }
                } elseif ($script:ServiceState["daemon"].permaDisabled) {
                    $daemonProc = $null
                }
            }
        }

        # Watchdog: Frontend
        if (($null -ne $frontendProc) -and $frontendProc.HasExited) {
            if (Test-HttpHealthy -Url $frontendRoot) {
                $activeFrontendProc = Get-ListeningProcess -Port $frontendPort
                if ($null -ne $activeFrontendProc) {
                    Write-Info "Frontend parent PID $($frontendProc.Id) exited but port $frontendPort is still served by PID $($activeFrontendProc.Id); tracking the live frontend process."
                    $frontendProc = $activeFrontendProc
                } else {
                    Write-Info "Frontend parent PID $($frontendProc.Id) exited but the service is still healthy; keeping the current frontend."
                    $frontendProc = $null
                }
            } else {
                $exitCode = Get-SafeExitCode -Process $frontendProc
                Update-ServiceFailure -Name "frontend" -ExitCode $exitCode
                if (Test-CanRestartService -Name "frontend") {
                    Write-WarnMessage "Frontend (PID $($frontendProc.Id), exit $exitCode) exited - restarting..."
                    try {
                        $frontendProc = Start-FrontendService
                        $script:ServiceState["frontend"].lastRestart = [DateTime]::Now
                        Write-Info "Frontend restarted as PID $($frontendProc.Id)"
                    } catch {
                        Write-WarnMessage "Frontend restart failed: $($_.Exception.Message)"
                    }
                }
            }
        }
    }
} catch {
    # Any exception in the watchdog loop (including Ctrl+C PipelineStoppedException)
    # is treated as intentional. Terminal close events (CTRL_CLOSE_EVENT) give us
    # ~5 seconds in finally but we skip cleanup so services survive.
    $exType = $_.Exception.GetType().Name
    $script:IntentionalShutdown = ($exType -eq "PipelineStoppedException" -or $exType -eq "StopUpstreamCommandsException")
    if (-not $script:IntentionalShutdown) {
        Write-Host "[start_all][error] $($_.Exception.Message)"
    }
} finally {
    # Clean up the sentinel
    Remove-Item $script:WatchdogSentinel -Force -ErrorAction SilentlyContinue
    if ($script:WatchdogOwnerLockHeld) {
        Release-WatchdogOwnerLock
    }

    if ($skipCleanup) {
        Write-Info "Detached mode - services will keep running."
    } elseif ($script:IntentionalShutdown) {
        Write-Info "Ctrl+C detected - stopping all services..."
        Stop-StartedProcessIfRunning -Process $daemonProc
        Stop-StartedProcessIfRunning -Process $labWorkerProc
        Stop-StartedProcessIfRunning -Process $botProc
        Stop-StartedProcessIfRunning -Process $frontendProc
        Stop-StartedProcessIfRunning -Process $backendProc
    } elseif ($startupCompleted) {
        # Startup already completed a deliberate non-watchdog path.
    } else {
        # Terminal closed, crash, or unexpected exit - LEAVE SERVICES RUNNING.
        # The standalone watchdog (Scheduled Task) or the next start_all invocation
        # will discover and manage the orphaned services via Ensure-* / health checks.
        Write-WarnMessage "Unexpected exit - leaving services running as orphans."
        Write-WarnMessage "The watchdog Scheduled Task will keep them alive."
    }
}
