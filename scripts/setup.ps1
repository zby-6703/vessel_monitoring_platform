param(
    [string]$CondaExe = $env:CONDA_EXE,
    [string]$Environment = 'Train',
    [ValidateSet('Cpu', 'Gpu')][string]$Runtime = 'Cpu',
    [switch]$SkipDependencies
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'conda-common.ps1')
$CondaExe = Resolve-PlatformConda $CondaExe
$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $PlatformRoot
try {
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Install Node.js (including npm) before setup.' }
    if (-not $SkipDependencies) {
        $environments = & $CondaExe env list --json | ConvertFrom-Json
        Assert-PlatformCommand 'Listing Conda environments'
        $exists = @($environments.envs | Where-Object { (Split-Path $_ -Leaf) -eq $Environment }).Count -gt 0
        if (-not $exists) {
            & $CondaExe create -y -n $Environment -c conda-forge python=3.11 pip ffmpeg
            Assert-PlatformCommand 'Creating Python environment'
        }
        & $CondaExe run -n $Environment python -c "import sys; assert sys.version_info[:2] == (3, 11), 'Train must use Python 3.11'"
        Assert-PlatformCommand 'Checking Python version'
        $opposite = if ($Runtime -eq 'Cpu') { 'onnxruntime-gpu' } else { 'onnxruntime' }
        $installed = & $CondaExe run -n $Environment python -c "from importlib.metadata import distributions; print(any(d.metadata['Name'].lower() == '$opposite' for d in distributions()))"
        Assert-PlatformCommand 'Checking ONNX runtime'
        if (($installed | Out-String).Trim() -eq 'True') { throw "Environment contains $opposite. Use the matching -Runtime option; do not install both ONNX runtimes together." }
        & $CondaExe run -n $Environment python -m pip install -r "backend/requirements-field-$($Runtime.ToLower()).txt"
        Assert-PlatformCommand 'Installing pinned Python dependencies'
        Push-Location frontend
        try { & npm.cmd ci; Assert-PlatformCommand 'Installing locked frontend dependencies' } finally { Pop-Location }
    }
    & $CondaExe run -n $Environment python scripts/prepare-local-config.py --runtime ($Runtime.ToLower())
    Assert-PlatformCommand 'Preparing private config'
    Push-Location frontend
    try { & npm.cmd run build; Assert-PlatformCommand 'Building frontend' } finally { Pop-Location }
    & $CondaExe run -n $Environment python scripts/check-deployment.py --verify-models
    Assert-PlatformCommand 'Offline deployment verification'
    Write-Host 'Ready. Configure the camera locally, then run .\scripts\start-field.ps1.'
} finally { Pop-Location }
