#!/usr/bin/env bash
# Aither — set up your AI assistant (macOS / Linux)
#
# The one-click installer for a non-technical user. It:
#   1. Makes sure Python is available (installs it via Homebrew on macOS if not)
#   2. Installs the Aither ADK (your assistant) from PyPI
#   3. Launches the friendly setup wizard, which walks you through the rest
#
# Usage:
#   curl -fsSL https://aitherium.com/install.sh | sh
#   curl -fsSL https://aitherium.com/install.sh | sh -s -- --skip-wizard
#
# After this, you have your own AI assistant that can chat, make images with
# Stable Diffusion on your own hardware, and connect to aitherium.com.

set -euo pipefail

ADK_PACKAGE="awdk"
SKIP_WIZARD=false

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
good() { echo -e "${GREEN}  OK:  ${NC}$*"; }
step() { echo -e ""; echo -e "${CYAN}  ▸ $*${NC}"; }
warn() { echo -e "${YELLOW}  note: ${NC}$*"; }
bad()  { echo -e "${RED}  !!   ${NC}$*" >&2; }

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-wizard) SKIP_WIZARD=true ;;
        -h|--help)
            echo "Aither installer — set up your AI assistant (macOS/Linux)"
            echo ""
            echo "Usage: curl -fsSL https://aitherium.com/install.sh | sh"
            echo "       curl -fsSL https://aitherium.com/install.sh | sh -s -- --skip-wizard"
            exit 0 ;;
        *) bad "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

echo ""
echo -e "${CYAN}  Aither — set up your AI assistant${NC}"
echo -e "${CYAN}  ==================================${NC}"
echo ""
echo "  This installs your own AI assistant on this computer."
echo "  It can chat with you, make images, and connect to aitherium.com."
echo ""

# ── 1. Python ───────────────────────────────────────────────────────────────

find_python() {
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then
            v=$("$c" -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            major=${v%%.*}; minor=${v#*.}
            if [[ $major -ge 3 && $minor -ge 10 ]]; then echo "$c"; return; fi
        fi
    done
}

PYTHON=$(find_python)
if [[ -z "$PYTHON" ]]; then
    step "Python is needed. Installing it for you…"
    if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
        brew install python@3.12 >/dev/null 2>&1 && PYTHON=$(find_python)
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y python3.12 python3-pip >/dev/null 2>&1 && PYTHON=$(find_python)
    fi
fi
if [[ -z "$PYTHON" ]]; then
    bad "Could not install Python automatically."
    echo "  Please install Python 3.10+ from https://python.org/downloads/ and run this again."
    exit 1
fi
good "Python is ready ($PYTHON)."

# ── 2. Install the ADK ──────────────────────────────────────────────────────

step "Installing your assistant (this takes a moment)…"
if ! $PYTHON -m pip install --upgrade --quiet "$ADK_PACKAGE"; then
    bad "The install did not complete. Check your internet connection and try again."
    exit 1
fi
good "Your assistant is installed."

# ── 3. Launch the wizard ────────────────────────────────────────────────────

if [[ "$SKIP_WIZARD" != "true" ]]; then
    step "Opening the setup wizard…"
    echo "  A window will open. It asks a few simple questions and does the rest."
    echo ""
    if ! $PYTHON -m adk.cli wizard --gui 2>/dev/null; then
        warn "The window did not open here — run:  aither wizard  to start it manually."
    fi
fi

# ── 4. Done ─────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}  =========================================="
echo "    You're all set! Your AI assistant is ready."
echo -e "  ==========================================${NC}"
echo ""
echo "  Next steps:"
echo "    • aither wizard        — set up again / change options"
echo "    • aither 'hello'       — start chatting"
echo "    • Visit aitherium.com  — your apps, all in one place"
echo ""
