#!/usr/bin/env bash
# Aither on a Steam Deck — remove everything deck-install.sh added.
#
#   ~/.local/share/aither-deck/deck-uninstall.sh            remove + leave the workspace
#   ~/.local/share/aither-deck/deck-uninstall.sh --keep-enrollment
#                                                           remove locally, keep the device
#                                                           listed in your workspace
#
# It removes the device from your workspace (`adk devices rm`), stops and deletes
# the aither-deck-* user units, the launcher, awsh + Node.js, awdk, and uv and
# ~/.aither ONLY when the installer was the one that created them.

set -uo pipefail

DECK_HOME="${HOME:?HOME is not set}"
DATA_DIR="${XDG_DATA_HOME:-$DECK_HOME/.local/share}/aither-deck"
CONF_DIR="${XDG_CONFIG_HOME:-$DECK_HOME/.config}/aither-deck"
UNIT_DIR="${XDG_CONFIG_HOME:-$DECK_HOME/.config}/systemd/user"
APP_DIR="${XDG_DATA_HOME:-$DECK_HOME/.local/share}/applications"
STATE_DIR="${XDG_STATE_HOME:-$DECK_HOME/.local/state}/aither-deck"
BIN_DIR="$DECK_HOME/.local/bin"
STATE_FILE="$DATA_DIR/install-state"
KEEP_ENROLLMENT=0
export PATH="$BIN_DIR:$PATH"

case "${1:-}" in
    --keep-enrollment) KEEP_ENROLLMENT=1 ;;
    "") ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

had_uv=1
had_aither_dir=1
if [ -f "$STATE_FILE" ]; then
    had_uv="$(sed -n 's/^had_uv=//p' "$STATE_FILE")"
    had_aither_dir="$(sed -n 's/^had_aither_dir=//p' "$STATE_FILE")"
fi

echo "  Removing Aither from this Deck"
UNITS="aither-deck-guard.service aither-deck-holder.service aither-deck-rpc.service aither-deck-shell.service aither-deck-node.service"

# Units first: the guard must not restart what we are stopping.
for u in $UNITS; do
    systemctl --user disable --now "$u" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$u"
done
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user reset-failed 2>/dev/null || true
echo "  ok  services stopped and removed"

# Leave the workspace while adk and the sign-in still exist.
if [ "$KEEP_ENROLLMENT" = 0 ] && command -v adk >/dev/null && [ -f "$DECK_HOME/.aither/node_auth.json" ]; then
    node_id="$(sed -n 's/.*"node_id": *"\([^"]*\)".*/\1/p' "$DECK_HOME/.aither/node_auth.json" | head -1)"
    if [ -n "$node_id" ]; then
        if adk devices rm "$node_id"; then
            echo "  ok  $node_id removed from your workspace"
        else
            echo "  !   could not remove $node_id from your workspace; remove it in Settings > Devices" >&2
        fi
    fi
fi

rm -f "$APP_DIR/aither-deck-uninstall.desktop" "$BIN_DIR/awsh"
if command -v uv >/dev/null; then
    uv tool uninstall awdk >/dev/null 2>&1 || true
fi
echo "  ok  awdk and awsh removed"

[ "$had_aither_dir" = 0 ] && rm -rf "$DECK_HOME/.aither"
if [ "$had_uv" = 0 ]; then
    rm -f "$BIN_DIR/uv" "$BIN_DIR/uvx"
    rm -rf "${XDG_DATA_HOME:-$DECK_HOME/.local/share}/uv" "${XDG_CACHE_HOME:-$DECK_HOME/.cache}/uv"
fi
if [ -f "$DECK_HOME/.bashrc" ] && grep -q '# aither-deck PATH' "$DECK_HOME/.bashrc"; then
    sed -i '/# aither-deck PATH$/d' "$DECK_HOME/.bashrc"
fi
rm -rf "$CONF_DIR" "$STATE_DIR"
# Last: this script lives in DATA_DIR; the open file descriptor keeps it readable.
rm -rf "$DATA_DIR"
echo "  ok  done. Nothing of Aither is left on this Deck."
