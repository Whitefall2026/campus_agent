[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not $SkipTests) {
    & $Python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        & node --check static\app.js
        if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed." }
    }
}

& $Python packaging\build_icon.py
if ($LASTEXITCODE -ne 0) { throw "Windows icon generation failed." }

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Run: python -m pip install -r packaging\requirements-build.txt"
}

& $Python -c "import wechatauto" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "WeChat runtime dependencies are missing. Run: python -m pip install -r packaging\requirements-build.txt"
}

& $Python -m PyInstaller --noconfirm --clean packaging\ruc_agent.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$SmokeData = Join-Path $ProjectRoot "build\smoke-data"
$env:RUC_AGENT_DATA_DIR = $SmokeData
try {
    $Process = Start-Process -FilePath "dist\RUCAgent\RUCAgent.exe" -ArgumentList "--smoke-test" -Wait -PassThru
}
finally {
    Remove-Item Env:RUC_AGENT_DATA_DIR -ErrorAction SilentlyContinue
}
if ($Process.ExitCode -ne 0) {
    throw "Packaged application smoke test failed with exit code $($Process.ExitCode)."
}

if (-not $SkipInstaller) {
    $IsccCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    )
    $Iscc = $IsccCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $Iscc) {
        $Command = Get-Command iscc -ErrorAction SilentlyContinue
        if ($Command) { $Iscc = $Command.Source }
    }
    if (-not $Iscc) {
        throw "Inno Setup 6 is missing. Install it with: winget install --id JRSoftware.InnoSetup -e"
    }
    & $Iscc "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed." }
}

Write-Host "Build complete: dist\RUCAgent\RUCAgent.exe"
if (-not $SkipInstaller) {
    Write-Host "Installer: dist\installer\RUC-Agent-Setup-1.0.0.exe"
}
