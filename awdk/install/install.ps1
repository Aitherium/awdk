<#
.SYNOPSIS
AitherADK bootstrap installer (Windows) — no Python required.

.DESCRIPTION
    powershell -ExecutionPolicy ByPass -c "irm https://github.com/Aitherium/awdk/releases/latest/download/install.ps1 | iex"

What it does:
  1. Installs uv (Astral) if missing — uv brings its own Python, so a system
     Python is NOT required.
  2. Installs awdk as a uv tool in an isolated environment (keeps
     `adk pack install` / provider extras / addons working — that's why this
     is a real Python env and not a frozen exe).
  3. Launches the first-run wizard (skip with $env:ADK_NO_WIZARD = '1').

Idempotent: re-running upgrades adk to the latest release.
#>
$ErrorActionPreference = 'Stop'
$PythonPin = if ($env:ADK_PYTHON) { $env:ADK_PYTHON } else { '3.12' }

function Say([string]$msg)  { Write-Host "[adk-install] $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host "[adk-install] ERROR: $msg" -ForegroundColor Red; exit 1 }

# -- 1. uv --------------------------------------------------------------------
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    Say "uv found: $(uv --version)"
} else {
    Say 'Installing uv (https://astral.sh/uv)...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # uv installs to %USERPROFILE%\.local\bin — pick it up for this session
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail 'uv installed but not on PATH - open a new terminal and re-run'
    }
}

# -- 2. awdk -------------------------------------------------------------
Say "Installing awdk (Python $PythonPin, isolated tool env)..."
uv tool install --python $PythonPin --upgrade awdk
if ($LASTEXITCODE -ne 0) { Fail 'uv tool install awdk failed' }

$toolBin = (uv tool dir --bin 2>$null); if (-not $toolBin) { $toolBin = "$env:USERPROFILE\.local\bin" }
$env:Path = "$toolBin;$env:Path"
if (-not (Get-Command adk -ErrorAction SilentlyContinue)) {
    Fail "adk installed to $toolBin but not on PATH - add it and re-run"
}

# Persist the tool bin on the USER Path so new terminals get `adk` too
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$toolBin*") {
    [Environment]::SetEnvironmentVariable('Path', "$toolBin;$userPath", 'User')
    Say "Added $toolBin to your user PATH (new terminals will have 'adk')."
}

$installed = (uv tool list 2>$null | Select-String '^awdk').Line
Say "Installed: $(if ($installed) { $installed } else { 'awdk' })"

# -- 3. first-run wizard --------------------------------------------------------
if ($env:ADK_NO_WIZARD -eq '1') {
    Say 'Done. Next: adk wizard ; adk up'
} else {
    Say 'Launching first-run wizard ($env:ADK_NO_WIZARD = 1 to skip)...'
    adk wizard
    Say 'Done. Start your agent OS with: adk up'
}
