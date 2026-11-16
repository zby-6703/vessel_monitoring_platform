param([string]$CondaExe = $env:CONDA_EXE, [string]$Environment = 'Train')
$ErrorActionPreference = 'Stop'

# Save with a UTF-8 BOM so Windows PowerShell 5.1 reads Chinese correctly.
# CMD only reads the ASCII launcher, regardless of its initial code page.
$consoleEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $consoleEncoding
[Console]::OutputEncoding = $consoleEncoding
$OutputEncoding = $consoleEncoding
$Host.UI.RawUI.WindowTitle = 'VesselSight 船舶智能监测平台'
$platformUrl = 'http://127.0.0.1:5173'

while ($true) {
    Write-Host ''
    Write-Host '=============================================='
    Write-Host '         VesselSight 船舶智能监测平台'
    Write-Host '=============================================='
    Write-Host '  [1] 开启平台并打开默认浏览器'
    Write-Host '  [2] 关闭平台（等待录像收尾）'
    Write-Host '  [0] 退出此窗口'
    Write-Host ''
    Write-Host "  Web 网址：$platformUrl"
    Write-Host '  退出窗口不会关闭正在运行的平台。'
    Write-Host ''
    $selection = Read-Host '请选择 [1/2/0]，然后按回车'
    if ($null -eq $selection -or ([Console]::IsInputRedirected -and $selection -eq '')) { return }

    switch ($selection.Trim()) {
        '0' { return }
        '1' {
            try {
                & (Join-Path $PSScriptRoot 'start-field.ps1') -CondaExe $CondaExe -Environment $Environment
                Write-Host ''
                Write-Host "平台已开启，Web 网址：$platformUrl"
                Write-Host '关闭浏览器或此窗口后，平台仍会运行。'
                Write-Host '摄像头采集请在页面中手动开启。'
                if (-not $env:VESSELSIGHT_NO_BROWSER) {
                    try { Start-Process -FilePath $platformUrl -ErrorAction Stop | Out-Null }
                    catch { Write-Warning "无法自动打开浏览器，请手动访问：$platformUrl" }
                }
            } catch {
                Write-Host "[错误] 开启失败：$($_.Exception.Message)" -ForegroundColor Red
                Write-Host '请查看上方错误及 data\logs 中的日志。首次使用请按 docs\field-deployment.md 完成部署。'
                Write-Host "如果平台已运行，可直接打开：$platformUrl"
            }
        }
        '2' {
            try {
                & (Join-Path $PSScriptRoot 'stop-field.ps1') -CondaExe $CondaExe -Environment $Environment
                Write-Host '平台已关闭。'
            } catch {
                Write-Host "[错误] 关闭失败：$($_.Exception.Message)" -ForegroundColor Red
                Write-Host '请查看上方错误，确认录像收尾完成后再关闭电脑。'
            }
        }
        default {
            Write-Host '请输入 1、2 或 0。'
            continue
        }
    }
    if (-not $env:VESSELSIGHT_NO_PAUSE) { [void](Read-Host '按回车键返回菜单') }
}
