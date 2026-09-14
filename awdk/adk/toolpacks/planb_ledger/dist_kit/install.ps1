# Plan B Ledger — self-contained setup wizard (Windows)
# Run:  right-click > Run with PowerShell   (or:  powershell -ExecutionPolicy Bypass -File install.ps1)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "   PLAN B LEDGER  -  one ledger, two faces" -ForegroundColor Cyan
Write-Host "   local-first - no cloud - yours" -ForegroundColor Cyan
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Python ────────────────────────────────────────────────────────────
$py = $null
foreach ($cand in @("python", "py")) {
    try {
        $v = & $cand --version 2>&1
        if ($v -match "Python 3\.(1[0-9]|[89])") { $py = $cand; break }
    } catch { }
}
if (-not $py) {
    Write-Host "  [X] Python 3.10+ not found." -ForegroundColor Red
    Write-Host "      Install from https://www.python.org/downloads/ (tick 'Add to PATH'),"
    Write-Host "      then run this installer again."
    Read-Host "  Press Enter to exit"
    exit 1
}
Write-Host "  [OK] Python found ($(& $py --version))" -ForegroundColor Green

# ── 2. Virtual env + deps ────────────────────────────────────────────────
$venv = Join-Path $here ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "  [..] Creating virtual environment..."
    & $py -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"
Write-Host "  [..] Installing dependencies (discord.py, httpx)..."
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $here "requirements.txt")
Write-Host "  [OK] Dependencies installed" -ForegroundColor Green

# ── 3. Discord bot token ─────────────────────────────────────────────────
Write-Host ""
Write-Host "  -- Discord bot setup --------------------------------------" -ForegroundColor Yellow
Write-Host "  1. Open https://discord.com/developers/applications -> New Application"
Write-Host "  2. Bot (left menu) -> Reset Token -> copy it"
Write-Host "  3. Same page: enable 'MESSAGE CONTENT INTENT'"
Write-Host "  4. OAuth2 -> URL Generator: scope 'bot', perms 'Send Messages',"
Write-Host "     'Attach Files', 'Read Message History' -> open URL, add to your server"
Write-Host ""
$token = Read-Host "  Paste your Discord bot token (Enter to skip -> CLI-only mode)"

# ── 4. Local brain (bonsai-27b via llama.cpp) ────────────────────────────
Write-Host ""
$endpoint = "http://127.0.0.1:8090"
$brainLive = $false
try {
    $r = Invoke-WebRequest -Uri "$endpoint/v1/models" -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -eq 200) { $brainLive = $true }
} catch { }
if ($brainLive) {
    Write-Host "  [OK] Local brain live: bonsai (llama.cpp) at $endpoint" -ForegroundColor Green
} else {
    Write-Host "  [i] No local model at $endpoint yet." -ForegroundColor Yellow
    Write-Host "      I can download the bonsai brain now (236MB-3.6GB by your RAM,"
    Write-Host "      fully offline after that, runs on CPU) - or skip: the built-in"
    Write-Host "      parser works without it."
    $dl = Read-Host "  Download + start the local brain now? [y/N]"
    if ($dl -eq "y") {
        & $vpy -m planb.bootstrap auto
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [i] Brain bootstrap incomplete - built-in parser will be used." -ForegroundColor Yellow
        }
    } else {
        $custom = Read-Host "  Different llama.cpp endpoint? (Enter to keep $endpoint)"
        if ($custom) { $endpoint = $custom }
    }
}

# ── 5. Write config ──────────────────────────────────────────────────────
$dataDir = Join-Path $env:USERPROFILE ".aither\planb"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$cfg = @{ llm_endpoint = $endpoint }
if ($token) { $cfg.discord_token = $token }
$cfg | ConvertTo-Json | Set-Content -Path (Join-Path $dataDir "config.json") -Encoding UTF8
Write-Host "  [OK] Config written to $dataDir\config.json" -ForegroundColor Green

# ── 6. Seed + launchers ──────────────────────────────────────────────────
$seed = Read-Host "  Load demo data (sample bills + entries)? [Y/n]"
if ($seed -ne "n") { & $vpy -m planb.cli seed }

Set-Content -Path (Join-Path $here "run-bot.cmd") -Encoding ASCII -Value `
    "@echo off`r`ncd /d `"$here`"`r`n`".venv\Scripts\python.exe`" -m planb.bot`r`npause"
Set-Content -Path (Join-Path $here "planb.cmd") -Encoding ASCII -Value `
    "@echo off`r`ncd /d `"$here`"`r`n`".venv\Scripts\python.exe`" -m planb.cli %*"

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Cyan
Write-Host "   DONE." -ForegroundColor Green
if ($token) { Write-Host "   Start the bot:   run-bot.cmd   (then !help in Discord)" }
Write-Host "   CLI demo:        planb.cmd demo"
Write-Host "   Print a sheet:   planb.cmd sheet"
Write-Host "   Your data:       $dataDir  (plain JSON - it's YOURS)"
Write-Host "  ============================================" -ForegroundColor Cyan
if ($token) {
    $go = Read-Host "  Start the Discord bot now? [Y/n]"
    if ($go -ne "n") { & $vpy -m planb.bot }
}
