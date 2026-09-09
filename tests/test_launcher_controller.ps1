# Exercises real start/stop IPC with a disposable bootstrap, never the app.
$ErrorActionPreference='Stop'
$repo=Split-Path -Parent $PSScriptRoot
$fixture=Join-Path ([IO.Path]::GetTempPath()) ('forven-launcher-fixture-'+[guid]::NewGuid())
New-Item -ItemType Directory -Path (Join-Path $fixture 'scripts') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $fixture '.tmp') -Force | Out-Null
foreach ($name in @('launcher-services.ps1','launcher-control.ps1')) { Copy-Item -LiteralPath (Join-Path $repo "scripts/$name") -Destination (Join-Path $fixture "scripts/$name") }
Set-Content -LiteralPath (Join-Path $fixture 'start_all.ps1') -Value 'Set-Content -LiteralPath (Join-Path $PSScriptRoot ".tmp/bootstrap.started") -Value "started"; Start-Sleep -Seconds 60'
$savedBackend=$env:FORVEN_PORT; $savedFrontend=$env:VITE_PORT
$env:FORVEN_PORT='48003'; $env:VITE_PORT='48004'
$resultFile=Join-Path $fixture '.tmp/result.json'
function Run-Control {
    param([string]$Action,[string]$Service='all')
    Remove-Item -LiteralPath $resultFile -Force -ErrorAction SilentlyContinue
    $helper=Start-Process powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $fixture 'scripts/launcher-control.ps1')+'"'),'-Action',$Action,'-Service',$Service,'-ResultPath',('"'+$resultFile+'"')) -WindowStyle Hidden -PassThru
    $deadline=(Get-Date).AddSeconds(30)
    while (-not (Test-Path -LiteralPath $resultFile)) {
        if ((Get-Date) -ge $deadline) { throw 'Controller result timed out' }
        Start-Sleep -Milliseconds 200
    }
    $helper.Dispose()
    return Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
}
try {
    $r=Run-Control stop
    if (-not $r.ok -or -not (Test-Path (Join-Path $fixture '.tmp/launcher.all.stopped'))) { throw 'Stop did not save global intent' }
    $r=Run-Control start backend
    if ($r.ok) { throw 'Partial start incorrectly bypassed global stop' }
    $r=Run-Control start
    if (-not $r.ok) { throw $r.message }
    Start-Sleep -Seconds 2
    if (-not (Test-Path (Join-Path $fixture '.tmp/bootstrap.started'))) { throw 'Bootstrap was not launched' }
    if (Test-Path (Join-Path $fixture '.tmp/launcher.all.stopped')) { throw 'Start failed to clear stop intent' }
    . (Join-Path $fixture 'scripts/launcher-services.ps1')
    if (@(Get-LauncherOwnedProcesses -Root $fixture).Count -eq 0) { throw 'Fixture must be alive before testing stop' }
    $r=Run-Control stop
    if (-not $r.ok) { throw $r.message }
    . (Join-Path $fixture 'scripts/launcher-services.ps1')
    if (@(Get-LauncherOwnedProcesses -Root $fixture).Count) { throw 'Fixture process survived stop' }
    'Launcher controller: global stop, guarded partial start, bootstrap start, and process stop passed.'
} finally {
    $null=Run-Control stop
    $env:FORVEN_PORT=$savedBackend; $env:VITE_PORT=$savedFrontend
}
