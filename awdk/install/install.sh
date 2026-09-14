#!/bin/sh
# AitherADK bootstrap installer (macOS / Linux) — no Python required.
#
#   curl -LsSf https://github.com/Aitherium/awdk/releases/latest/download/install.sh | sh
#
# What it does:
#   1. Installs uv (Astral) if missing — uv brings its own Python, so a system
#      Python is NOT required.
#   2. Installs awdk as a uv tool in an isolated environment (keeps
#      `adk pack install` / provider extras / addons working — that's why this
#      is a real Python env and not a frozen binary).
#   3. Launches the first-run wizard (skip with ADK_NO_WIZARD=1).
#
# Idempotent: re-running upgrades adk to the latest release.
set -eu

PYTHON_PIN="${ADK_PYTHON:-3.12}"

say()  { printf '\033[1;36m[adk-install]\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m[adk-install] ERROR:\033[0m %s\n' "$1" >&2; exit 1; }

# ── 1. uv ────────────────────────────────────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    say "uv found: $(uv --version)"
else
    say "Installing uv (https://astral.sh/uv)…"
    command -v curl >/dev/null 2>&1 || fail "curl is required to install uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin (or XDG override) — pick it up for this session
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv installed but not on PATH — open a new shell and re-run"
fi

# ── 2. awdk ────────────────────────────────────────────────────────────
say "Installing awdk (Python $PYTHON_PIN, isolated tool env)…"
uv tool install --python "$PYTHON_PIN" --upgrade awdk

TOOL_BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
export PATH="$TOOL_BIN:$PATH"
command -v adk >/dev/null 2>&1 || fail "adk installed to $TOOL_BIN but not on PATH — add it to your shell profile"

say "Installed: $(uv tool list 2>/dev/null | grep '^awdk' || echo awdk)"
case ":$(sh -lc 'echo $PATH')": in
    *":$TOOL_BIN:"*) ;;
    *) say "NOTE: add $TOOL_BIN to your PATH (e.g. 'uv tool update-shell') so 'adk' works in new shells." ;;
esac

# ── 3. first-run wizard ──────────────────────────────────────────────────────
if [ "${ADK_NO_WIZARD:-0}" = "1" ]; then
    say "Done. Next: adk wizard && adk up"
else
    say "Launching first-run wizard (ADK_NO_WIZARD=1 to skip)…"
    adk wizard
    say "Done. Start your agent OS with: adk up"
fi
