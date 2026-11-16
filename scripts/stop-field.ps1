param([string]$CondaExe = $env:CONDA_EXE, [string]$Environment = 'Train')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'conda-common.ps1')

# An already stopped API needs neither Python nor a camera connection.
$apiListener = Get-NetTCPConnection -State Listen -LocalPort 8010 -ErrorAction SilentlyContinue
if ($apiListener) {
    $CondaExe = Resolve-PlatformConda $CondaExe
    Write-Host 'Finishing local capture and recording before stopping services...'
    & $CondaExe run --no-capture-output -n $Environment python (Join-Path $PSScriptRoot 'prepare-update.py')
    Assert-PlatformCommand 'Finishing capture and recording; services were left running'
}

& (Join-Path $PSScriptRoot 'stop-dev.ps1')
