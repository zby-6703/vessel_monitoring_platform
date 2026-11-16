@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
title VesselSight 船舶智能监测平台

:menu
echo.
echo ==============================================
echo          VesselSight 船舶智能监测平台
echo ==============================================
echo   [1] 开启平台并打开默认浏览器
echo   [2] 关闭平台（等待录像收尾）
echo   [0] 退出此窗口
echo.
echo   Web 网址：http://127.0.0.1:5173
echo   退出窗口不会关闭正在运行的平台。
echo.
choice /c 120 /n /m "请选择 [1/2/0]："
if errorlevel 4 exit /b 1
if errorlevel 3 exit /b 0
if errorlevel 2 goto stop_platform
if errorlevel 1 goto start_platform
exit /b 1

:start_platform
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-field.ps1" %*
set "VESSELSIGHT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%VESSELSIGHT_EXIT_CODE%"=="0" goto start_failed
echo 平台已开启，Web 网址：http://127.0.0.1:5173
echo 关闭浏览器或此窗口后，平台仍会运行。
echo 摄像头采集请在页面中手动开启。
if not defined VESSELSIGHT_NO_BROWSER start "" "http://127.0.0.1:5173"
goto action_done

:start_failed
echo [错误] 开启失败，退出码：%VESSELSIGHT_EXIT_CODE%
echo 请查看上方错误及 data\logs 中的日志。
echo 首次使用请按 docs\field-deployment.md 完成部署。
echo 如果平台已运行，可直接打开：http://127.0.0.1:5173
goto action_done

:stop_platform
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-field.ps1" %*
set "VESSELSIGHT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%VESSELSIGHT_EXIT_CODE%"=="0" goto stop_failed
echo 平台已关闭。
goto action_done

:stop_failed
echo [错误] 关闭失败，退出码：%VESSELSIGHT_EXIT_CODE%
echo 请查看上方错误，确认录像收尾完成后再关闭电脑。

:action_done
if not defined VESSELSIGHT_NO_PAUSE pause
goto menu
