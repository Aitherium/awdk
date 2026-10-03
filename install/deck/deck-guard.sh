#!/usr/bin/env bash
# Aither on a Steam Deck — the guard. Games come first.
#
# Every few seconds it reads three facts and acts on them:
#   gaming   a Steam game is running (Steam launches every game under
#            `reaper SteamLaunch AppId=<id>`, in Game Mode and Desktop Mode alike)
#   ac       mains power is online (/sys/class/power_supply/*/type == Mains)
#   docked   an external display is connected (a DP/HDMI connector, not the eDP panel)
#
# While gaming: the session daemon and any memory lending are STOPPED, and the
# fleet node is frozen (SIGSTOP via systemd) so it takes no CPU at all.
# When the game exits they come back. Memory is lent only when
# DECK_LEND_MEMORY=1, DECK_HOLDER_CONNECT is set, on AC, docked (unless
# DECK_REQUIRE_DOCK=0) and no game is running.
#
# It writes what it saw to ~/.local/state/aither-deck/presence.json:
#   {"ts": <epoch>, "gaming": bool, "ac": bool, "docked": bool, "lending": bool}
#
#   deck-guard.sh --serve        the loop (aither-deck-guard.service)
#   deck-guard.sh --once         one decision, printed
#   deck-guard.sh --self-test    proves each detector can say both yes and no
#
# Exit: 0 ok, 1 a self-test case failed, 2 cannot judge (no /proc).

set -uo pipefail

INTERVAL="${DECK_GUARD_INTERVAL:-10}"
PROC="${DECK_GUARD_PROC:-/proc}"
SYS="${DECK_GUARD_SYS:-/sys}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/aither-deck"
LEND="${DECK_LEND_MEMORY:-0}"
CONNECT="${DECK_HOLDER_CONNECT:-}"
REQUIRE_DOCK="${DECK_REQUIRE_DOCK:-1}"
SYSTEMCTL="${DECK_GUARD_SYSTEMCTL:-systemctl}"

is_gaming() {  # 0 when a Steam game process exists
    local cmd f
    for f in "$PROC"/[0-9]*/cmdline; do
        [ -r "$f" ] || continue
        cmd="$(tr '\0' ' ' < "$f" 2>/dev/null)" || continue
        case "$cmd" in
            *"SteamLaunch AppId="*) return 0 ;;
        esac
    done
    return 1
}

on_ac() {
    local d
    for d in "$SYS"/class/power_supply/*; do
        [ -r "$d/type" ] || continue
        if [ "$(cat "$d/type")" = "Mains" ] && [ "$(cat "$d/online" 2>/dev/null)" = "1" ]; then
            return 0
        fi
    done
    return 1
}

is_docked() {  # an external connector is connected; the built-in panel is eDP
    local s name
    for s in "$SYS"/class/drm/card*-*/status; do
        [ -r "$s" ] || continue
        name="$(basename "$(dirname "$s")")"
        case "$name" in *-eDP-*) continue ;; esac
        [ "$(cat "$s")" = "connected" ] && return 0
    done
    return 1
}

b() { if "$@"; then echo true; else echo false; fi; }

unit_active() { "$SYSTEMCTL" --user is-active --quiet "$1"; }

decide() {
    local gaming ac docked lend=false
    gaming="$(b is_gaming)"
    ac="$(b on_ac)"
    docked="$(b is_docked)"
    if [ "$gaming" = false ] && [ "$LEND" = 1 ] && [ -n "$CONNECT" ] && [ "$ac" = true ] \
        && { [ "$REQUIRE_DOCK" = 0 ] || [ "$docked" = true ]; }; then
        lend=true
    fi

    if [ "$gaming" = true ]; then
        unit_active aither-deck-holder.service && "$SYSTEMCTL" --user stop aither-deck-holder.service
        unit_active aither-deck-shell.service && "$SYSTEMCTL" --user stop aither-deck-shell.service
        unit_active aither-deck-node.service \
            && "$SYSTEMCTL" --user kill --signal=SIGSTOP aither-deck-node.service
        : > "$STATE_DIR/frozen"
    else
        if [ -e "$STATE_DIR/frozen" ]; then
            "$SYSTEMCTL" --user kill --signal=SIGCONT aither-deck-node.service 2>/dev/null
            rm -f "$STATE_DIR/frozen"
        fi
        if "$SYSTEMCTL" --user is-enabled --quiet aither-deck-shell.service 2>/dev/null; then
            unit_active aither-deck-shell.service || "$SYSTEMCTL" --user start aither-deck-shell.service
        fi
        if [ "$lend" = true ]; then
            unit_active aither-deck-holder.service || "$SYSTEMCTL" --user start aither-deck-holder.service
        else
            unit_active aither-deck-holder.service && "$SYSTEMCTL" --user stop aither-deck-holder.service
        fi
    fi

    local line
    line="$(printf '{"ts": %s, "gaming": %s, "ac": %s, "docked": %s, "lending": %s}' \
        "$(date +%s)" "$gaming" "$ac" "$docked" "$lend")"
    printf '%s\n' "$line" > "$STATE_DIR/presence.json.tmp" \
        && mv "$STATE_DIR/presence.json.tmp" "$STATE_DIR/presence.json"
    printf '%s\n' "$line"
}

self_test() {
    local root fails=0
    root="$(mktemp -d)"
    mkdir -p "$root/proc/101" "$root/proc/202" "$root/sys/class/power_supply/ACAD" \
        "$root/sys/class/power_supply/BAT1" "$root/sys/class/drm/card0-eDP-1" \
        "$root/sys/class/drm/card0-DP-1"
    printf 'bash\0-l\0' > "$root/proc/101/cmdline"
    echo Mains > "$root/sys/class/power_supply/ACAD/type"
    echo Battery > "$root/sys/class/power_supply/BAT1/type"
    echo 1 > "$root/sys/class/power_supply/BAT1/online"
    echo connected > "$root/sys/class/drm/card0-eDP-1/status"

    check() {  # name expected(0|1) fn
        local name="$1" want="$2" got=0
        PROC="$root/proc" SYS="$root/sys" "$3" || got=1
        if [ "$got" = "$want" ]; then echo "  pass  $name"; else echo "  FAIL  $name"; fails=$((fails + 1)); fi
    }
    printf '\0' > "$root/proc/202/cmdline"
    check "no game -> not gaming" 1 is_gaming
    printf '/home/deck/.steam/ubuntu12_32/reaper\0SteamLaunch AppId=1145360\0--\0game\0' \
        > "$root/proc/202/cmdline"
    check "reaper SteamLaunch -> gaming" 0 is_gaming
    echo 0 > "$root/sys/class/power_supply/ACAD/online"
    check "battery only -> not on AC" 1 on_ac
    echo 1 > "$root/sys/class/power_supply/ACAD/online"
    check "ACAD online -> on AC" 0 on_ac
    echo disconnected > "$root/sys/class/drm/card0-DP-1/status"
    check "built-in panel only -> not docked" 1 is_docked
    echo connected > "$root/sys/class/drm/card0-DP-1/status"
    check "DP-1 connected -> docked" 0 is_docked
    rm -rf "$root"
    [ "$fails" = 0 ]
}

MODE="${1:---once}"
case "$MODE" in
    --self-test) self_test; exit $? ;;
    --once|--serve) ;;
    *) echo "usage: deck-guard.sh --serve | --once | --self-test" >&2; exit 2 ;;
esac

[ -d "$PROC/1" ] || { echo "cannot judge: no $PROC" >&2; exit 2; }
mkdir -p "$STATE_DIR"

if [ "$MODE" = "--serve" ]; then
    last=""
    while true; do
        now="$(decide | sed 's/"ts": [0-9]*, //')"
        [ "$now" != "$last" ] && echo "$now"   # transitions only
        last="$now"
        sleep "$INTERVAL"
    done
fi
decide
