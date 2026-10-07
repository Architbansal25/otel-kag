@echo off
rem ============================================================================
rem  Starts the whole demo. Batch, not PowerShell: WDAC code-integrity
rem  enforcement on this machine blocks .ps1 scripts, and .cmd is not covered.
rem
rem  Uses only tools that ship with Windows: wsl, curl, netstat, taskkill, timeout.
rem
rem  Double-click it, or run:  START-DEMO.cmd
rem  Stop again with:          STOP-DEMO.cmd
rem ============================================================================
setlocal
title OTel + KAG demo

set "ROOT=%~dp0"
set "DISTRO=Ubuntu-22.04"
rem demo.sh as WSL sees it, wherever this folder is checked out.
set "DEMOSH="
for /f "usebackq delims=" %%i in (`wsl -d %DISTRO% -e wslpath -a "%~dp0ops\demo.sh"`) do set "DEMOSH=%%i"
if not defined DEMOSH set "DEMOSH=/mnt/c/Project/flo/flo2026 demo/otel-kag/ops/demo.sh"

echo.
echo === OTel + KAG demo ===
echo.

rem --- 1. LLM key -------------------------------------------------------------
echo [1] Checking the LLM key
if defined ANTHROPIC_API_KEY goto :keyok
if defined LLM_API_KEY goto :keyok
if defined GROQ_API_KEY goto :keyok
if defined OPENAI_API_KEY goto :keyok
echo     [WARN] No LLM key in this shell.
echo            The demo still runs, but the REASON stage will say
echo            'no LLM configured' instead of producing an answer.
echo.
echo            Fix:  setx GROQ_API_KEY "gsk_..."   or   setx ANTHROPIC_API_KEY "sk-ant-..."
echo            then close this window and open a NEW one.
goto :keydone
:keyok
echo     [ok]   an LLM key is set
:keydone

rem --- 2. services inside WSL -------------------------------------------------
echo.
echo [2] Starting Jaeger + services inside WSL (takes ~40s)
wsl -d %DISTRO% -- bash "%DEMOSH%" start
if errorlevel 1 (
  echo     [FAIL] The WSL stack did not start.
  echo            Check the logs:  wsl -d %DISTRO% -- bash "%DEMOSH%" logs jaeger
  goto :end
)

rem --- 3. console on Windows --------------------------------------------------
echo.
echo [3] Starting the console on Windows
call :killconsole
cd /d "%ROOT%kag"
start "RCA console" /MIN py ui.py
cd /d "%ROOT%"

set /a TRIES=0
:waitloop
rem ping, not timeout: `timeout` aborts with "Input redirection is not
rem supported" whenever stdin is not a real console, which would make this
rem loop spin instantly and report a failure for a console that is fine.
ping -n 2 127.0.0.1 >nul
rem /api/ping is the cheap liveness route; /api/status fans out to seven
rem services and is too slow to poll.
curl -s -o nul -m 3 http://localhost:8090/api/ping
if not errorlevel 1 goto :consoleup
set /a TRIES+=1
if %TRIES% lss 45 goto :waitloop
echo     [FAIL] Console did not start.
echo            Run it by hand to see the error:
echo              cd /d "%ROOT%kag"
echo              py ui.py
goto :end
:consoleup
echo     [ok]   console up

rem --- 4. browser -------------------------------------------------------------
start "" http://localhost:8090

echo.
echo Ready.
echo   Console     http://localhost:8090   ^<- drive the whole demo here
echo   Swagger     http://localhost:8081/swagger-ui.html
echo   Health      http://localhost:8081/actuator/health
echo   Jaeger UI   http://localhost:16686
echo.
echo Stop everything with STOP-DEMO.cmd
echo.
goto :end

rem --- helper: kill whatever is listening on 8090 -----------------------------
:killconsole
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8090" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1
exit /b 0

:end
endlocal
pause
