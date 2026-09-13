param([switch]$NoDesktopShortcut)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .venv and install .[tui] first; see README.md.'
}
& $python -c "import textual, uvicorn"
if ($LASTEXITCODE -ne 0) {
    throw 'Run: .\.venv\Scripts\python.exe -m pip install -e ".[tui]"'
}
$powershell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$launcher = Join-Path $root "scripts\launch-scholarweave.ps1"
$shell = New-Object -ComObject WScript.Shell
$folders = @([Environment]::GetFolderPath("Programs"))
if (-not $NoDesktopShortcut) { $folders += [Environment]::GetFolderPath("Desktop") }
foreach ($folder in $folders) {
    if (-not $folder) { throw "Could not find the Windows shortcut folder." }
    $path = Join-Path $folder "ScholarWeave.lnk"
    $link = $shell.CreateShortcut($path)
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
    if ((Test-Path -LiteralPath $path) -and
        ($link.TargetPath -ne $powershell -or $link.Arguments -ne $arguments)) {
        throw "An unrelated shortcut already exists at $path. It was not replaced."
    }
    $link.TargetPath = $powershell
    $link.Arguments = $arguments
    $link.WorkingDirectory = $root
    $link.Description = "Build ScholarWeave, start the backend, and open the terminal cockpit"
    $link.Save()
    Write-Output "Created: $path"
}
Write-Output "Right-click ScholarWeave in Start (or its shortcut) and choose Pin to taskbar."
