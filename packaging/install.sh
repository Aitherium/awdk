#!/bin/sh
# Aither installer -- thin delegator to the ONE installer door.
#
# The real installer (every profile, every mode, AitherZero provisioning) lives in
# exactly one place: https://aitherium.com/install.sh. This file only fetches it and
# forwards every argument, so an old link to this copy keeps working and there is
# still one source of truth.
#
#   sh install.sh                              # the start console
#   sh install.sh --profile full               # everything
#   sh install.sh --profile harness            # + the local harness daemon
#   sh install.sh --profile team|deck|bonsai|onprem
#   sh install.sh --aitherzero                 # provision with AitherZero first
#
# --skip-wizard (this file's old flag) is accepted and ignored.
set -eu

BASE="${AITHER_INSTALL_BASE:-https://aitherium.com}"
command -v curl >/dev/null 2>&1 || { echo "install.sh: curl is required" >&2; exit 2; }
command -v bash >/dev/null 2>&1 || { echo "install.sh: bash is required" >&2; exit 2; }

n=$#; i=0
while [ "$i" -lt "$n" ]; do
    a="$1"; shift; i=$((i + 1))
    [ "$a" = "--skip-wizard" ] || set -- "$@" "$a"
done

# Fetch first, then run: `curl | bash` reports bash's exit, so a 404 would install
# nothing and still report success.
door="$(curl -fsSL "$BASE/install.sh")" || { echo "install.sh: could not fetch $BASE/install.sh" >&2; exit 2; }
printf '%s' "$door" | bash -s -- "$@"
