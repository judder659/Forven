# Isolated process/intent regression checks. Never stops a real service.
$ErrorActionPreference='Stop'
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts/launcher-services.ps1')
$testRoot=Join-Path ([IO.Path]::GetTempPath()) ('forven-launcher-test-'+[guid]::NewGuid())
New-Item -ItemType Directory -Path (Join-Path $testRoot '.tmp') -Force | Out-Null
function Assert-True { param($Value,[string]$Message) if (-not $Value) { throw $Message } }
try {
    Assert-True (Test-LauncherServiceEnabled $testRoot backend) 'Default backend should be enabled'
    Set-Content (Join-Path $testRoot '.tmp/launcher.backend.stopped') 'test'
    Assert-True (-not (Test-LauncherServiceEnabled $testRoot backend)) 'Backend stop intent lost'
    Assert-True (Test-LauncherServiceEnabled $testRoot frontend) 'Backend stop disabled frontend'
    Set-Content (Join-Path $testRoot '.tmp/launcher.all.stopped') 'test'
    Assert-True (-not (Test-LauncherServiceEnabled $testRoot frontend)) 'Global stop intent lost'
    $fixtureRoot='C:\Users\Test User\Projects\forven'
    $snapshot=@(
        [pscustomobject]@{ProcessId=10;ParentProcessId=1;CreationDate='a';CommandLine='"C:\Users\Test User\Projects\forven\.venv\Scripts\python.exe" -m forven.api --port 8003'},
        [pscustomobject]@{ProcessId=11;ParentProcessId=10;CreationDate='b';CommandLine='"C:\Python\python.exe" -m forven.api --port 8003'},
        [pscustomobject]@{ProcessId=12;ParentProcessId=11;CreationDate='c';CommandLine='"C:\Python\python.exe" -c worker'},
        [pscustomobject]@{ProcessId=20;ParentProcessId=1;CreationDate='d';CommandLine='"C:\Users\Test User\Projects\forven-upgrade\.venv\Scripts\python.exe" -m forven.api --port 8013'},
        [pscustomobject]@{ProcessId=30;ParentProcessId=1;CreationDate='e';CommandLine='"C:\Python\python.exe" other.py'},
        [pscustomobject]@{ProcessId=40;ParentProcessId=1;CreationDate='f';CommandLine='node "C:\Users\Test User\Projects\forven\frontend\node_modules\vite\bin\vite.js"'},
        [pscustomobject]@{ProcessId=50;ParentProcessId=1;CreationDate='g';CommandLine='powershell -File "C:\Users\Test User\Projects\forven\start_all.ps1"'}
    )
    $actual=@(Get-LauncherOwnedProcesses $fixtureRoot $snapshot)
    Assert-True ($actual.Count -eq 5) 'Unexpected process ownership count'
    Assert-True (20 -notin $actual.Id) 'Adjacent checkout was claimed'
    Assert-True (30 -notin $actual.Id) 'Unrelated Python was claimed'
    Assert-True (@($actual | Where-Object Role -eq backend).Count -eq 3) 'Backend descendants missing'
    Assert-True (@($actual | Where-Object Role -eq frontend).Count -eq 1) 'Frontend missing'
    Assert-True (@($actual | Where-Object Role -eq supervisor).Count -eq 1) 'Supervisor missing'
    Assert-True ((Get-LauncherProcessRole 'powershell -Command "Get-Content C:\forven\forven.api"' 'powershell.exe') -eq '') 'Diagnostic shell was claimed as backend'
    Assert-True ((Get-LauncherProcessRole 'node arbitrary.js forven.api:app' 'unrelated.exe') -eq '') 'Unrelated executable was claimed'
    $snapshot += [pscustomobject]@{ProcessId=51;ParentProcessId=50;CreationDate='h';Name='python.exe';CommandLine='C:\Python\python.exe -c init_db'}
    $withBootstrap=@(Get-LauncherOwnedProcesses $fixtureRoot $snapshot)
    Assert-True (@($withBootstrap | Where-Object Role -eq bootstrap).Count -eq 1) 'Bootstrap child would be orphaned'
    'Launcher service tests: 13 checks passed.'
} finally {
    # Explicit generated test files only; no recursive cleanup or app paths.
    foreach ($leaf in @('launcher.backend.stopped','launcher.all.stopped')) {
        Remove-Item -LiteralPath (Join-Path $testRoot ".tmp/$leaf") -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath (Join-Path $testRoot '.tmp') -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $testRoot -ErrorAction SilentlyContinue
}
