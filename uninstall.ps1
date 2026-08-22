<#
.SYNOPSIS
  Remove the roku-monitor logon task from Windows.
.PARAMETER Purge
  Also delete %APPDATA%\roku-monitor (config) and %LOCALAPPDATA%\roku-monitor (logs).
.NOTES
  Stopping the daemon turns the TV off, unless ROKU_OFF_ON_STOP=false.
#>
[CmdletBinding()]
param([switch]$Purge)

$App     = 'roku-monitor'
$ConfDir = Join-Path $env:APPDATA $App
$LogDir  = Join-Path $env:LOCALAPPDATA $App

# WM_CLOSE (taskkill without /F) lets the daemon run its normal stop path.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*roku_monitor.py*' } |
    ForEach-Object { & taskkill.exe /PID $_.ProcessId | Out-Null }
Start-Sleep -Seconds 2

try { Stop-ScheduledTask -TaskName $App -ErrorAction SilentlyContinue } catch { }
try { Unregister-ScheduledTask -TaskName $App -Confirm:$false -ErrorAction Stop; Write-Host "removed scheduled task '$App'" }
catch { Write-Host "no scheduled task '$App'" }

Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $App -ErrorAction SilentlyContinue

if ($Purge) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ConfDir, $LogDir
    Write-Host "removed $ConfDir and $LogDir"
} else {
    Write-Host "kept $ConfDir (use -Purge to delete config and logs)"
}
