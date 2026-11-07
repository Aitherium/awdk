<#
.SYNOPSIS
Set up Claude Code for the Aitherium team (Windows) -- one command, no secrets.

.DESCRIPTION
    powershell -ExecutionPolicy ByPass -c "irm https://github.com/Aitherium/awdk/releases/latest/download/aitherium-team.ps1 | iex"

What it does (idempotent; re-run any time):
  1. uv + awdk + awsettings as uv tools (uv brings its own Python).
  2. The AitherOS checkout ($env:AITHER_REPO, default ~\AitherOS-Fresh); cloned
     with your own GitHub access when missing.
  3. adk login        -- browser device flow with YOUR Aitherium account.
  4. mint_session_bearer.py -- your per-user MCP bearer (~\.aither\session-bearer).
  5. awsettings --quiet pull -- the team Claude Code settings.
  6. The awknowledge doctrine skill into ~\.claude\skills\awknowledge.
  7. The `aitheros` stdio-bridge entry in <repo>\.mcp.json (plus `aitheros-cloud`
     when no local gateway answers on 127.0.0.1:8182).
  8. check_mcp_client_config.py --list to prove the config is clean.

No key, token or password is ever written by this script or sent to you by
email: every credential comes from your own sign-in.
Skip steps with $env:AITHER_TEAM_SKIP = 'login,settings,doctrine,mcp'.
#>
$ErrorActionPreference = 'Stop'
$PythonPin = if ($env:ADK_PYTHON) { $env:ADK_PYTHON } else { '3.12' }
$Repo = if ($env:AITHER_REPO) { $env:AITHER_REPO } else { Join-Path $HOME 'AitherOS-Fresh' }
$Skip = if ($env:AITHER_TEAM_SKIP) { $env:AITHER_TEAM_SKIP.Split(',') } else { @() }

function Say([string]$msg)  { Write-Host "[aitherium-team] $msg" -ForegroundColor Cyan }
function Fail([string]$msg) { Write-Host "[aitherium-team] ERROR: $msg" -ForegroundColor Red; exit 1 }
function Skipped([string]$step) { return $Skip -contains $step }

# -- 1. uv, awdk, awsettings ----------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'Installing uv (https://astral.sh/uv)...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv not on PATH - open a new terminal and re-run' }
}
Say "Installing awdk + awsettings (Python $PythonPin)..."
uv tool install --python $PythonPin --upgrade awdk
if ($LASTEXITCODE -ne 0) { Fail 'uv tool install awdk failed' }
uv tool install --python $PythonPin --upgrade awsettings
if ($LASTEXITCODE -ne 0) { Fail 'uv tool install awsettings failed' }
$toolBin = (uv tool dir --bin 2>$null); if (-not $toolBin) { $toolBin = "$env:USERPROFILE\.local\bin" }
$env:Path = "$toolBin;$env:Path"
uv python install $PythonPin | Out-Null
$py = (uv python find $PythonPin).Trim()
if (-not $py) { Fail "uv could not find Python $PythonPin" }

# -- 2. the AitherOS checkout ---------------------------------------------------
if (-not (Test-Path (Join-Path $Repo 'AitherOS\dev\tools\mint_session_bearer.py'))) {
    Say "Cloning Aitherium/AitherOS into $Repo (uses your own GitHub access)..."
    if (Get-Command gh -ErrorAction SilentlyContinue) { gh repo clone Aitherium/AitherOS $Repo }
    else { git clone https://github.com/Aitherium/AitherOS $Repo }
    if ($LASTEXITCODE -ne 0) { Fail "clone failed - ask the workspace owner for GitHub access, then re-run" }
}
$Tools = Join-Path $Repo 'AitherOS\dev\tools'

# -- 3. adk login (device flow, YOUR account) ----------------------------------
if (-not (Skipped 'login')) {
    Say 'Signing in with your Aitherium account (browser device flow)...'
    adk login
    if ($LASTEXITCODE -ne 0) { Fail 'adk login failed' }
}

# -- 4. per-user MCP session bearer --------------------------------------------
if (-not (Skipped 'login')) {
    Say 'Minting your MCP session bearer...'
    uv run --no-project --python $PythonPin --with awdk --with httpx python (Join-Path $Tools 'mint_session_bearer.py')
    if ($LASTEXITCODE -ne 0) { Fail 'mint_session_bearer.py failed' }
}

# -- 5. team Claude Code settings ----------------------------------------------
if (-not (Skipped 'settings')) {
    Say 'Pulling team Claude Code settings...'
    awsettings --quiet pull
    if ($LASTEXITCODE -ne 0) { Say 'awsettings pull did not complete - re-run later: awsettings --quiet pull' }
}

# -- 6. awknowledge doctrine skill ---------------------------------------------
if (-not (Skipped 'doctrine')) {
    $src = Join-Path $Repo '.claude\skills\awknowledge'
    $dst = Join-Path $HOME '.claude\skills\awknowledge'
    if (Test-Path $src) {
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -Recurse -Force (Join-Path $src '*') $dst
        Say "Doctrine skill installed: $dst (run /awknowledge in Claude Code to apply it)"
    } else { Say "awknowledge skill not found at $src - skipped" }
}

# -- 7. .mcp.json aitheros stdio bridge ----------------------------------------
if (-not (Skipped 'mcp')) {
    $local = $false
    try { $local = ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://127.0.0.1:8182/health).StatusCode -eq 200) } catch { $local = $false }
    $env:AITHER_TEAM_PY = $py
    $env:AITHER_TEAM_LOCAL_GATEWAY = if ($local) { '1' } else { '0' }
    $merge = @'
import json, os, sys
repo = sys.argv[1]
path = os.path.join(repo, ".mcp.json")
cfg = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
servers = cfg.setdefault("mcpServers", {})
bridge = "AitherOS/dev/tools/mcp_stdio_bridge.py"
servers["aitheros"] = {"type": "stdio", "command": os.environ["AITHER_TEAM_PY"],
    "args": [bridge, "--name", "aitheros", "--mount", "aitheros=http://127.0.0.1:8182/mcp"]}
if os.environ.get("AITHER_TEAM_LOCAL_GATEWAY") != "1":
    servers["aitheros-cloud"] = {"type": "stdio", "command": os.environ["AITHER_TEAM_PY"],
        "args": [bridge, "--name", "aitheros-cloud", "--upstream", "https://mcp.aitherium.com/mcp"]}
with open(path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print("wrote", path, "servers:", ", ".join(sorted(servers)))
'@
    $merge | & $py - $Repo
    if ($LASTEXITCODE -ne 0) { Fail '.mcp.json merge failed' }
}

# -- 8. prove the MCP config is clean ------------------------------------------
Push-Location $Repo
try {
    & $py (Join-Path $Tools 'check_mcp_client_config.py') --list
    if ($LASTEXITCODE -ne 0) { Fail 'check_mcp_client_config.py reported a problem (see above)' }
} finally { Pop-Location }

Say "Done. Open Claude Code in $Repo. Your Claude Code seat is added by the workspace owner."
