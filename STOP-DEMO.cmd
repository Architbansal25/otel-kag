@echo off
rem ============================================================================
rem  Stops the console on Windows and the whole stack inside WSL.
rem  Batch rather than PowerShell: WDAC blocks .ps1 on this machine.
rem ============================================================================
setlocal

set "DISTRO=Ubuntu-22.04"
rem demo.sh as WSL sees it, wherever this folder is checked out.
set "DEMOSH="
for /f "usebackq delims=" %%i in (`wsl -d %DISTRO% -e wslpath -a "%~dp0ops\demo.sh"`) do set "DEMOSH=%%i"
if not defined DEMOSH set "DEMOSH=/mnt/c/Project/flo/flo2026 demo/otel-kag/ops/demo.sh"

echo.
echo Stopping the console...
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8090" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%p >nul 2>&1
  echo   [stopped] console ^(pid %%p^)
  set FOUND=1
)
if "%FOUND%"=="0" echo   console was not running

echo Stopping the WSL stack...
wsl -d %DISTRO% -- bash "%DEMOSH%" stop

echo.
echo Done.
echo.
endlocal
pause
