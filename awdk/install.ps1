#Requires -Version 5.1
<#
.SYNOPSIS
    Aither — set up your AI assistant (Windows)

.DESCRIPTION
    The one-click installer for a non-technical user. It:
      1. Makes sure Python is available (installs it via winget if not)
      2. Installs the Aither ADK (your assistant) from PyPI
      3. Launches the friendly setup wizard, which walks you through the rest

    After this, you have your own AI assistant that can chat, make images with
    Stable Diffusion on your own graphics card, and connect to aitherium.com.

.EXAMPLE
    irm https://aitherium.com/install.ps1 | iex

.EXAMPLE
    .\install.ps1 -SkipWizard

.NOTES
    This replaces the old SDK/Node/Shell trio. The modern package is awdk.
#>

[CmdletBinding()]
param(
    [switch]$SkipWizard,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$ADK_PACKAGE = "awdk"

# ── Logging helpers (plain words, big and friendly) ─────────────────────────

function Write-Good { param([string]$M) Write-Host "  OK:  $M" -ForegroundColor Green }
function Write-Step { param([string]$M) Write-Host ""; Write-Host "  ▸ $M" -ForegroundColor Cyan }
function Write-Warn { param([string]$M) Write-Host "  note: $M" -ForegroundColor Yellow }
function Write-Bad  { param([string]$M) Write-Host "  !!   $M" -ForegroundColor Red }

# ── Find a usable Python 3.10+ ──────────────────────────────────────────────

function Find-Python {
    foreach ($cmd in @("python", "python3")) {
        try {
            $v = & $cmd -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $p = $v.Split(".")
                if ([int]$p[0] -ge 3 -and [int]$p[1] -ge 10) { return @{ Cmd = $cmd } }
            }
        } catch { }
    }
    try {
        $v = & py -3 -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) {
            $p = $v.Split(".")
            if ([int]$p[0] -ge 3 -and [int]$p[1] -ge 10) { return @{ Cmd = "py -3" } }
        }
    } catch { }
    return $null
}

# ── Bootstrap Python when missing ────────────────────────────────────────────

function Install-Python {
    Write-Step "Python is needed. Installing it for you…"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            & winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --silent | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Good "Python installed by winget."
                # winget Python is on PATH in NEW windows; try the known shims too.
                $shim = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
                if (Test-Path $shim) { return @{ Cmd = $shim } }
                return $null
            }
        } catch {
            Write-Warn "winget install did not finish cleanly."
        }
    }
    Write-Bad "Could not install Python automatically."
    Write-Host "  Please install Python 3.10 or newer from https://python.org/downloads/"
    Write-Host "  and make sure 'Add Python to PATH' is checked, then run this again."
    exit 1
}

# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  Aither — set up your AI assistant" -ForegroundColor Cyan
Write-Host "  ==================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This installs your own AI assistant on this computer."
Write-Host "  It can chat with you, make images, and connect to aitherium.com."
Write-Host ""

# ── 1. Python ───────────────────────────────────────────────────────────────

$pyInfo = Find-Python
if (-not $pyInfo) {
    $pyInfo = Install-Python
}
if (-not $pyInfo) {
    $pyInfo = Find-Python
    if (-not $pyInfo) {
        Write-Bad "Python is still not available. Install it from https://python.org/downloads/ and run this again."
        exit 1
    }
}
Write-Good "Python is ready."

# ── 2. Install the ADK ──────────────────────────────────────────────────────

Write-Step "Installing your assistant (this takes a moment)…"
$cmdParts = ($pyInfo.Cmd -split "\s+")
try {
    & $cmdParts[0] ($cmdParts[1..($cmdParts.Length)] + @("-m", "pip", "install", "--upgrade", "--quiet", $ADK_PACKAGE))
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
} catch {
    Write-Bad "The install did not complete. Check your internet connection and try again."
    exit 1
}
Write-Good "Your assistant is installed."

# ── 3. Launch the wizard ────────────────────────────────────────────────────

if (-not $SkipWizard) {
    Write-Step "Opening the setup wizard…"
    Write-Host "  A window will open. It asks a few simple questions and does the rest."
    Write-Host ""

    $guiCmd = if ($cmdParts[0] -eq "py") {
        @("py", "-3", "-m", "adk.cli", "wizard", "--gui")
    } else {
        @($cmdParts[0], "-m", "adk.cli", "wizard", "--gui")
    }
    try {
        & $guiCmd[0] $guiCmd[1..($guiCmd.Length)] 2>$null
        if ($LASTEXITCODE -ne 0) { throw "wizard exit $LASTEXITCODE" }
    } catch {
        Write-Warn "The window did not open here — run:  aither wizard  to start it manually."
    }
}

# ── 4. Done ─────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Green
Write-Host "    You're all set! Your AI assistant is ready." -ForegroundColor Green
Write-Host "  ==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    • aither wizard        — set up again / change options"
Write-Host "    • aither 'hello'       — start chatting"
Write-Host "    • Visit aitherium.com  — your apps, all in one place"
Write-Host ""
