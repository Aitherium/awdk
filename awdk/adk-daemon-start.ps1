#Requires -Version 7.0
<#
.SYNOPSIS
    Start the AitherADK Agent Daemon — Local Sovereign Runtime

.DESCRIPTION
    Standalone agent with local-first inference routing:
    - Primary:   MicroScheduler at https://127.0.0.1:8150 (internal CA)
    - Fallback:  Cloud APIs (when AITHER_API_KEY is set)
    - Tools:     MCP local gateway 127.0.0.1:8182 + cloud fallback

.PARAMETER Port
    Port to bind daemon (default 9001)

.EXAMPLE
    .\adk-daemon-start.ps1
    .\adk-daemon-start.ps1 -Port 9001

.NOTES
    Endpoints:
      Chat:   POST http://127.0.0.1:9001/chat
      OpenAI: POST http://127.0.0.1:9001/v1/chat/completions
      Health: GET  http://127.0.0.1:9001/health
      Docs:   GET  http://127.0.0.1:9001/docs
#>

param(
    [int]$Port = 9001
)

# Set environment variables for local-first inference
$env:AITHER_CORE_LLM_URL = "https://127.0.0.1:8150"
$env:AITHER_LLM_BACKEND = "auto"
$env:AITHER_PREFER_LOCAL = "true"
$env:AITHER_MCP_GATEWAY = "127.0.0.1:8182"
$env:AITHER_PORT = $Port
$env:AITHER_HOST = "127.0.0.1"
$env:AITHER_JSON_LOGGING = "false"

# Optional: Cloud fallback (set before running if you want cloud tools)
# $env:AITHER_API_KEY = "..."

Write-Host "Starting AitherADK daemon on 127.0.0.1:$Port" -ForegroundColor Green
Write-Host "  LLM:      MicroScheduler (https://127.0.0.1:8150)" -ForegroundColor Cyan
Write-Host "  Fallback: Cloud APIs (set AITHER_API_KEY to enable)" -ForegroundColor DarkCyan
Write-Host "  MCP:      Local gateway (127.0.0.1:8182, cloud fallback)" -ForegroundColor Cyan
Write-Host "  Chat:     POST http://127.0.0.1:$Port/chat" -ForegroundColor Yellow
Write-Host "  Health:   GET  http://127.0.0.1:$Port/health" -ForegroundColor Yellow
Write-Host "  OpenAI:   POST http://127.0.0.1:$Port/v1/chat/completions" -ForegroundColor Yellow
Write-Host ""

# Start daemon in background job
$job = Start-Job -ScriptBlock {
    $port = $args[0]
    $env:AITHER_CORE_LLM_URL = "https://127.0.0.1:8150"
    $env:AITHER_LLM_BACKEND = "auto"
    $env:AITHER_PREFER_LOCAL = "true"
    $env:AITHER_MCP_GATEWAY = "127.0.0.1:8182"
    $env:AITHER_PORT = $port
    $env:AITHER_HOST = "127.0.0.1"
    $env:AITHER_JSON_LOGGING = "false"

    python -m adk.server --port $port --backend auto --identity adk-daemon
} -ArgumentList $Port

Write-Host "Daemon started in background job (ID: $($job.Id))"
Write-Host "To stop: Stop-Job -Id $($job.Id)"
Write-Host "To view logs: Get-Job -Id $($job.Id) | Receive-Job -Wait"
Write-Host ""

# Wait a moment for startup
Start-Sleep -Seconds 2

# Try to verify it's running
$healthUrl = "http://127.0.0.1:$Port/health"
try {
    $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5 -SkipCertificateCheck
    Write-Host "✓ Daemon health check passed" -ForegroundColor Green
    Write-Host "  Response: $($response | ConvertTo-Json -Depth 2)" -ForegroundColor DarkGray
} catch {
    Write-Host "⚠ Health check not yet available (daemon may still be starting)" -ForegroundColor Yellow
    Write-Host "  Try again in a few seconds: curl http://127.0.0.1:$Port/health" -ForegroundColor DarkGray
}
