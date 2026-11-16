function Resolve-PlatformConda {
    param([string]$CondaExe)
    if ([string]::IsNullOrWhiteSpace($CondaExe)) { $CondaExe = $env:CONDA_EXE }
    if ([string]::IsNullOrWhiteSpace($CondaExe)) {
        $command = Get-Command conda.exe -ErrorAction SilentlyContinue
        if ($command) { $CondaExe = $command.Source }
    }
    if ([string]::IsNullOrWhiteSpace($CondaExe)) {
        # Explorer-launched .bat files do not inherit an activated Conda shell.
        # Windows Python registration also covers custom Conda install drives.
        $candidates = @(
            foreach ($registryRoot in @(
                'HKCU:\Software\Python',
                'HKLM:\Software\Python',
                'HKLM:\Software\WOW6432Node\Python'
            )) {
                Get-ChildItem -LiteralPath $registryRoot -Recurse -ErrorAction SilentlyContinue |
                    Where-Object { $_.PSChildName -eq 'InstallPath' } |
                    ForEach-Object {
                        $installDirectory = $_.GetValue('')
                        if ($installDirectory -is [string] -and $installDirectory) {
                            Join-Path $installDirectory 'Scripts\conda.exe'
                        }
                    }
            }
            foreach ($installRoot in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:ProgramData)) {
                if (-not $installRoot) { continue }
                foreach ($distribution in @('miniconda3', 'anaconda3', 'miniforge3', 'mambaforge')) {
                    Join-Path $installRoot "$distribution\Scripts\conda.exe"
                }
            }
        )
        $CondaExe = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
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
