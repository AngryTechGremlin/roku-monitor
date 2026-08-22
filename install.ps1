<#
.SYNOPSIS
  Install roku-monitor as a per-user logon task on Windows.
.DESCRIPTION
  Registers a Scheduled Task that runs the daemon with pythonw.exe in your
  interactive session at every logon. No administrator rights are needed and
  nothing is installed system-wide: config lives in %APPDATA%\roku-monitor and
  logs in %LOCALAPPDATA%\roku-monitor.
.PARAMETER NoDiscover
  Skip the interactive "which TV?" step.
.PARAMETER NoStart
  Register the task but do not start the daemon now.
#>
[CmdletBinding()]
param([switch]$NoDiscover, [switch]$NoStart)

$ErrorActionPreference = 'Stop'
$App     = 'roku-monitor'
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script  = Join-Path $RepoDir 'roku_monitor.py'
$ConfDir = Join-Path $env:APPDATA $App
$Conf    = Join-Path $ConfDir 'env'
$LogDir  = Join-Path $env:LOCALAPPDATA $App

function Say  ($m) { Write-Host $m -ForegroundColor Cyan }
function Warn ($m) { Write-Host "warning: $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "error: $m" -ForegroundColor Red; exit 1 }

# --- prerequisites -----------------------------------------------------------
# The WindowsApps "python.exe" is a Store stub that only opens the Store page,
# so resolve a real interpreter rather than trusting whatever is first in PATH.
$python = $null
foreach ($cand in @('py', 'python')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($cmd.Source -like '*\WindowsApps\*') { continue }
    try {
        $exe = if ($cand -eq 'py') { (& $cmd.Source -3 -c "import sys; print(sys.executable)" 2>$null) } else { $cmd.Source }
        if ($exe -and (Test-Path $exe)) { $python = $exe; break }
    } catch { }
}
if (-not $python) {
    Die "no real Python found. Install it with:`n    winget install -e --id Python.Python.3.12`nthen open a new terminal and run this script again."
}
$ver = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]'3.10') { Die "Python $ver is too old; 3.10 or newer is required (found $python)" }
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Warn "pythonw.exe not found next to $python; the daemon will run with a console window"
    $pythonw = $python
}
if (-not (Test-Path $Script)) { Die "roku_monitor.py not found next to this script ($RepoDir)" }
Say "using $python (v$ver)"

# --- config ------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $ConfDir, $LogDir | Out-Null
if (-not (Test-Path $Conf)) {
    Copy-Item (Join-Path $RepoDir '.env.example') $Conf
    Say "created $Conf"
} else {
    Say "keeping existing $Conf"
}

function Set-Conf($key, $value) {
    if ($value -match "[`r`n]") { Die "refusing to write ${key}: value contains a newline" }
    $lines = Get-Content $Conf
    if ($lines -match "^$key=") {
        $lines = $lines -replace "^$key=.*", "$key=$value"
    } else {
        $lines += "$key=$value"
    }
    Set-Content -Path $Conf -Value $lines -Encoding ASCII
}

function Read-Input() {
    while ($true) {
        $i = Read-Host 'Which input is the PC on? [hdmi1/hdmi2/hdmi3/hdmi4/av1/tuner] (hdmi3)'
        if (-not $i) { $i = 'hdmi3' }
        $i = $i.ToLower() -replace '\s', ''
        if ($i -match '^[1-4]$') { $i = "hdmi$i" }
        if ($i -match '^(hdmi[1-4]|av1|tuner)$') { Set-Conf 'ROKU_INPUT' $i; return $i }
        Warn "'$i' is not one of hdmi1..4/av1/tuner"
    }
}

# Parses the "ROKU_TV_IP=..." block that `discover` prints for a chosen TV.
function Use-DiscoverOutput($text) {
    $ip     = ($text | Select-String '^\s*ROKU_TV_IP=(.+)$').Matches.Groups[1].Value
    if (-not $ip) { return $false }
    $serial = ($text | Select-String '^\s*ROKU_TV_SERIAL=(.*)$').Matches.Groups[1].Value
    $mac    = ($text | Select-String '^\s*ROKU_TV_MAC=(.*)$').Matches.Groups[1].Value
    Set-Conf 'ROKU_TV_IP' $ip.Trim()
    if ($serial -match '^[A-Za-z0-9]{6,}$') { Set-Conf 'ROKU_TV_SERIAL' $serial.Trim() }
    if ($mac -match '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$') { Set-Conf 'ROKU_TV_MAC' $mac.Trim() }
    $inp = Read-Input
    Say "wrote ROKU_TV_IP=$($ip.Trim()) ROKU_INPUT=$inp to $Conf"
    return $true
}

function Ask-ByIp() {
    $manual = Read-Host "TV's IP address (or blank to skip)"
    if (-not $manual) { return $false }
    $out = & $python $Script discover --ip $manual 2>&1
    $out | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { return $false }
    return (Use-DiscoverOutput $out)
}

if (-not $NoDiscover) {
    Say 'Looking for Roku TVs on your network (turn the TV on first)...'
    $out = & $python $Script discover 2>&1
    $out | ForEach-Object { Write-Host $_ }
    $rows = @($out | Select-String '^\s*\d+\s+[\d\.]+\s' | Where-Object { $_ -notmatch '\(no ECP answer' })
    if ($rows.Count -gt 0) {
        $pick = Read-Host "Which one is the TV you use as a monitor? [1-$($rows.Count), i to type an IP, or s to skip]"
        if ($pick -match '^\d+$' -and [int]$pick -ge 1 -and [int]$pick -le $rows.Count) {
            $f = ($rows[[int]$pick - 1].ToString().Trim() -split '\s+')
            Set-Conf 'ROKU_TV_IP' $f[1]
            if ($f[-3] -match '^[A-Za-z0-9]{6,}$') { Set-Conf 'ROKU_TV_SERIAL' $f[-3] }
            if ($f[-2] -match '^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$') { Set-Conf 'ROKU_TV_MAC' $f[-2] }
            $inp = Read-Input
            Say "wrote ROKU_TV_IP=$($f[1]) ROKU_INPUT=$inp to $Conf"
        } elseif ($pick -eq 'i') {
            if (-not (Ask-ByIp)) { Say "skipped; edit $Conf by hand (ROKU_TV_IP is required)" }
        } else {
            Say "skipped; edit $Conf by hand (ROKU_TV_IP is required)"
        }
    } else {
        # Discovery is multicast; some networks never deliver it to a given TV.
        Warn 'no Roku answered the search (some networks block multicast).'
        if (-not (Ask-ByIp)) { Warn "edit $Conf by hand: ROKU_TV_IP is required" }
    }
}

# --- logon task --------------------------------------------------------------
# Must run in the interactive session: the daemon listens for display-power
# messages on a hidden window, which only exists in a real user session.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$Script`" run" -WorkingDirectory $RepoDir
# Not "$env:USERDOMAIN\$env:USERNAME": on a workgroup machine USERDOMAIN can be
# WORKGROUP, which Task Scheduler rejects as a principal. The identity name is
# always right (MACHINE\user locally, DOMAIN\user when joined).
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $me
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
# ExecutionTimeLimit 0 = no limit; the default (3 days) would kill the daemon.
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
                -StartWhenAvailable
$haveTask = $false
try {
    Register-ScheduledTask -TaskName $App -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'Roku TV as a monitor: mirror what the GPU is driving' -Force | Out-Null
    $haveTask = $true
    Say "registered scheduled task '$App' (runs at logon, as $me)"
    # A previous run may have left the fallback behind; two copies would fight.
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $App -ErrorAction SilentlyContinue
} catch {
    Warn "could not register the scheduled task ($($_.Exception.Message)); falling back to a Run key (no auto-restart)"
    New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $App `
        -Value "`"$pythonw`" `"$Script`" run" -PropertyType String -Force | Out-Null
}

if (-not $NoStart) {
    if ($haveTask) {
        try { Start-ScheduledTask -TaskName $App; Start-Sleep -Seconds 3; Say 'started' }
        catch { Warn "could not start: $($_.Exception.Message)" }
    } else {
        Say 'sign out and back in to start it (or run it once by hand)'
    }
}

Say 'done.'
Write-Host "  config: $Conf"
Write-Host "  log:    $(Join-Path $LogDir "$App.log")"
Write-Host "  status: & '$python' '$Script' status"
Write-Host "  task:   Get-ScheduledTaskInfo -TaskName $App"
