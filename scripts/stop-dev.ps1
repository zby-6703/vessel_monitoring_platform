$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProcessFile = Join-Path $PlatformRoot "data\logs\dev-processes.json"
if (-not (Test-Path $ProcessFile)) {
    Write-Host "No managed development processes were found."
    return
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId $child.ProcessId
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

$processes = Get-Content -Raw $ProcessFile | ConvertFrom-Json
foreach ($processId in @($processes.api, $processes.web)) {
    Stop-ProcessTree -ProcessId $processId
}
Remove-Item -LiteralPath $ProcessFile -Force
Write-Host "VesselSight development services stopped."

