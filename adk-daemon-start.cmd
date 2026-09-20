@echo off
REM AitherADK Agent Daemon — Local Sovereign Runtime
REM Standalone agent with local-first inference (MicroScheduler :8150)
REM MCP tools fallback to cloud when :8182 is down
REM
REM Usage:
REM   adk-daemon-start.cmd [--port 9001]
REM
REM Endpoints:
REM   Chat:   POST http://127.0.0.1:9001/chat
REM   OpenAI: POST http://127.0.0.1:9001/v1/chat/completions
REM   Health: GET  http://127.0.0.1:9001/health
REM   Docs:   GET  http://127.0.0.1:9001/docs

setlocal enabledelayedexpansion

REM Determine port (default 9001)
set "PORT=9001"
if not "%~1"=="" (
    if "%~1"=="--port" (
        set "PORT=%~2"
    )
)

REM Configuration: Local-first inference
REM
REM Backend is set EXPLICITLY to "vllm", NOT "auto". This matters: config.py:365 only
REM consults saved config when llm_backend == "auto", so an explicit AITHER_LLM_BACKEND=auto
REM is indistinguishable from "unset" and gets OVERRIDDEN by ~/.aither/config.json
REM (default_backend was "openai" on this box) — the daemon then talked to the CLOUD while
REM claiming to be local-first. Naming a concrete provider is what actually pins it local.
REM
REM MicroScheduler is the canonical fleet LLM router (CLAUDE.md: never bypass it) and speaks
REM OpenAI-compatible /v1 over HTTPS with the internal AitherNet CA — so SSL_CERT_FILE points
REM at that CA bundle rather than disabling verification.
set "AITHER_LLM_BACKEND=vllm"
set "AITHER_LLM_BASE_URL=https://127.0.0.1:8150/v1"
set "AITHER_MODEL=aither-orchestrator"
set "SSL_CERT_FILE=C:\AitherOS-Fresh\AitherOS\config\certs\aithernet-ca-bundle.pem"
set "REQUESTS_CA_BUNDLE=C:\AitherOS-Fresh\AitherOS\config\certs\aithernet-ca-bundle.pem"
REM Sovereign mode (adk/server.py:377): skip ALL cloud registration at startup — gateway
REM MCP, secrets sync, AitherNet mesh join, IdP enrolment, relays. Without this the
REM "local sovereign" daemon phones gateway.aitherium.com / idp.aitherium.com BEFORE it
REM binds its port, so it cannot start at all when off-box or when the tunnel is slow —
REM measured: startup never reached "Application startup complete" in 120s. Local routes,
REM the local MCP server and A2A all still mount.
set "AITHER_OFFLINE=1"

REM Gateway credential for the LOCAL MCP gateway. Without a bearer the gateway answers
REM {"error":"Missing bearer token"} and the daemon connects but lists ZERO tools — which
REM looks like "connected, parity achieved" in the logs while actually being tool-less.
REM The key lives in %USERPROFILE%\.aither\daemon.env (0600, outside the repo) so it is
REM never committed; create that file with a single AITHER_API_KEY=... line.
if exist "%USERPROFILE%\.aither\daemon.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%USERPROFILE%\.aither\daemon.env") do (
        if /i "%%A"=="AITHER_API_KEY" set "AITHER_API_KEY=%%B"
    )
)
set "AITHER_CORE_LLM_URL=https://127.0.0.1:8150"
set "AITHER_PREFER_LOCAL=true"
set "AITHER_MCP_GATEWAY=127.0.0.1:8182"
set "AITHER_PORT=%PORT%"
set "AITHER_HOST=127.0.0.1"

REM Optional: Cloud fallback API key (if not set, daemon still runs without cloud tools)
REM set "AITHER_API_KEY=..."

REM Logging
set "AITHER_JSON_LOGGING=false"

REM Start the daemon in a new window (detached)
echo Starting AitherADK daemon on 127.0.0.1:%PORT%
echo   LLM:      MicroScheduler (https://127.0.0.1:8150)
echo   Fallback: Cloud APIs (set AITHER_API_KEY to enable)
echo   MCP:      Local gateway (127.0.0.1:8182, cloud fallback)
echo   Chat:     POST http://127.0.0.1:%PORT%/chat
echo   Health:   GET  http://127.0.0.1:%PORT%/health
echo.

REM ONE SPEC (2026-09-19). When this host carries the scheduled-task payload that the
REM :9001 watchdog itself launches (~\.aither\bin\hidden-tasks\AitherOS-AdkDaemon.cmd:
REM AITHER_OFFLINE=1, log redirect, canonical tree), delegate to it instead of starting a
REM SECOND daemon with a DIFFERENT env. Measured that day: awsh launched this file
REM (--backend vllm, visible console) while the watchdog launched the hidden one every
REM tick -- three launchers, two configs, one port. The env above still applies on a
REM box without the hidden task (a stranger's machine), which is the fallback below.
if "%PORT%"=="9001" if exist "%USERPROFILE%\.aither\bin\hidden-tasks\AitherOS-AdkDaemon.cmd" (
    echo   delegating to the hidden scheduled-task payload ^(one launcher, one spec^)
    if exist "%USERPROFILE%\.aither\bin\run-hidden.vbs" (
        start "" wscript.exe //B //Nologo "%USERPROFILE%\.aither\bin\run-hidden.vbs" "%USERPROFILE%\.aither\bin\hidden-tasks\AitherOS-AdkDaemon.cmd"
    ) else (
        start "AitherADK Daemon :%PORT%" "%USERPROFILE%\.aither\bin\hidden-tasks\AitherOS-AdkDaemon.cmd"
    )
    endlocal
    exit /b 0
)

REM Run daemon in a new detached window
start "AitherADK Daemon :%PORT%" python -m adk.server --port %PORT% --backend vllm --identity adk-daemon

REM Wait briefly to let the daemon start before returning
timeout /t 2 /nobreak

endlocal
