$ErrorActionPreference = "Stop"

$taskName = "WorkBuddy-Data-Collection-8010"
$runtimeCandidates = @(
    "C:\Users\youruser\.workbuddy\binaries\python\versions\3.13.12.old.9068\python.exe",
    "C:\Users\youruser\.workbuddy\binaries\python\versions\3.13.12\python.exe",
    "C:\Users\youruser\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)

Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
$listeners = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8010 `
    -State Listen -ErrorAction SilentlyContinue
$launcherProcessIds = @()
foreach ($processId in @($listeners.OwningProcess | Sort-Object -Unique)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
    if (-not $process) {
        continue
    }
    if ($runtimeCandidates -notcontains $process.ExecutablePath -or
            $process.CommandLine -notmatch "app_fastapi\.py") {
        throw "PID $processId on 127.0.0.1:8010 is not the expected WorkBuddy service."
    }
    $launcher = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" `
        -ErrorAction SilentlyContinue
    if ($launcher -and $launcher.Name -ieq "powershell.exe" -and
            $launcher.CommandLine -match "run_fastapi_service\.ps1") {
        $launcherProcessIds += $launcher.ProcessId
    }
    Stop-Process -Id $processId -Force -ErrorAction Stop
}

foreach ($launcherProcessId in @($launcherProcessIds | Sort-Object -Unique)) {
    Stop-Process -Id $launcherProcessId -Force -ErrorAction Stop
}

if (Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8010 `
        -State Listen -ErrorAction SilentlyContinue) {
    throw "127.0.0.1:8010 is still listening after the controlled stop."
}
