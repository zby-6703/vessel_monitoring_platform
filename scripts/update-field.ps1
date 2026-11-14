param(
    [string]$CondaExe = $env:CONDA_EXE,
    [string]$Environment = 'Train',
    [ValidateSet('Cpu', 'Gpu')][string]$Runtime = 'Cpu'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'conda-common.ps1')
$CondaExe = Resolve-PlatformConda $CondaExe
$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $PlatformRoot
try {
    $changes = & git status --porcelain --untracked-files=no
    Assert-PlatformCommand 'Checking tracked changes'
    if ($changes) { throw 'Tracked source files have local changes. Commit or resolve them before updating. Private .env and data can remain.' }
    & $CondaExe run -n $Environment python scripts/prepare-update.py
    Assert-PlatformCommand 'Draining capture'
    & (Join-Path $PSScriptRoot 'stop-dev.ps1')
    & git pull --ff-only
    Assert-PlatformCommand 'Pulling code'
    & (Join-Path $PSScriptRoot 'setup.ps1') -CondaExe $CondaExe -Environment $Environment -Runtime $Runtime
    & (Join-Path $PSScriptRoot 'start-field.ps1') -CondaExe $CondaExe -Environment $Environment
} finally { Pop-Location }
