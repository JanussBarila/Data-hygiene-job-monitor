$ErrorActionPreference = 'Stop'
$projectFolder = $PSScriptRoot
$guiPath = Join-Path $projectFolder 'email_settings.py'
$pythonPath = $null

try {
    if (-not (Test-Path -LiteralPath $guiPath -PathType Leaf)) {
        throw 'Extract all files beside your existing job_tracker.py, then double-click Email_Settings.cmd.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $projectFolder 'job_tracker.py') -PathType Leaf)) {
        throw 'Move all add-on files into the folder containing your existing job_tracker.py, then double-click Email_Settings.cmd.'
    }
    try {
        $task = Get-ScheduledTask -TaskName 'LatviaVacanciesMorning' -TaskPath '\' -ErrorAction Stop
        $candidate = [string](@($task.Actions)[0].Execute)
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and
            ([IO.Path]::GetFileName($candidate) -match '^python(w)?\.exe$')) {
            $pythonPath = $candidate
        }
    } catch {
        # An existing task is preferred; uv is also supported for an initial setup.
    }
    if (-not $pythonPath) {
        $uvPath = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
        if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
            $uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue
            if ($uvCommand) { $uvPath = $uvCommand.Source }
        }
        if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
            throw 'Python could not be located. Check your existing vacancy tracker installation.'
        }
        $output = & $uvPath run --no-project python -c 'import sys; print(sys.executable)'
        if ($LASTEXITCODE -ne 0) { throw 'uv could not locate Python.' }
        $pythonPath = ([string]($output | Select-Object -Last 1)).Trim()
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Python executable not found.' }
    & $pythonPath -X utf8 -c 'import tkinter'
    if ($LASTEXITCODE -ne 0) {
        throw 'This Python is missing Tcl/Tk. Install Python with Tcl/Tk support, or use the existing console setup until it is available.'
    }
    $pythonWindowed = Join-Path ([IO.Path]::GetDirectoryName($pythonPath)) 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonWindowed -PathType Leaf) {
        Start-Process -FilePath $pythonWindowed -ArgumentList ('-X utf8 "' + $guiPath + '"') -WorkingDirectory $projectFolder | Out-Null
    } else {
        & $pythonPath -X utf8 $guiPath
        if ($LASTEXITCODE -ne 0) { throw 'The settings window could not be opened.' }
    }
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
