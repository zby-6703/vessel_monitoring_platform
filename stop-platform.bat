@echo off
setlocal DisableDelayedExpansion
title VesselSight - Stop platform

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-field.ps1" %*
set "VESSELSIGHT_EXIT_CODE=%ERRORLEVEL%"
echo.
if "%VESSELSIGHT_EXIT_CODE%"=="0" (
    echo Platform is stopped. You can close this window.
) else (
    echo [ERROR] Platform shutdown failed. See the message above.
    echo Do not shut down the computer until recording has finished.
)

if not defined VESSELSIGHT_NO_PAUSE pause
exit /b %VESSELSIGHT_EXIT_CODE%
