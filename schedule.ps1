<#
  Register a Windows scheduled task that rebuilds the dashboard and updates
  the WorkBuddy Library page. Runs windowless; output goes to <out>\daily.log.

  .\schedule.ps1                    # 08:00 and 23:00 daily
  .\schedule.ps1 -Times 22:30,07:00
  .\schedule.ps1 -NoPublish         # local dashboard only
  .\schedule.ps1 -Remove
#>
param(
    [string[]]$Times = @("08:00", "23:00"),
    [string]$Out = $(if ($env:WB_USAGE_OUT) { $env:WB_USAGE_OUT } else { Join-Path $HOME "wb-usage" }),
    [string]$TaskName = "WbUsageDaily",
    [switch]$NoPublish,
    [switch]$Remove
)

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task $TaskName"
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$script = Join-Path $PSScriptRoot "wb_usage.py"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$log = Join-Path $Out "daily.log"

$argList = "`"$script`" --out `"$Out`" --log `"$log`""
if (-not $NoPublish) { $argList += " --publish" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $argList -WorkingDirectory $Out
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Refresh the WorkBuddy usage dashboard" -Force | Out-Null
Write-Host "Task $TaskName runs daily at $($Times -join ', '); log: $log"
