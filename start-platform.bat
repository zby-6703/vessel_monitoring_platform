@echo off
setlocal DisableDelayedExpansion
title VesselSight - Start platform

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-field.ps1" %*
set "VESSELSIGHT_EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%VESSELSIGHT_EXIT_CODE%"=="0" goto failed

echo Platform is running: http://127.0.0.1:5173
echo Closing this window will leave the platform running.
echo Use stop-platform.bat to stop it.
if not defined VESSELSIGHT_NO_BROWSER start "" "http://127.0.0.1:5173"
goto done

:failed
echo [ERROR] Platform startup failed. See the message above and data\logs.
echo Complete the first-time setup in docs\field-deployment.md before starting.
echo If the platform is already running, open http://127.0.0.1:5173 directly.

:done
if not defined VESSELSIGHT_NO_PAUSE pause
exit /b %VESSELSIGHT_EXIT_CODE%
