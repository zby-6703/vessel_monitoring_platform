param(
    [string]$CondaExe = $env:CONDA_EXE,
    [string]$Environment = "Train",
    [switch]$FieldMode
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'conda-common.ps1')
$CondaExe = Resolve-PlatformConda $CondaExe
$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $PlatformRoot "backend"
$FrontendRoot = Join-Path $PlatformRoot "frontend"
$LogRoot = Join-Path $PlatformRoot "data\logs"
New-Item -ItemType Directory -Force $LogRoot | Out-Null
$EnvFile = Join-Path $PlatformRoot ".env"
$ProcessFile = Join-Path $LogRoot "dev-processes.json"

if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $PlatformRoot ".env.example") $EnvFile
    Write-Host "Created .env from .env.example. Review model and stream settings before production use."
}
if (-not (Test-Path (Join-Path $FrontendRoot "node_modules"))) {
    throw "Frontend dependencies are missing. Run: cd frontend; npm install"
}
if ($FieldMode -and -not (Test-Path (Join-Path $FrontendRoot 'dist\index.html'))) {
    throw 'Frontend build missing. Run .\scripts\setup.ps1 first.'
}
foreach ($port in @(8010, 5173)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $ownerInfo = if ($owner) { "$($owner.ProcessName) (PID $($owner.Id))" } else { "PID $($listener.OwningProcess)" }
        throw "Port $port is already in use by $ownerInfo. Run .\scripts\status.ps1 to check the platform, or .\scripts\stop-dev.ps1 before restarting."
    }
}

$previousAppEnv = $env:APP_ENV
try {
    if ($FieldMode) { $env:APP_ENV = 'production' }
    $api = Start-Process -FilePath $CondaExe -ArgumentList @("run", "-n", $Environment, "python", "run.py") -WorkingDirectory $BackendRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "api.log") -RedirectStandardError (Join-Path $LogRoot "api.error.log") -PassThru
} finally { $env:APP_ENV = $previousAppEnv }
$webArguments = if ($FieldMode) { @('run', 'preview', '--', '--host', '0.0.0.0', '--port', '5173', '--strictPort') } else { @('run', 'dev') }
$web = Start-Process -FilePath "npm.cmd" -ArgumentList $webArguments -WorkingDirectory $FrontendRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "web.log") -RedirectStandardError (Join-Path $LogRoot "web.error.log") -PassThru

@{ api = $api.Id; web = $web.Id; field_mode = [bool]$FieldMode; started_at = (Get-Date).ToString("o") } | ConvertTo-Json | Set-Content -Encoding utf8 $ProcessFile

$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $apiReady = Get-NetTCPConnection -State Listen -LocalPort 8010 -ErrorAction SilentlyContinue
    $webReady = Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue
} until (($apiReady -and $webReady) -or (Get-Date) -gt $deadline)

if (-not ($apiReady -and $webReady)) {
    throw "Services did not become ready. Check data\logs\api.error.log and web.error.log."
}
Write-Host "API: http://127.0.0.1:8010/api/docs"
Write-Host "UI:  http://127.0.0.1:5173"
Write-Host "Offline analysis: http://127.0.0.1:5173/offline"
