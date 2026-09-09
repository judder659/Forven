# Compile with Windows' bundled .NET/PowerShell; no downloaded dependencies.
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$output=Join-Path $root 'ForvenLauncher.exe'
if (Test-Path -LiteralPath $output) { throw 'ForvenLauncher.exe already exists. Close it and remove that file before rebuilding.' }
Add-Type -Path (Join-Path $PSScriptRoot 'ForvenLauncher.cs') -OutputAssembly $output -OutputType WindowsApplication -ReferencedAssemblies @('System.dll','System.Core.dll','System.Windows.Forms.dll',[System.Management.Automation.PSObject].Assembly.Location)
Write-Output "Built $output"
