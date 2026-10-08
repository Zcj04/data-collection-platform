param(
    [string]$PythonPath,
    [string[]]$TestPaths = @("tests/test_data_integrity_guards.py", "tests/test_auth_rbac.py")
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not $PythonPath) {
    $PythonPath = Join-Path $projectRoot ".venv-tests\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Test runtime missing. See docs/testing.md for setup."
}
& $PythonPath -c "import sys,pytest,fastapi,pandas,openai; assert sys.version_info >= (3,10); print('Test runtime:', sys.executable)"
if ($LASTEXITCODE -ne 0) { throw "Test dependencies are incomplete." }
$previousAutoCollection = $env:WORKBUDDY_DISABLE_AUTO_COLLECTION
$previousEncoding = $env:PYTHONIOENCODING
try {
    $env:WORKBUDDY_DISABLE_AUTO_COLLECTION = "1"
    $env:PYTHONIOENCODING = "utf-8"
    & $PythonPath -m pytest @TestPaths -q
    $testExitCode = $LASTEXITCODE
} finally {
    $env:WORKBUDDY_DISABLE_AUTO_COLLECTION = $previousAutoCollection
    $env:PYTHONIOENCODING = $previousEncoding
}
exit $testExitCode
