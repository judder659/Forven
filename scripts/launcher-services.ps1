# Shared by the desktop launcher and both existing supervisors. No side effects on import.
function Test-LauncherServiceEnabled {
    param([string]$Root, [string]$Service = 'all')
    if (Test-Path -LiteralPath (Join-Path $Root '.tmp/launcher.all.stopped')) { return $false }
    return -not (Test-Path -LiteralPath (Join-Path $Root ".tmp/launcher.$Service.stopped"))
}

function Get-LauncherProcessRole {
    param([string]$CommandLine, [string]$ProcessName = '')
    if ($ProcessName -and $ProcessName -notmatch '^(?i:pythonw?\.exe|node\.exe|powershell\.exe|pwsh\.exe)$') { return '' }
    if ($ProcessName -match '^(?i:powershell|pwsh)\.exe$') {
        if ($CommandLine -match '(?i)(?:^|\s)-File\s+"?[^"\r\n]*[\\/]start_all\.ps1(?:["\s]|$)') { return 'supervisor' }
        return ''
    }
    if ($CommandLine -match '(?i)(?:-m\s+forven\.api\b|forven\.api:app)') { return 'backend' }
    if ($CommandLine -match '(?i)\bforven\s+daemon\s+start\b') { return 'daemon' }
    if ($CommandLine -match '(?i)\bforven\s+bot\s+start\b') { return 'bot' }
    if ($CommandLine -match '(?i)\bforven\s+lab\s+worker\b') { return 'lab' }
    if ($CommandLine -match '(?i)[\\/]vite[\\/].*\.(?:js|mjs)') { return 'frontend' }
    if ($CommandLine -match '(?i)[\\/]start_all\.ps1(?:["\s]|$)') { return 'supervisor' }
    return ''
}

function Get-LauncherOwnedProcesses {
    param([string]$Root, [object[]]$Snapshot)
    # Require a complete path boundary. "forven-upgrade" is NOT "forven".
    $rootPattern = '(?i)' + [regex]::Escape($Root.TrimEnd('\','/')) + '(?:[\\/"\s]|$)'
    if ($null -eq $Snapshot) { $Snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop) }
    $owned = @{}
    foreach ($proc in $Snapshot) {
        $cmd = [string]$proc.CommandLine
        $processName = if ($proc.PSObject.Properties['Name']) { [string]$proc.Name } else { '' }
        $role = Get-LauncherProcessRole $cmd $processName
        if ($role -and $cmd -match $rootPattern) {
            $owned[[int]$proc.ProcessId] = [pscustomobject]@{ Id=[int]$proc.ProcessId; Role=$role; Created=$proc.CreationDate; Parent=[int]$proc.ParentProcessId; ServiceProcess=$true }
        }
    }
    # Include Python's system-interpreter child and worker descendants, but never
    # infer ownership from a shared interpreter name or a listening port alone.
    do {
        $added = $false
        foreach ($proc in $Snapshot) {
            $id = [int]$proc.ProcessId
            $parent = [int]$proc.ParentProcessId
            if (-not $owned.ContainsKey($id) -and $owned.ContainsKey($parent)) {
                $parentRole = $owned[$parent].Role
                if ($parentRole -eq 'supervisor') {
                    $childName = if ($proc.PSObject.Properties['Name']) { [string]$proc.Name } else { '' }
                    $parentRole = Get-LauncherProcessRole ([string]$proc.CommandLine) $childName
                    if (-not $parentRole) { $parentRole = 'bootstrap' }
                }
                $owned[$id] = [pscustomobject]@{ Id=$id; Role=$parentRole; Created=$proc.CreationDate; Parent=$parent; ServiceProcess=((Get-LauncherProcessRole ([string]$proc.CommandLine)) -eq $parentRole) }
                $added = $true
            }
        }
    } while ($added)
    return @($owned.Values)
}

function Stop-LauncherOwnedProcesses {
    param([string]$Root, [string[]]$Roles)
    $targets = @(Get-LauncherOwnedProcesses -Root $Root | Where-Object { $_.Role -in $Roles })
    # Stop descendants before their parents. Verify creation time against PID reuse.
    while ($targets.Count) {
        $parents = @($targets | ForEach-Object { $_.Parent })
        $leaves = @($targets | Where-Object { $_.Id -notin $parents })
        if (-not $leaves.Count) { throw 'Unable to resolve the Forven process tree.' }
        foreach ($target in $leaves) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.Id)" -ErrorAction Stop
            if ($current -and $current.CreationDate -eq $target.Created) {
                Stop-Process -Id $target.Id -Force -ErrorAction Stop
                Wait-Process -Id $target.Id -Timeout 10 -ErrorAction SilentlyContinue
            }
        }
        $leafIds = @($leaves | ForEach-Object { $_.Id })
        $targets = @($targets | Where-Object { $_.Id -notin $leafIds })
    }
}
