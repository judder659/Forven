param([switch]$SmokeTest)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$root = $PSScriptRoot
$temp = Join-Path $root '.tmp/launcher'
New-Item -ItemType Directory -Path $temp -Force | Out-Null
$script:pollProcess = $null
$script:actionProcess = $null
$script:pollResult = ''
$script:actionResult = ''
$script:lastPoll = [datetime]::MinValue
$script:buttons = @()
$script:labels = @{}
$script:presented = $false
$form = New-Object Windows.Forms.Form
$form.Text = 'Forven Launcher'
$form.Size = New-Object Drawing.Size(930,720)
$form.MinimumSize = New-Object Drawing.Size(860,680)
$form.StartPosition = 'CenterScreen'
$form.BackColor = [Drawing.Color]::FromArgb(10,10,10)
$form.ForeColor = [Drawing.Color]::Gainsboro
$form.Font = New-Object Drawing.Font('Segoe UI',10)
$form.AutoScaleMode = 'Dpi'

function New-Label {
    param([string]$Text, [int]$X, [int]$Y, [int]$Width, [int]$Height=28)
    $control = New-Object Windows.Forms.Label
    $control.Text=$Text; $control.SetBounds($X,$Y,$Width,$Height)
    $form.Controls.Add($control)
    return $control
}
function New-Button {
    param([string]$Text,[int]$X,[int]$Y,[int]$Width,[scriptblock]$Click)
    $button=New-Object Windows.Forms.Button
    $button.Text=$Text; $button.SetBounds($X,$Y,$Width,36)
    $button.FlatStyle='Flat'; $button.FlatAppearance.BorderColor=[Drawing.Color]::FromArgb(65,65,65)
    $button.BackColor=[Drawing.Color]::FromArgb(22,22,22)
    $button.ForeColor=[Drawing.Color]::Gainsboro
    $button.Add_Click($Click)
    $form.Controls.Add($button)
    return $button
}
function Start-Helper {
    param([string]$Action,[string]$Service,[string]$Result)
    $args=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $root 'scripts/launcher-control.ps1')+'"'),'-Action',$Action,'-Service',$Service,'-ResultPath',('"'+$Result+'"'))
    $options=@{ FilePath='powershell.exe'; ArgumentList=$args; WorkingDirectory=$root; WindowStyle='Hidden'; PassThru=$true }
    if ($Action -ne 'status') {
        $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
        $principal=New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { $options.Verb='RunAs' }
    }
    return Start-Process @options
}
function Invoke-Control {
    param([string]$Action,[string]$Service)
    if ($script:actionProcess) { return }
    if ($Action -in @('stop','restart') -and $Service -ne 'frontend') {
        $answer=[Windows.Forms.MessageBox]::Show($form,
            'This interrupts Forven workers and may interrupt trade monitoring. Open exchange positions are NOT closed. In-flight research may need recovery. Continue?',
            "$Action $Service",[Windows.Forms.MessageBoxButtons]::YesNo,[Windows.Forms.MessageBoxIcon]::Warning,[Windows.Forms.MessageBoxDefaultButton]::Button2)
        if ($answer -ne [Windows.Forms.DialogResult]::Yes) { return }
    }
    try {
        $script:actionResult=Join-Path $temp (([guid]::NewGuid().ToString())+'.json')
        $script:actionProcess=Start-Helper $Action $Service $script:actionResult
        foreach ($button in $script:buttons) { $button.Enabled=$false }
        $message.Text='Applying service controls. Windows may ask for administrator approval.'
        $message.ForeColor=[Drawing.Color]::Gold
    } catch { $message.Text='Action did not start: '+$_.Exception.Message; $message.ForeColor=[Drawing.Color]::Salmon }
}
$title=New-Label 'FORVEN  /  LAUNCHER' 28 24 700 40
$title.Font=New-Object Drawing.Font('Consolas',20,[Drawing.FontStyle]::Bold)
$null=New-Label 'Start your workspace. See what is running. Recover without Task Manager.' 28 70 840
$script:buttons+=New-Button 'Start Forven' 28 112 150 { Invoke-Control 'start' 'all' }
$script:buttons+=New-Button 'Restart Forven' 190 112 150 { Invoke-Control 'restart' 'all' }
$script:buttons+=New-Button 'Stop Forven' 352 112 150 { Invoke-Control 'stop' 'all' }
$open=New-Button 'Open Forven' 688 112 180 {
    $port=if ($env:VITE_PORT) { [int]$env:VITE_PORT } else { 5173 }
    Start-Process "http://localhost:$port"
}
$open.ForeColor=[Drawing.Color]::Gold
$null=New-Label 'SERVICES' 28 176 300
$checked=New-Label 'Checking current status...' 560 176 308
foreach ($row in @(@('backend','Backend + API workers',214),@('frontend','Frontend',273))) {
    $name=[string]$row[0]; $y=[int]$row[2]
    $null=New-Label $row[1] 28 $y 235
    $script:labels[$name]=New-Label 'Checking...' 270 $y 255
    foreach ($item in @(@('Start','start',546),@('Restart','restart',654),@('Stop','stop',762))) {
        $action=[string]$item[1]; $service=$name
        $button=New-Button $item[0] ([int]$item[2]) ($y-6) 100 { param($sender,$eventArgs) Invoke-Control $sender.Tag.Action $sender.Tag.Service }
        $button.Tag=@{Action=$action;Service=$service}
        $script:buttons+=$button
    }
}
$background=New-Label 'Background services: checking...' 28 320 840
$message=New-Label 'Opening this window does not start or stop trading.' 28 356 840 54
$message.ForeColor=[Drawing.Color]::Gold
$null=New-Label 'Closing the launcher leaves Forven running. Stop Forven stops managed services, not exchange positions.' 28 415 840 40
$logs=New-Object Windows.Forms.TextBox
$logs.Multiline=$true; $logs.ReadOnly=$true; $logs.ScrollBars='Vertical'
$logs.Font=New-Object Drawing.Font('Consolas',9)
$logs.BackColor=[Drawing.Color]::FromArgb(17,17,17); $logs.ForeColor=[Drawing.Color]::Silver
$logs.BorderStyle='FixedSingle'; $logs.SetBounds(28,496,840,130)
$logs.Anchor='Top,Bottom,Left,Right'
$form.Controls.Add($logs)
$null=New-Label 'RECENT SUPERVISOR ACTIVITY' 28 466 510
$null=New-Button 'Open logs folder' 690 453 178 { Start-Process explorer.exe -ArgumentList ('"'+(Join-Path $root '.tmp/logs')+'"') }
$location=New-Label $root 28 640 840
$location.Anchor='Bottom,Left,Right'; $location.ForeColor=[Drawing.Color]::Gray
$timer=New-Object Windows.Forms.Timer
$timer.Interval=500
$timer.Add_Tick({
    try {
        # A hidden console launch can suppress the first native window show.
        # Present the form once after the message loop is active.
        if (-not $script:presented) { $form.Show(); $form.Activate(); $script:presented=$true }
        $actionFinished=$false
        if ($script:actionProcess) {
            $actionFinished=Test-Path -LiteralPath $script:actionResult
            if (-not $actionFinished) {
                try { $actionFinished=$script:actionProcess.HasExited } catch { } # elevated process handles may be unreadable
            }
        }
        if ($actionFinished) {
            if (Test-Path -LiteralPath $script:actionResult) {
                $r=Get-Content -LiteralPath $script:actionResult -Raw | ConvertFrom-Json
                $message.Text=$r.message
                $message.ForeColor=if ($r.ok) { [Drawing.Color]::Gold } else { [Drawing.Color]::Salmon }
                Remove-Item -LiteralPath $script:actionResult -Force
            } else { $message.Text='Action ended without confirmation. Refresh status and inspect the logs.'; $message.ForeColor=[Drawing.Color]::Salmon }
            $script:actionProcess.Dispose(); $script:actionProcess=$null
            foreach ($button in $script:buttons) { $button.Enabled=$true }
            $script:lastPoll=[datetime]::MinValue
        }
        if ($script:pollProcess -and $script:pollProcess.HasExited) {
            if (Test-Path -LiteralPath $script:pollResult) {
                $r=Get-Content -LiteralPath $script:pollResult -Raw | ConvertFrom-Json
                if (-not $r.ok) { throw $r.message }
                foreach ($service in $r.services) {
                    $label=$script:labels[$service.name]; $label.Text=$service.state
                    $label.ForeColor=if ($service.state -eq 'Healthy') { [Drawing.Color]::MediumAquamarine } elseif ($service.state -eq 'Stopped by you') { [Drawing.Color]::Silver } else { [Drawing.Color]::Gold }
                }
                $checked.Text='Status checked '+$r.checked
                $background.Text='Separate background processes: '+ $(if (@($r.background).Count) { $r.background -join ', ' } else { 'none visible (API workers are included above)' })
                if ($r.issues) { $background.Text='Health: '+$r.issues }
                Remove-Item -LiteralPath $script:pollResult -Force
            } else { throw 'Status helper did not return a result.' }
            $script:pollProcess.Dispose(); $script:pollProcess=$null
        }
        if (-not $script:pollProcess -and ((Get-Date)-$script:lastPoll).TotalSeconds -ge 5) {
            $script:lastPoll=Get-Date
            $script:pollResult=Join-Path $temp (([guid]::NewGuid().ToString())+'.json')
            $script:pollProcess=Start-Helper 'status' 'all' $script:pollResult
            $logFiles=@('start_all.log','watchdog.log') | ForEach-Object { Join-Path $root ".tmp/logs/$_" } | Where-Object { Test-Path -LiteralPath $_ }
            $logs.Text=(@($logFiles | ForEach-Object { Get-Content -LiteralPath $_ -Tail 10 } | Sort-Object | Select-Object -Last 10) -join "`r`n")
            $logs.SelectionStart=$logs.TextLength; $logs.ScrollToCaret()
        }
    } catch {
        $checked.Text='Status unavailable - retrying'
        foreach ($label in $script:labels.Values) { $label.Text='Unknown'; $label.ForeColor=[Drawing.Color]::Gold }
        if ($script:pollProcess -and $script:pollProcess.HasExited) { $script:pollProcess.Dispose(); $script:pollProcess=$null }
    }
})
$form.Add_Shown({ $timer.Start() })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })
if ($SmokeTest) { $form.CreateControl(); $form.Dispose(); 'Launcher controls constructed successfully.'; exit 0 }
[Windows.Forms.Application]::Run($form)
