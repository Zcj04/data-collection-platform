param(
    [string]$PythonPath,
    [switch]$DisableAutoCollection
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeCandidates = @(
    (Join-Path $projectRoot ".venv\Scripts\python.exe"),
    (Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions\3.13.12.old.9068\python.exe"),
    (Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions\3.13.12\python.exe"),
    (Join-Path $env:USERPROFILE ".workbuddy\binaries\python\envs\default\Scripts\python.exe")
)
if ($PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Specified PythonPath does not exist."
    }
    $runtimeCandidates = @((Resolve-Path -LiteralPath $PythonPath).Path)
}
$runtime = $runtimeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $runtime) {
    throw "No Python runtime is available. Create .venv or supply -PythonPath."
}

Set-Location -LiteralPath $projectRoot
if ($DisableAutoCollection) {
    # Process-local only: manual collection endpoints remain available after
    # the network is restored, while startup schedules make no upstream calls.
    $env:WORKBUDDY_DISABLE_AUTO_COLLECTION = "1"
    Write-Host "Automatic collection and member monitoring are disabled for this service process."
} else {
    Remove-Item Env:WORKBUDDY_DISABLE_AUTO_COLLECTION -ErrorAction SilentlyContinue
}
while ($true) {
    & $runtime -u "app_fastapi.py"
    $serviceExitCode = $LASTEXITCODE
    if ($serviceExitCode -eq 0) {
        exit 0
    }
    Write-Warning "WorkBuddy service exited with code $serviceExitCode; restarting in 15 seconds."
    Start-Sleep -Seconds 15
}
