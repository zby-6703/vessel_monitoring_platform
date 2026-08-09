$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ports = foreach ($port in @(8010, 5173)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    [pscustomobject]@{
        Service = if ($port -eq 8010) { "API" } else { "Web UI" }
        Port = $port
        Status = if ($listener) { "Listening" } else { "Stopped" }
        ProcessId = if ($listener) { $listener.OwningProcess } else { $null }
    }
}
$ports | Format-Table -AutoSize

try {
    $health = Invoke-RestMethod http://127.0.0.1:8010/api/health -TimeoutSec 3
    Write-Host "API health: $($health.status)"
    Write-Host "Database: $($health.dependencies.database)"
    Write-Host "Redis realtime: $($health.dependencies.redis)"
    foreach ($model in $health.models) {
        Write-Host "  $($model.name): $($model.status)"
    }
} catch {
    Write-Host "API health endpoint is unavailable."
}
