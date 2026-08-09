<#!
.SYNOPSIS
Checks whether Docker Desktop is available for VesselSight deployment.

.DESCRIPTION
Uses docker.exe from PATH when available, otherwise checks the standard
per-user Docker Desktop installation location. It does not start, stop, or
modify any containers.
#>

$ErrorActionPreference = "Stop"
$PlatformRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Find-DockerExe {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
    )
    return $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}

$docker = Find-DockerExe
if (-not $docker) {
    throw "Docker CLI was not found. Install and start Docker Desktop, then rerun this script."
}

$dockerBin = Split-Path -Parent $docker
$env:PATH = "$dockerBin;$env:PATH"
Write-Host "Docker CLI: $docker"
& $docker version
& $docker compose version

$envFile = Join-Path $PlatformRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Warning ".env is missing. Run: Copy-Item .env.example .env"
    return
}

$content = Get-Content -Raw $envFile
$required = "MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "API_INGEST_TOKEN"
$missing = foreach ($name in $required) {
    if ($content -notmatch "(?m)^$name=.+$") { $name }
}
if ($missing) {
    Write-Warning ".env is missing required deployment settings: $($missing -join ', ')"
    Write-Host "Copy the corresponding values from .env.example and replace the placeholder values."
    return
}

Push-Location $PlatformRoot
try {
    & $docker compose config -q
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose configuration validation failed."
    }
    Write-Host "Docker Compose configuration: valid"
} finally {
    Pop-Location
}
