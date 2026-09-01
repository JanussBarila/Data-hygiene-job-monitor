param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskName = 'LatviaVacanciesMorning'
$folder = $PSScriptRoot
$tracker = Join-Path $folder 'job_tracker.py'
$wrapper = Join-Path $folder 'job_tracker_email.py'
$mailModule = Join-Path $folder 'vacancies_email.py'

try {
    Import-Module ScheduledTasks -ErrorAction Stop
    $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
    if ($task.State -eq 'Running') { throw 'The morning check is running. Wait until it finishes and run this installer again.' }
    $oldMarker = 'Latvia job tracker v1 | ' + $tracker
    if ($task.Description -ne $oldMarker) {
        throw 'This task belongs to a different setup. Run the installer from the folder containing your working job_tracker.py.'
    }
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) { throw 'The existing task has unexpected actions; it was not changed.' }
    $pythonPath = $actions[0].Execute
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'The Python path in the existing task is unavailable.' }
    if (-not (Test-Path -LiteralPath $tracker -PathType Leaf)) { throw 'job_tracker.py is missing.' }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $taskUser = [string]$task.Principal.UserId
    if ($taskUser -ne $sid -and $taskUser -ne [Security.Principal.WindowsIdentity]::GetCurrent().Name) {
        try { $taskSid = ([Security.Principal.NTAccount]::new($taskUser)).Translate([Security.Principal.SecurityIdentifier]).Value }
        catch { throw 'Could not verify the Windows account of the existing task.' }
        if ($taskSid -ne $sid) { throw 'Run this installer as the same Windows user who owns the morning task.' }
    }

    if ($Remove) {
        $action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-X utf8 "' + $tracker + '"') -WorkingDirectory $folder
        Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action | Out-Null
        Write-Host 'Email notifications removed from the schedule. The morning vacancy check stays enabled.'
        exit 0
    }

    foreach ($path in @($wrapper, $mailModule)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing file: $path" }
    }
    Write-Host 'Setting up email. Enter the application password only in the local terminal.'
    & $pythonPath -X utf8 $mailModule --setup
    if ($LASTEXITCODE -ne 0) { throw 'Email setup was not completed. The scheduled task was not changed.' }

    # Keep the existing triggers, principal, network conditions and retry settings.
    $action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('-X utf8 "' + $wrapper + '"') -WorkingDirectory $folder
    Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action | Out-Null
    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
    Write-Host "EMAIL SCHEDULE READY. Next run: $($info.NextRunTime)"
    Write-Host 'Running the first fresh check now. If there are matching vacancies, it sends one email.'
    & $pythonPath -X utf8 $wrapper
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'The schedule is updated, but the first check or email failed. See logs/email_*.log and logs/run_*.log.'
        exit 1
    }
    Write-Host 'DONE. Check your inbox and spam folder. No new matching vacancies means no email.'
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
