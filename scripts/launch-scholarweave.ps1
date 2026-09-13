param(
    [string]$ApiUrl = "http://127.0.0.1:8000",
    [switch]$Check,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Host.UI.RawUI.WindowTitle = "ScholarWeave"
try {
    $arguments = @("-m", "scholarweave_tui", "--build-frontend", "--api-url", $ApiUrl)
    if ($Check) { $arguments += "--check" }
    & (Join-Path $root ".venv\Scripts\python.exe") @arguments
    if ($LASTEXITCODE -ne 0) { throw "ScholarWeave exited with code $LASTEXITCODE. See the error above." }
}
catch {
    Write-Host "`n$($_.Exception.Message)" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}
