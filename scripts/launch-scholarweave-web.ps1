param(
    [string]$ApiUrl = "http://127.0.0.1:1102",
    [switch]$Check,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Host.UI.RawUI.WindowTitle = "ScholarWeave Web"
try {
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Create .venv and install ScholarWeave first; see README.md.'
    }
    $arguments = @("-m", "scholarweave_tui", "--web", "--build-frontend", "--api-url", $ApiUrl)
    if ($Check) { $arguments += "--check" }
    & $python @arguments
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 130) {
        throw "ScholarWeave exited with code $LASTEXITCODE. See the error above."
    }
}
catch {
    Write-Host "`n$($_.Exception.Message)" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}
