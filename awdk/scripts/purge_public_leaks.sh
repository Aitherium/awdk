#!/usr/bin/env bash
# Purge the moat-leaking artifacts from the PUBLIC awdk surface.
#
# Scope (everything before 2.0.0 shipped adk/nanogpt.py + ungated fleet/forge):
#   1. GitHub Releases  — delete every pre-2.0 release on Aitherium/awdk
#   2. Git tags         — delete the matching pre-2.0 tags
#   3. Git history      — scrub adk/nanogpt.py from the public repo's history
#   4. PyPI             — (manual) yank pre-2.0 releases (no API; see note)
#
# SAFETY: dry-run by default. Destructive steps require explicit flags AND are
# irreversible + outward-facing (they break pinned installs and existing
# clones/forks). Read what it prints before running with --execute.
#
# Usage:
#   scripts/purge_public_leaks.sh                       # dry-run, show plan
#   scripts/purge_public_leaks.sh --execute             # delete releases + tags
#   scripts/purge_public_leaks.sh --execute --rewrite-history   # + scrub history
#
# Requires: gh (authenticated), git, and (for history) git-filter-repo.

set -euo pipefail

REPO="Aitherium/awdk"
REMOTE_URL="https://github.com/${REPO}.git"
LEAK_PATH="adk/nanogpt.py"          # the file to scrub from history
KEEP_FROM="2.0.0"                    # first clean release

EXECUTE=0
REWRITE=0
for arg in "$@"; do
  case "$arg" in
    --execute) EXECUTE=1 ;;
    --rewrite-history) REWRITE=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

note() { printf '\033[36m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }

# Is a tag (vMAJOR.MINOR.PATCH) strictly below KEEP_FROM?
is_pre_2() {
  local t="${1#v}"
  local major="${t%%.*}"
  [[ "$major" =~ ^[0-9]+$ ]] && (( major < 2 ))
}

note "== awdk public leak purge (repo: ${REPO}) =="
[[ $EXECUTE -eq 0 ]] && warn "DRY RUN — nothing will be deleted. Add --execute to act."

# ---- 1 + 2: releases & tags ------------------------------------------------
note ""
note "[1/3] Pre-${KEEP_FROM} GitHub releases + tags to remove:"
mapfile -t TAGS < <(gh release list --repo "$REPO" --limit 200 \
                      --json tagName --jq '.[].tagName')

TARGETS=()
for tag in "${TAGS[@]}"; do
  # only version tags (skip shell-v*, etc.) that are pre-2.0
  [[ "$tag" =~ ^v[0-9] ]] || continue
  if is_pre_2 "$tag"; then TARGETS+=("$tag"); fi
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  note "  (none found)"
else
  printf '  %s\n' "${TARGETS[@]}"
  if [[ $EXECUTE -eq 1 ]]; then
    for tag in "${TARGETS[@]}"; do
      note "  deleting release+tag $tag ..."
      gh release delete "$tag" --repo "$REPO" --yes --cleanup-tag || \
        warn "  (release delete failed for $tag — may be tag-only)"
      git push "$REMOTE_URL" --delete "$tag" 2>/dev/null || true
    done
  fi
fi

# ---- 3: history scrub ------------------------------------------------------
note ""
note "[3/3] Scrub ${LEAK_PATH} from public repo history:"
if [[ $REWRITE -eq 1 && $EXECUTE -eq 1 ]]; then
  command -v git-filter-repo >/dev/null 2>&1 || {
    warn "  git-filter-repo not installed: pip install git-filter-repo"; exit 3; }
  WORK="$(mktemp -d)"
  note "  cloning mirror into $WORK (a fresh mirror does NOT inherit the"
  note "  monorepo pre-push hook, so it can push to the public repo) ..."
  git clone --mirror "$REMOTE_URL" "$WORK/awdk.git"
  ( cd "$WORK/awdk.git"
    git filter-repo --force --invert-paths --path "$LEAK_PATH"
    # Drop every pre-2.0 version tag (they mark leaky, ungated releases). The
    # matching GitHub releases were already deleted in step 1.
    for tag in $(git tag); do
      [[ "$tag" =~ ^v[0-9] ]] || continue
      if is_pre_2 "$tag"; then git tag -d "$tag" >/dev/null; fi
    done
    # Verify the leak is gone from ALL history before the irreversible push.
    if [[ -n "$(git log --all --oneline -- "$LEAK_PATH")" ]]; then
      warn "  ABORT: $LEAK_PATH still present in history after filter — not pushing."
      exit 4
    fi
    note "  verified: $LEAK_PATH absent from all history; $(git rev-list --count HEAD) commits remain."
    warn "  FORCE-PUSHING rewritten history + tag deletions (irreversible) ..."
    git push --force --mirror "$REMOTE_URL"
  )
  rm -rf "$WORK"
  note "  history scrub complete."
else
  warn "  skipped (needs --execute --rewrite-history)."
  warn "  Manual equivalent:"
  echo  "    git clone --mirror $REMOTE_URL && cd awdk.git"
  echo  "    git filter-repo --invert-paths --path $LEAK_PATH"
  echo  "    git push --force --mirror $REMOTE_URL"
fi

# ---- 4: PyPI (manual) ------------------------------------------------------
note ""
note "[PyPI] No API for yank/delete — run the helper for the version list + steps:"
echo  "    python scripts/yank_leaky_releases.py --verify"

note ""
warn "REMINDER: public code that was already installed/cloned/forked cannot be"
warn "truly un-published. This reduces exposure; it is not erasure. If nanogpt"
warn "or any removed module carried secrets, rotate them now."
