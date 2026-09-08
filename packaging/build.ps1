[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Version = "1.0.1",
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

& $Python packaging\build_icon.py
if ($LASTEXITCODE -ne 0) { throw "Windows icon generation failed." }

if (-not (Test-Path -LiteralPath "packaging\app_icon.ico")) {
    throw "Missing packaging\app_icon.ico."
}

$SourceVersion = (& $Python -c "from app.version import __version__; print(__version__)" | Select-Object -Last 1).Trim()
if ($LASTEXITCODE -ne 0 -or $SourceVersion -ne $Version) {
    throw "Build version $Version does not match app.version $SourceVersion."
}

if (-not $SkipTests) {
    $TestData = Join-Path $ProjectRoot ("build\test-data-" + [guid]::NewGuid().ToString("N"))
    $ResolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
    $ResolvedTest = [IO.Path]::GetFullPath($TestData)
    if (-not $ResolvedTest.StartsWith($ResolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe test data path: $ResolvedTest"
    }
    $env:RUC_AGENT_DATA_DIR = $ResolvedTest
    try {
        & $Python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }
        & $Python -m compileall -q app tests server.py desktop.py
        if ($LASTEXITCODE -ne 0) { throw "Python compile check failed." }
        if (Get-Command node -ErrorAction SilentlyContinue) {
            & node --check static\app.js
            if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed." }
        }
    }
    finally {
        Remove-Item Env:RUC_AGENT_DATA_DIR -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $ResolvedTest) {
            Remove-Item -LiteralPath $ResolvedTest -Recurse -Force
        }
    }
}

& $Python -c "import PyInstaller, pystray, PIL, uiautomation, wechatauto"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies are missing. Run: python -m pip install -r packaging\requirements-build.txt"
}

& $Python -m PyInstaller --noconfirm --clean packaging\ruc_agent.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$SmokeData = Join-Path $ProjectRoot ("build\smoke-data-" + $Version)
$env:RUC_AGENT_DATA_DIR = $SmokeData
try {
    $Process = Start-Process -FilePath "dist\RUCAgent\RUCAgent.exe" -ArgumentList "--smoke-test" -WindowStyle Hidden -Wait -PassThru
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
    & $Iscc "/DMyAppVersion=$Version" "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Installer build failed." }
}

$Artifacts = @("dist\RUCAgent\RUCAgent.exe")
if (-not $SkipInstaller) {
    $Artifacts += "dist\installer\RUC-Agent-Setup-$Version.exe"
}
$ChecksumFile = "dist\installer\SHA256SUMS-$Version.txt"
$Checksums = foreach ($Artifact in $Artifacts) {
    $Hash = Get-FileHash -Algorithm SHA256 -LiteralPath $Artifact
    "{0}  {1}" -f $Hash.Hash.ToLowerInvariant(), (Split-Path -Leaf $Artifact)
}
$Checksums | Set-Content -LiteralPath $ChecksumFile -Encoding ascii

Write-Host "Build complete: dist\RUCAgent\RUCAgent.exe"
if (-not $SkipInstaller) {
    Write-Host "Installer: dist\installer\RUC-Agent-Setup-$Version.exe"
}
Write-Host "Checksums: $ChecksumFile"
