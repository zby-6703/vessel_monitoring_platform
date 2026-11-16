@echo off
setlocal DisableDelayedExpansion
rem Keep CMD input ASCII. The Unicode menu is in a UTF-8 BOM PowerShell file.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\platform-menu.ps1" %*
set "VESSELSIGHT_EXIT_CODE=%ERRORLEVEL%"
if "%VESSELSIGHT_EXIT_CODE%"=="0" exit /b 0
echo [ERROR] Platform menu failed. See the message above.
if not defined VESSELSIGHT_NO_PAUSE pause
exit /b %VESSELSIGHT_EXIT_CODE%
