param(
    [ValidatePattern('^\d{2}:\d{2}$')]
    [string]$At = '11:00'
)

$ErrorActionPreference = 'Stop'
$taskName = 'LatviaVacanciesMorning'
$projectFolder = $PSScriptRoot
$trackerPath = Join-Path $projectFolder 'job_tracker.py'
$sourcePath = Join-Path $projectFolder 'job_filter_mcp.py'
$taskMarker = 'Latvia job tracker v1 | '

try {
    $clock = [TimeSpan]::ParseExact($At, 'hh\:mm', [Globalization.CultureInfo]::InvariantCulture)
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw 'Put these files beside your existing job_filter_mcp.py.'
    }
    if (-not (Test-Path -LiteralPath $trackerPath -PathType Leaf)) {
        throw 'job_tracker.py is missing from this folder.'
    }

    Import-Module ScheduledTasks -ErrorAction Stop
    $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uvCommand) {
        $uvPath = $uvCommand.Source
    } else {
        $uvPath = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    }
    if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
        throw 'uv.exe was not found. Restart VS Code and check: uv --version'
    }

    # Resolve the real Python executable now: the scheduled task does not depend on PATH.
    $pythonOutput = & $uvPath run --no-project python -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0) { throw 'uv could not locate Python.' }
    $pythonPath = ([string]($pythonOutput | Select-Object -Last 1)).Trim()
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "Python executable not found: $pythonPath"
    }

    $arguments = '-X utf8 "' + $trackerPath + '"'
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Description -ne ($taskMarker + $trackerPath)) {
            throw "Task $taskName already exists and belongs to another setup. It was not changed."
        }
    }

    Write-Host 'Checking the tracker and creating the initial history...'
    & $pythonPath -X utf8 $trackerPath
    if ($LASTEXITCODE -ne 0) {
        throw 'The tracker reported an error. Fix it and run this installer again; the schedule was not changed.'
    }

    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $projectFolder
    $startAt = [datetime]::Today.Add($clock)
    if ($startAt -le (Get-Date)) { $startAt = $startAt.AddDays(1) }
    $trigger = New-ScheduledTaskTrigger -Daily -At $startAt
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)

    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description ($taskMarker + $trackerPath) -Force | Out-Null

    $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
    Write-Host ''
    Write-Host "READY: $taskName"
    Write-Host "Every day at $At, using this computer's local time."
    Write-Host "State: $($task.State)"
    Write-Host "Next run: $($info.NextRunTime)"
    Write-Host 'The computer must be on, connected to the internet, and you must be signed into Windows.'
    Write-Host "Results folder: $projectFolder"
    Write-Host 'Check vacancies_new.csv, vacancies_history.csv, and the logs folder.'
    Write-Host 'To stop: Disable-ScheduledTask -TaskName LatviaVacanciesMorning'
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
