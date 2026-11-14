param([string]$CondaExe = $env:CONDA_EXE, [string]$Environment = 'Train')
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-dev.ps1') -CondaExe $CondaExe -Environment $Environment -FieldMode
