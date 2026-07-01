$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainPy = Join-Path $ProjectRoot "main.py"

$Candidates = @(
    (Join-Path $env:APPDATA "uv\python\cpython-3.14-windows-x86_64-none\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    "python.exe",
    "py.exe"
)

$Python = $null
foreach ($Candidate in $Candidates) {
    try {
        $Resolved = Get-Command $Candidate -ErrorAction Stop
        $Python = $Resolved.Source
        break
    } catch {
        if (Test-Path -LiteralPath $Candidate) {
            $Python = $Candidate
            break
        }
    }
}

if (-not $Python) {
    throw "No approved Python executable was found. Install Python or use uv to install one."
}

Set-Location -LiteralPath $ProjectRoot
& $Python $MainPy @args
