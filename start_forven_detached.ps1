$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo
$env:START_BOT = '0'
$env:START_DAEMON = '0'
$env:START_LAB_WORKER = '0'
$env:DETACH_SERVICES = '1'
$env:FORCE_RESTART = '1'
$env:SHOW_CHILD_WINDOWS = '0'
& (Join-Path $repo 'start_all.ps1')
