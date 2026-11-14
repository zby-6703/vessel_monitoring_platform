function Resolve-PlatformConda {
    param([string]$CondaExe)
    if ([string]::IsNullOrWhiteSpace($CondaExe)) { $CondaExe = $env:CONDA_EXE }
    if ([string]::IsNullOrWhiteSpace($CondaExe)) {
        $command = Get-Command conda.exe -ErrorAction SilentlyContinue
        if ($command) { $CondaExe = $command.Source }
    }
    if ([string]::IsNullOrWhiteSpace($CondaExe) -or -not (Test-Path -LiteralPath $CondaExe -PathType Leaf)) {
        throw 'Conda not found. Pass -CondaExe <your Miniconda directory>\Scripts\conda.exe.'
    }
    return (Resolve-Path -LiteralPath $CondaExe).Path
}

function Assert-PlatformCommand {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}
