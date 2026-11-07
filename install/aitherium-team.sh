#!/bin/sh
# Set up Claude Code for the Aitherium team (macOS / Linux) -- one command, no secrets.
#
#   curl -LsSf https://github.com/Aitherium/awdk/releases/latest/download/aitherium-team.sh | sh
#
# What it does (idempotent; re-run any time):
#   1. uv + awdk + awsettings as uv tools (uv brings its own Python).
#   2. The AitherOS checkout ($AITHER_REPO, default ~/AitherOS-Fresh); cloned with
#      your own GitHub access when missing.
#   3. adk login        -- browser device flow with YOUR Aitherium account.
#   4. mint_session_bearer.py -- your per-user MCP bearer (~/.aither/session-bearer).
#   5. awsettings --quiet pull -- the team Claude Code settings.
#   6. The awknowledge doctrine skill into ~/.claude/skills/awknowledge.
#   7. The `aitheros` stdio-bridge entry in <repo>/.mcp.json (plus `aitheros-cloud`
#      when no local gateway answers on 127.0.0.1:8182).
#   8. check_mcp_client_config.py --list to prove the config is clean.
#
# No key, token or password is ever written by this script or sent to you by
# email: every credential comes from your own sign-in.
# Skip steps with AITHER_TEAM_SKIP=login,settings,doctrine,mcp
set -eu

PYTHON_PIN="${ADK_PYTHON:-3.12}"
REPO="${AITHER_REPO:-$HOME/AitherOS-Fresh}"
SKIP=",${AITHER_TEAM_SKIP:-},"

say()  { printf '\033[1;36m[aitherium-team]\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m[aitherium-team] ERROR:\033[0m %s\n' "$1" >&2; exit 1; }
skipped() { case "$SKIP" in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

# -- 1. uv, awdk, awsettings ----------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (https://astral.sh/uv)..."
    command -v curl >/dev/null 2>&1 || fail "curl is required to install uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv not on PATH - open a new shell and re-run"
fi
say "Installing awdk + awsettings (Python $PYTHON_PIN)..."
uv tool install --python "$PYTHON_PIN" --upgrade awdk || fail "uv tool install awdk failed"
uv tool install --python "$PYTHON_PIN" --upgrade awsettings || fail "uv tool install awsettings failed"
TOOL_BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
export PATH="$TOOL_BIN:$PATH"
uv python install "$PYTHON_PIN" >/dev/null
PY="$(uv python find "$PYTHON_PIN")"
[ -n "$PY" ] || fail "uv could not find Python $PYTHON_PIN"

# -- 2. the AitherOS checkout ---------------------------------------------------
if [ ! -f "$REPO/AitherOS/dev/tools/mint_session_bearer.py" ]; then
    say "Cloning Aitherium/AitherOS into $REPO (uses your own GitHub access)..."
    if command -v gh >/dev/null 2>&1; then
        gh repo clone Aitherium/AitherOS "$REPO" || fail "clone failed - ask the workspace owner for GitHub access"
    else
        git clone https://github.com/Aitherium/AitherOS "$REPO" || fail "clone failed - ask the workspace owner for GitHub access"
    fi
fi
TOOLS="$REPO/AitherOS/dev/tools"

# -- 3 + 4. adk login (device flow, YOUR account) and the MCP session bearer ----
if ! skipped login; then
    say "Signing in with your Aitherium account (browser device flow)..."
    adk login || fail "adk login failed"
    say "Minting your MCP session bearer..."
    uv run --no-project --python "$PYTHON_PIN" --with awdk --with httpx \
        python "$TOOLS/mint_session_bearer.py" || fail "mint_session_bearer.py failed"
fi

# -- 5. team Claude Code settings ----------------------------------------------
if ! skipped settings; then
    say "Pulling team Claude Code settings..."
    awsettings --quiet pull || say "awsettings pull did not complete - re-run later: awsettings --quiet pull"
fi

# -- 6. awknowledge doctrine skill ---------------------------------------------
if ! skipped doctrine; then
    SRC="$REPO/.claude/skills/awknowledge"
    DST="$HOME/.claude/skills/awknowledge"
    if [ -d "$SRC" ]; then
        mkdir -p "$DST"
        cp -R "$SRC/." "$DST/"
        say "Doctrine skill installed: $DST (run /awknowledge in Claude Code to apply it)"
    else
        say "awknowledge skill not found at $SRC - skipped"
    fi
fi

# -- 7. .mcp.json aitheros stdio bridge ----------------------------------------
if ! skipped mcp; then
    LOCAL=0
    if command -v curl >/dev/null 2>&1 && curl -fsS -m 3 http://127.0.0.1:8182/health >/dev/null 2>&1; then
        LOCAL=1
    fi
    AITHER_TEAM_PY="$PY" AITHER_TEAM_LOCAL_GATEWAY="$LOCAL" "$PY" - "$REPO" <<'PYEOF' || fail ".mcp.json merge failed"
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
PYEOF
fi

# -- 8. prove the MCP config is clean ------------------------------------------
(cd "$REPO" && "$PY" "$TOOLS/check_mcp_client_config.py" --list) \
    || fail "check_mcp_client_config.py reported a problem (see above)"

say "Done. Open Claude Code in $REPO. Your Claude Code seat is added by the workspace owner."
