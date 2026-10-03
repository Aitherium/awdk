#!/usr/bin/env bash
# Aither on a Steam Deck — join this Deck to your fleet without touching SteamOS.
#
# Run once in Desktop Mode (Konsole):
#   curl -fsSL https://raw.githubusercontent.com/Aitherium/awdk/main/install/deck/deck-install.sh | bash
#
# Everything lands in your home directory. The read-only root stays read-only,
# no sudo, no pacman, no developer mode. What it does:
#   1. uv (user-level Python manager)          -> ~/.local/bin/uv
#   2. awdk (the `adk` CLI)                    -> uv tool, ~/.local/bin/adk
#   3. awsh (terminal assistant), Node.js      -> ~/.local/share/aither-deck/node (sha256-checked)
#   4. Sign in (device code: type it on your phone) and enrol this Deck in your workspace
#   5. systemd --user units:
#        aither-deck-node    enrol + heartbeat + reverse link   (`adk rc`)
#        aither-deck-shell   the session daemon on 127.0.0.1    (`adk harness serve`)
#        aither-deck-guard   backs everything off while a game runs; lends memory
#                            (`adk kvholder serve`) only on AC power + docked + idle
#   6. Desktop launcher "Leave Aither fleet" that runs deck-uninstall.sh
#
# Remove it all: ~/.local/share/aither-deck/deck-uninstall.sh
#
# Options:
#   --no-awsh            skip Node.js + awsh
#   --no-enroll          install only; enrol later with: adk rc --once
#   --lend-memory        let the guard lend memory as a KV holder (off by default)
#   --holder-max-mb N    memory to lend (default 6144 of the Deck's 16 GB)
#   --no-dock-required   lend on AC power even when not docked
#   --api-key-file F     sign in non-interactively with a key read from file F
#   --awdk-spec S        pip spec for awdk (default: awdk)

set -euo pipefail

DECK_HOME="${HOME:?HOME is not set}"
DATA_DIR="${XDG_DATA_HOME:-$DECK_HOME/.local/share}/aither-deck"
CONF_DIR="${XDG_CONFIG_HOME:-$DECK_HOME/.config}/aither-deck"
UNIT_DIR="${XDG_CONFIG_HOME:-$DECK_HOME/.config}/systemd/user"
APP_DIR="${XDG_DATA_HOME:-$DECK_HOME/.local/share}/applications"
BIN_DIR="$DECK_HOME/.local/bin"
STATE_FILE="$DATA_DIR/install-state"
RAW_BASE="${AITHER_DECK_RAW_BASE:-https://raw.githubusercontent.com/Aitherium/awdk/main/install/deck}"
NODE_MAJOR="${AITHER_DECK_NODE_MAJOR:-22}"

WITH_AWSH=1
ENROLL=1
LEND_MEMORY=0
HOLDER_MAX_MB=6144
REQUIRE_DOCK=1
API_KEY_FILE=""
AWDK_SPEC="${AITHER_DECK_AWDK_SPEC:-awdk}"

say()  { printf '  > %s\n' "$*"; }
ok()   { printf '  ok  %s\n' "$*"; }
warn() { printf '  !   %s\n' "$*" >&2; }
die()  { printf '  x   %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --no-awsh) WITH_AWSH=0 ;;
        --no-enroll) ENROLL=0 ;;
        --lend-memory) LEND_MEMORY=1 ;;
        --holder-max-mb) shift; HOLDER_MAX_MB="${1:?--holder-max-mb needs a number}" ;;
        --no-dock-required) REQUIRE_DOCK=0 ;;
        --api-key-file) shift; API_KEY_FILE="${1:?--api-key-file needs a path}" ;;
        --awdk-spec) shift; AWDK_SPEC="${1:?--awdk-spec needs a value}" ;;
        -h|--help) sed -n '2,32p' "$0" 2>/dev/null || true; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done

case "$HOLDER_MAX_MB" in ''|*[!0-9]*) die "--holder-max-mb must be a whole number" ;; esac
[ "$(id -u)" -ne 0 ] || die "run this as your normal user (deck), not root: nothing here needs sudo"
command -v curl >/dev/null || die "curl is missing"
command -v systemctl >/dev/null || die "systemctl is missing (this is meant for SteamOS / a systemd Linux)"

# shellcheck source=/dev/null
OS_ID="$(. /etc/os-release 2>/dev/null && printf '%s' "${ID:-unknown}")"
echo
echo "  Aither on this Deck"
echo "  ==================="
if [ "$OS_ID" != "steamos" ]; then
    warn "this is $OS_ID, not SteamOS; continuing (everything is user-level anyway)"
fi

mkdir -p "$DATA_DIR" "$CONF_DIR" "$UNIT_DIR" "$APP_DIR" "$BIN_DIR"
chmod 700 "$CONF_DIR"
export PATH="$BIN_DIR:$PATH"

# Record what existed BEFORE us, so the uninstaller removes only what we added.
if [ ! -f "$STATE_FILE" ]; then
    {
        echo "# written by deck-install.sh; read by deck-uninstall.sh"
        if command -v uv >/dev/null; then echo "had_uv=1"; else echo "had_uv=0"; fi
        if [ -d "$DECK_HOME/.aither" ]; then echo "had_aither_dir=1"; else echo "had_aither_dir=0"; fi
    } > "$STATE_FILE"
fi

# The uninstaller lives next to the install so removal never needs the network.
fetch_sibling() {  # name -> $DATA_DIR/name, from the local checkout when run from one
    local name="$1" here=""
    here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
    if [ -n "$here" ] && [ -f "$here/$name" ]; then
        cp "$here/$name" "$DATA_DIR/$name"
    else
        curl -fsSL "$RAW_BASE/$name" -o "$DATA_DIR/$name"
    fi
    chmod 755 "$DATA_DIR/$name"
}
say "fetching the guard and the uninstaller"
fetch_sibling deck-guard.sh
fetch_sibling deck-uninstall.sh
ok "$DATA_DIR"

# 1. uv
if ! command -v uv >/dev/null; then
    say "installing uv (user-level)"
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$BIN_DIR" UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
command -v uv >/dev/null || die "uv did not install"
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

# 2. awdk. uv brings its own Python, so SteamOS's system Python is never touched.
say "installing awdk ($AWDK_SPEC)"
uv tool install --quiet --force --python 3.12 --with numpy "$AWDK_SPEC"
command -v adk >/dev/null || die "adk is not on PATH after install"
ok "$(adk --version 2>/dev/null | head -1 || echo adk)"

# 3. awsh on a private Node.js (SteamOS ships none, and /usr is read-only).
if [ "$WITH_AWSH" = 1 ]; then
    NODE_DIR="$DATA_DIR/node"
    if [ ! -x "$NODE_DIR/bin/node" ]; then
        say "installing Node.js $NODE_MAJOR (sha256-checked)"
        base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"
        sums="$(curl -fsSL "$base/SHASUMS256.txt")"
        line="$(printf '%s\n' "$sums" | grep -E ' node-v[0-9.]+-linux-x64\.tar\.xz$' | head -1)"
        [ -n "$line" ] || die "could not find a linux-x64 Node.js build in $base"
        want="${line%% *}"
        file="${line##* }"
        tmp="$(mktemp -d)"
        curl -fsSL "$base/$file" -o "$tmp/$file"
        got="$(sha256sum "$tmp/$file" | awk '{print $1}')"
        [ "$got" = "$want" ] || { rm -rf "$tmp"; die "Node.js checksum mismatch for $file"; }
        mkdir -p "$NODE_DIR"
        tar -xJf "$tmp/$file" -C "$NODE_DIR" --strip-components=1
        rm -rf "$tmp"
    fi
    say "installing awsh"
    PATH="$NODE_DIR/bin:$PATH" npm_config_cache="$DATA_DIR/npm-cache" npm install --silent --global --prefix "$DATA_DIR/npm" @aitherium/awsh >/dev/null
    rm -f "$BIN_DIR/awsh"
    # npm's shebang is `env node`; a wrapper pins our Node without editing PATH files.
    cat > "$BIN_DIR/awsh" <<EOF
#!/usr/bin/env bash
export PATH="$NODE_DIR/bin:\$PATH"
exec "$DATA_DIR/npm/bin/awsh" "\$@"
EOF
    chmod 755 "$BIN_DIR/awsh"
    ok "awsh $("$BIN_DIR/awsh" --version 2>/dev/null | head -1 || echo installed)"
fi

# Mesh discovery for the KV holder arrived after 3.8.49; use it when this adk has it.
HOLDER_MESH=0
if adk kvholder serve --help 2>/dev/null | grep -q -- '--mesh'; then HOLDER_MESH=1; fi

# 4. Settings the guard reads. No secrets here; the sign-in lives in ~/.aither.
cat > "$CONF_DIR/deck.env" <<EOF
# Aither on this Deck. Edit, then: systemctl --user restart aither-deck-guard
DECK_LEND_MEMORY=$LEND_MEMORY
DECK_HOLDER_MAX_MB=$HOLDER_MAX_MB
DECK_REQUIRE_DOCK=$REQUIRE_DOCK
# How lent memory reaches the fleet: a relay URL (ws(s)://HOST:PORT/holder), or,
# when empty and DECK_HOLDER_MESH=1, mesh discovery (tailnet peers / LAN). A mesh
# join prints a 6-letter code in: journalctl --user -u aither-deck-holder
# approve it on your desktop with: adk kvholder mesh approve CODE
DECK_HOLDER_CONNECT=
DECK_HOLDER_MESH=$HOLDER_MESH
EOF
chmod 600 "$CONF_DIR/deck.env"

# 5. systemd --user units. Background work runs at idle priority, always.
ADK_BIN="$(command -v adk)"
# "deck" once the installed adk knows the class; older releases only take laptop.
NODE_CLASS=laptop
if adk rc --help 2>/dev/null | grep -Eq '[{,]deck[,}]'; then NODE_CLASS=deck; fi
write_unit() { cat > "$UNIT_DIR/$1"; }

write_unit aither-deck-node.service <<EOF
[Unit]
Description=Aither: this Deck in your fleet (enrol, heartbeat, link)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=$BIN_DIR:/usr/bin:/bin
ExecStart=$ADK_BIN rc --node-class $NODE_CLASS
Restart=on-failure
RestartSec=30
Nice=19
CPUWeight=idle
IOSchedulingClass=idle
MemoryHigh=512M

[Install]
WantedBy=default.target
EOF

write_unit aither-deck-shell.service <<EOF
[Unit]
Description=Aither: session daemon on 127.0.0.1 (reachable only through the link)

[Service]
Type=simple
Environment=PATH=$BIN_DIR:/usr/bin:/bin
ExecStart=$ADK_BIN harness serve --host 127.0.0.1
Restart=on-failure
RestartSec=30
Nice=19
CPUWeight=idle
IOSchedulingClass=idle
MemoryHigh=1G

[Install]
WantedBy=default.target
EOF

write_unit aither-deck-holder.service <<EOF
[Unit]
Description=Aither: lend memory as a KV holder (started and stopped by aither-deck-guard)

[Service]
Type=simple
EnvironmentFile=$CONF_DIR/deck.env
Environment=PATH=$BIN_DIR:/usr/bin:/bin
ExecStart=/usr/bin/env bash -c 'if [ -n "\$DECK_HOLDER_CONNECT" ]; then exec $ADK_BIN kvholder serve --connect "\$DECK_HOLDER_CONNECT" --max-mb "\$DECK_HOLDER_MAX_MB" --store tq4; else exec $ADK_BIN kvholder serve --mesh --max-mb "\$DECK_HOLDER_MAX_MB" --store tq4; fi'
Restart=on-failure
RestartSec=60
Nice=19
CPUWeight=idle
IOSchedulingClass=idle
EOF

write_unit aither-deck-guard.service <<EOF
[Unit]
Description=Aither: back off while a game runs; lend memory only on AC + dock + idle

[Service]
Type=simple
EnvironmentFile=$CONF_DIR/deck.env
ExecStart=$DATA_DIR/deck-guard.sh --serve
Restart=always
RestartSec=10
Nice=19
CPUWeight=idle

[Install]
WantedBy=default.target
EOF

# 6. The removal launcher.
cat > "$APP_DIR/aither-deck-uninstall.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Leave Aither fleet
Comment=Remove Aither from this Deck (deck-uninstall.sh)
Exec=konsole --hold -e $DATA_DIR/deck-uninstall.sh
Icon=edit-delete
Terminal=false
Categories=System;
EOF

systemctl --user daemon-reload
# Keep the units running in Game Mode too. Allowed for your own user on SteamOS;
# harmless when refused (Game Mode keeps the deck session logged in anyway).
loginctl enable-linger "$(id -un)" 2>/dev/null || true

# 7. Sign in + enrol, then hand the link to the unit.
if [ "$ENROLL" = 1 ]; then
    if [ -n "$API_KEY_FILE" ]; then
        [ -r "$API_KEY_FILE" ] || die "cannot read $API_KEY_FILE"
        adk login --api-key "$(tr -d '[:space:]' < "$API_KEY_FILE")" --no-sync
    elif [ ! -f "$DECK_HOME/.aither/auth.json" ]; then
        say "sign in: a code appears below. Open the link on your phone and type it."
        adk login --no-sync </dev/tty
    fi
    say "enrolling this Deck in your workspace"
    adk rc --node-class "$NODE_CLASS" --once
fi

systemctl --user enable --now aither-deck-shell.service aither-deck-guard.service >/dev/null 2>&1
if [ "$ENROLL" = 1 ]; then
    systemctl --user enable --now aither-deck-node.service >/dev/null 2>&1
fi
"$DATA_DIR/deck-guard.sh" --once || true

echo
if [ "$ENROLL" = 1 ]; then
    ok "done. This Deck is in your fleet."
else
    ok "installed. Join the fleet later with: adk rc --once && systemctl --user enable --now aither-deck-node"
fi
echo "     status:     systemctl --user status 'aither-deck-*'"
echo "     devices:    adk devices list"
echo "     remove all: $DATA_DIR/deck-uninstall.sh"
