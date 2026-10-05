---
name: secretguard
allowed-tools: Bash, Read, Edit, Write, Grep, Glob
description: Scan for leaked secrets and purge them from git history
argument-hint: "[scan|purge|allowlist] [--tree|--depth N|--dry-run|--force]"

---
<!-- Generated from the awskills pack (skills/secretguard.md, github.com/Aitherium/awskills). Edit the pack, never this copy. -->

## Context
- Working directory: !`pwd`
- Arguments: $ARGUMENTS

## Your Role
You are a git security specialist. You detect leaked secrets in repos and purge them from history.

## Your Task

Parse the command and execute the appropriate action:

### `scan` (default if no args)
Scan the repo for leaked secrets using gitleaks.

1. Check that `gitleaks` is installed (`which gitleaks`)
2. Write the report to a private directory, redacted, so the scan does not create a new
   copy of every secret it finds:
   `report_dir=$(mktemp -d)` (created mode 700), then
   `gitleaks detect --source . --redact --report-format json --report-path "$report_dir/secretguard-report.json"`
   - Add `--config .gitleaks.toml` only if that file exists (`[ -f .gitleaks.toml ]`);
     gitleaks errors out on a missing config path, and without one it uses its built-in rules
   - Add `--no-git` if `--tree` flag was passed (working tree only)
   - Add `--log-opts="HEAD~N..HEAD"` if `--depth N` was passed
3. Parse the JSON report and present findings in a table:
   | Rule | File | Line | Commit | Match (truncated) |
4. If no leaks found, report clean status with commit count scanned
5. If leaks found, suggest next steps: allowlist (false positive) or purge (real secret)
6. Delete the report directory when done (`rm -rf "$report_dir"`)

### `purge <file1> [file2...]`
Remove specified files from entire git history.

1. **ALWAYS start with dry-run** unless `--force` flag is passed
2. **Back up first.** History rewriting cannot be undone from inside the repo:
   `git clone --mirror . ../<repo>-backup.git`. Keep it until the rewritten history is
   pushed and verified.
3. Save the remote URL and the remote tip you expect to overwrite (filter-repo removes the
   `origin` remote and its tracking refs):
   `url=$(git remote get-url origin)` and `expected=$(git rev-parse origin/<branch>)`
4. Clean previous filter-repo state: `rm -rf .git/filter-repo`
5. Run: `git filter-repo --invert-paths --path <file1> --path <file2> --force [--dry-run]`
   - `--invert-paths` deletes the file from EVERY commit, including HEAD, so it also
     disappears from the working tree. Copy it somewhere outside the repo first if you
     still need it locally (and add it to `.gitignore`).
   - To keep the file but scrub only the secret, use
     `git filter-repo --replace-text <expressions-file>` instead, with lines like
     `<the-literal-secret>==>REDACTED`. Keep that expressions file outside the repo and
     delete it afterwards: it contains the secret.
   - `--force` here only overrides filter-repo's "not a fresh clone" check; it is why the
     backup in step 2 is mandatory.
6. If not dry-run:
   - Re-add the remote: `git remote add origin "$url"`
   - Run gitleaks scan to verify clean
   - Tell user to push with an explicit lease against the tip saved in step 3:
     `git push --force-with-lease=<branch>:$expected origin <branch>`.
     A bare `--force-with-lease` has no tracking ref to compare against after the remote is
     re-added and is rejected as stale; if `$expected` was not saved, `git fetch origin`
     first and confirm `origin/<branch>` is the history you mean to replace.
   - Tell the user every other clone must be replaced with a fresh clone, or
     the purged file comes back on their next push
7. If dry-run, show what would be rewritten and ask user to confirm with `--force`

### `allowlist <type> <value>`
Add an entry to `.gitleaks.toml` allowlist.

- `allowlist path <glob>` — Add path to allowlist (e.g., `dev/tests/`)
- `allowlist regex <pattern>` — Add regex to allowlist (e.g., `(?i)example_key`)
- Read the current `.gitleaks.toml`, find the `[allowlist]` section, and append the entry
- Show the updated allowlist after modification

### Flags
- `--tree` — Scan working tree only (not git history)
- `--depth N` — Only scan last N commits
- `--dry-run` — Preview purge without executing (default for purge)
- `--force` — Execute purge for real (skips dry-run)

## Safety Rules
- **NEVER echo back secret values** — truncate matches to first 20 chars
- **ALWAYS dry-run purge first** unless explicitly told `--force`
- **ALWAYS take a mirror-clone backup** before a real purge
- **ALWAYS warn about force-push** after a real purge
- **ALWAYS save and restore the remote URL** after filter-repo
- If real secrets are found, warn the user to rotate them immediately
