# Removes the scheduled tasks. Run from your own terminal:
#     pwsh -File scripts\uninstall-task.ps1

$ErrorActionPreference = 'Stop'

foreach ($task in @('BiliPet-Shadow', 'BiliPet-Boot')) {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Write-Host "removed: $task"
    } else {
        Write-Host "not found: $task"
    }
}
