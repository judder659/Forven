<#
.SYNOPSIS
  Run Forven's trading bots as a separate, low-privilege Windows account.

.DESCRIPTION
  By default bots run as your own Windows user. They never get Forven's master
  encryption key, but a compromised bot could still read your profile (including
  the key file) and change Forven's code. This script, run ONCE from an
  ADMINISTRATOR PowerShell in the Forven folder, sets up a dedicated account:

    1. Creates a local account (default "forven-bot") with a long random
       password nobody needs to know, hidden from the sign-in screen.
    2. Lets that account read and run Forven's code and Python (no writes), and
       read and write Forven's data folder (database, market data, bot memory).
    3. Explicitly denies it the master key (wherever it lives) and .env files.
    4. Stores the password, encrypted with Forven's key, beside that key, and
       runs a check that starts a probe as the account.

  Bots switch to the account the next time each one starts. Nothing else about
  your install changes. Undo everything with -Remove.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-bot-user.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\setup-bot-user.ps1 -Remove
#>
param(
    [string]$UserName = 'forven-bot',
    [string]$ForvenHome = '',
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an administrator PowerShell (right-click PowerShell, "Run as administrator").'
}

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
if (-not $ForvenHome) {
    $ForvenHome = if ($env:FORVEN_HOME) { $env:FORVEN_HOME } else { & $python -c "from forven.config import FORVEN_HOME; print(FORVEN_HOME)" }
}
$ForvenHome = $ForvenHome.Trim()
$prefixes = @((& $python -c "import sys; print(sys.prefix); print(sys.base_prefix)") | Sort-Object -Unique)
$operatorSecrets = Join-Path $env:LOCALAPPDATA 'Forven'

function Invoke-Icacls {
    param([string[]]$IcaclsArgs)
    $out = & icacls @IcaclsArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "icacls $($IcaclsArgs -join ' ') failed: $out" }
}

$codePaths = @($root) + $prefixes
$deniedFiles = @(
    (Join-Path $ForvenHome '.forven_key'),
    (Join-Path $root '.env'),
    (Join-Path $ForvenHome '.env')
)

if ($Remove) {
    & $python -m forven bot-account clear
    foreach ($path in $codePaths + @($ForvenHome, $operatorSecrets) + $deniedFiles) {
        if (Test-Path $path) { & icacls $path /remove $UserName | Out-Null }
    }
    if (Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue) { Remove-LocalUser -Name $UserName }
    $hidden = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
    Remove-ItemProperty -Path $hidden -Name $UserName -ErrorAction SilentlyContinue
    Write-Host "Removed $UserName. Bots run as your own user again from their next start."
    return
}

# The password is encrypted with Forven's key, which lives under the account
# running this script. An elevated prompt opened as a different admin account
# would encrypt it with the wrong key.
if (-not (Test-Path (Join-Path $operatorSecrets '.forven_key')) -and -not (Test-Path (Join-Path $ForvenHome '.forven_key')) -and -not $env:FORVEN_ENCRYPTION_KEY) {
    throw "Forven's key was not found for $env:USERNAME. Open the administrator PowerShell as the same Windows user that runs Forven."
}

# 1. The account, with a random password nobody types.
$bytes = New-Object byte[] 48
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$plain = [Convert]::ToBase64String($bytes)
$secure = ConvertTo-SecureString $plain -AsPlainText -Force
if (Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $UserName -Password $secure -PasswordNeverExpires $true
} else {
    New-LocalUser -Name $UserName -Password $secure -PasswordNeverExpires -AccountNeverExpires `
        -UserMayNotChangePassword -Description 'Runs Forven trading bots with limited access' | Out-Null
}
# Built-in Users group by SID, so non-English Windows works too.
Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $UserName -ErrorAction SilentlyContinue
$hidden = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
New-Item -Path $hidden -Force | Out-Null
New-ItemProperty -Path $hidden -Name $UserName -Value 0 -PropertyType DWord -Force | Out-Null

# 2. Read-and-run on code and Python; read-write on the data folder.
foreach ($path in $codePaths) { Invoke-Icacls @($path, '/grant', "${UserName}:(OI)(CI)RX") }
New-Item -ItemType Directory -Path (Join-Path $ForvenHome 'bot-runtime') -Force | Out-Null
Invoke-Icacls @($ForvenHome, '/grant', "${UserName}:(OI)(CI)M")

# 3. Explicit denies: these win over any grant.
foreach ($file in $deniedFiles) { if (Test-Path $file) { Invoke-Icacls @($file, '/deny', "${UserName}:F") } }
if (Test-Path $operatorSecrets) { Invoke-Icacls @($operatorSecrets, '/deny', "${UserName}:(OI)(CI)F") }

# 4. Store the password and prove it works.
$plain | & $python -m forven bot-account set --username "$env:COMPUTERNAME\$UserName" --password-stdin
if ($LASTEXITCODE -ne 0) { throw 'Could not store the bot account.' }
$plain = $null
& $python -m forven bot-account check
if ($LASTEXITCODE -ne 0) {
    # Leave bots running as before rather than half-isolated.
    & $python -m forven bot-account clear | Out-Null
    Write-Warning 'The check failed (see above), so bots keep running as your own user. Fix the problem and run this script again, or run it with -Remove to undo the account and permissions.'
    exit 1
}
Write-Host ''
Write-Host "Done. Each bot runs as $UserName from its next start. Restart running bots from the Bot Factory page to switch them now."
