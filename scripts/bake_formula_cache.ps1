# Recalculate all formulas via Excel COM and save, so formula results
# are cached inside the file. Fixes blank cells when the workbook is
# opened in WPS / online previews / converters that do not honor
# fullCalcOnLoad for formulas without cached values.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File bake_formula_cache.ps1 -Path <xlsx>
# Exit codes: 0 ok; 2 Excel unavailable; 3 open/save failed
param(
    [Parameter(Mandatory = $true)][string]$Path
)
$ErrorActionPreference = "Stop"
$Path = [System.IO.Path]::GetFullPath($Path)
if (-not (Test-Path -LiteralPath $Path)) {
    [Console]::Error.WriteLine("file-not-found: $Path")
    exit 3
}
try {
    $excel = New-Object -ComObject Excel.Application
} catch {
    [Console]::Error.WriteLine("excel-unavailable")
    exit 2
}
$excel.Visible = $false
$excel.DisplayAlerts = $false
$excel.AskToUpdateLinks = $false
try { $excel.AutomationSecurity = 3 } catch {}
try {
    $wb = $excel.Workbooks.Open($Path, 0, $false)
    try {
        $null = $excel.CalculateFullRebuild()
        $wb.Save()
        [Console]::Error.WriteLine("baked")
        exit 0
    } finally {
        $wb.Close($false)
    }
} catch {
    [Console]::Error.WriteLine("bake-failed: " + $_.Exception.Message)
    exit 3
} finally {
    try { $excel.Quit() } catch {}
    try { [System.Runtime.Interopservices.Marshal]::ReleaseComObject($excel) | Out-Null } catch {}
}
