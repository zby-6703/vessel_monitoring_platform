$ErrorActionPreference = "Stop"
$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProcessFile = Join-Path $PlatformRoot "data\logs\dev-processes.json"

function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0 -or $ProcessId -eq $PID) { return }
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId $child.ProcessId
    }
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    }
}

function Test-PlatformListener {
    param($Process, [int]$Port)
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    $commandLine = $Process.CommandLine
    if ($Port -eq 5173) {
        $frontendPath = Join-Path $PlatformRoot "frontend\node_modules\"
        return ($Process.Name -eq "node.exe" -and
            $commandLine.IndexOf($frontendPath, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine -match 'vite[\\/]bin[\\/]vite\.js')
    }
    if ($Process.Name -notmatch '^python(w)?\.exe$' -or
        $commandLine -notmatch '(uvicorn\s+app\.main:app\b|\brun\.py\b)') {
        return $false
    }
    # Manually launched Python processes may only have relative paths in their
    # command line. Confirm the API identity before stopping a port owner.
    try {
        $service = Invoke-RestMethod "http://127.0.0.1:$Port/" -TimeoutSec 3
        return $service.service -eq "VesselSight Operations API"
    } catch {
        return $false
    }
}

$stopped = $false
if (Test-Path $ProcessFile) {
    try {
        $processes = Get-Content -Raw $ProcessFile | ConvertFrom-Json
        foreach ($processId in @($processes.api, $processes.web)) {
            if ($processId -gt 0) {
                $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
                # Ignore a recorded PID that has since been reused.
                if ($process -and $processes.started_at -and
                    $process.CreationDate -gt ([datetime]$processes.started_at).AddSeconds(2)) {
                    Write-Warning "Ignoring reused managed PID $processId."
                    continue
                }
                if ($process) {
                    Stop-ProcessTree -ProcessId $processId
                    $stopped = $true
                }
            }
        }
    } catch {
        Write-Warning "Could not stop services from the process record: $($_.Exception.Message)"
    }
}

# Also find services started directly, or left running without the PID file.
foreach ($port in @(8010, 5173)) {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($ownerId in @($listeners.OwningProcess | Sort-Object -Unique)) {
        if (-not $ownerId) { continue }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" -ErrorAction SilentlyContinue
        if (Test-PlatformListener -Process $process -Port $port) {
            Stop-ProcessTree -ProcessId $ownerId
            $stopped = $true
            Write-Host "Stopped platform service on port $port (PID $ownerId)."
        } else {
            Write-Warning "Port $port belongs to an unrecognized process (PID $ownerId); leaving it running."
        }
    }
}

$remaining = @(Get-NetTCPConnection -State Listen -LocalPort 8010,5173 -ErrorAction SilentlyContinue)
if ($remaining.Count -gt 0) {
    throw "Ports still listening: $($remaining.LocalPort -join ', '). Check .\scripts\status.ps1."
}
if (Test-Path $ProcessFile) {
    Remove-Item -LiteralPath $ProcessFile -Force
}
if ($stopped) {
    Write-Host "VesselSight development services stopped."
} else {
    Write-Host "VesselSight development services are already stopped."
}

