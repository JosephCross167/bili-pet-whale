# Register scheduled tasks for bili-pet.  MUST RUN AS ADMINISTRATOR.
#
#   BiliPet-Boot      at system startup (+2 min), runs the daemon
#   BiliPet-Watchdog  every 10 minutes, restarts the daemon if it died
#
# Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-task.ps1
#
# The file is UTF-8 with BOM on purpose: PowerShell 5.1 mis-reads plain UTF-8.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\pythonw.exe'

if (-not (Test-Path $python)) {
    throw "Python not found: $python"
}

# Never stop just because the machine is busy or unplugged; retry on failure.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# ---- 1. boot daemon
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$bootTrigger.Delay = 'PT2M'
$bootAction = New-ScheduledTaskAction -Execute $python -Argument 'runner.py --daemon' -WorkingDirectory $root

Register-ScheduledTask `
    -TaskName 'BiliPet-Boot' `
    -Action $bootAction `
    -Trigger $bootTrigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'bili-pet daemon (boot start)' `
    -Force | Out-Null
Write-Host 'registered: BiliPet-Boot'

# ---- 2. watchdog every 10 minutes
$watchdogAction = New-ScheduledTaskAction -Execute $python -Argument '-m bili.watchdog' -WorkingDirectory $root

$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(3) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$watchdogSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName 'BiliPet-Watchdog' `
    -Action $watchdogAction `
    -Trigger $watchdogTrigger `
    -Settings $watchdogSettings `
    -Principal $principal `
    -Description 'bili-pet watchdog: restart daemon if it stopped' `
    -Force | Out-Null
Write-Host 'registered: BiliPet-Watchdog'

Write-Host ''
Write-Host 'done. verify with:'
Write-Host '  schtasks /query /tn BiliPet-Boot /v /fo LIST'
Write-Host '  schtasks /query /tn BiliPet-Watchdog /v /fo LIST'
