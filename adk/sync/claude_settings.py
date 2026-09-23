"""Claude Code settings sync — the permission and MCP config that follows you.

Sibling of :mod:`adk.sync.settings`. That module syncs *adk's own* config
(`~/.aither/config.yaml` — LLM backend, packs, external MCP servers). This one
syncs **Claude Code's** `.claude/settings*.json`: the permission allowlist, the
enabled MCP servers, hooks, and the non-secret preference keys.

WHY THIS EXISTS, measured 2026-09-05. An owner-authorized publish was blocked by
the permission classifier. The fix was one allow rule in
`.claude/settings.local.json` — and that rule then existed on exactly one
machine. The same wall is waiting on every other surface the same person works
from: `awsh`, an `awdk` agent host, and the dev container that
`tunnel.aitherium.com` hands a phone. Each is a fresh box with a fresh
`.claude/`, so the same interruption is re-paid per surface, by hand, forever.

Nothing synced it. `adk.sync.settings` is deliberately scoped to adk's own
config and says so; `lib/agent_packs/compile_claude_code.py` writes
`.claude/agents/<id>.md`, not settings. The gap was exact.

CONTRACT — same three decisions as :mod:`adk.sync.settings`, for the same reasons:

  * **The portal is the source of truth** for the shared set. Pull applies it
    over the local file; push sends a fresh snapshot, debounced and fail-soft.
  * **Offline keeps working.** A failed pull leaves the local file untouched;
    the next successful one reconciles. A surface that cannot reach the portal
    must still start.
  * **Secrets NEVER travel.** `env` values, `apiKeyHelper`, `awsCredentialExport`,
    `gcpAuthRefresh`, `otelHeadersHelper`, and everything under
    `sandbox.credentials` are device-local. Only key NAMES are carried where a
    name is structural.

...and three that are specific to this file:

  * **It writes `settings.local.json`, never `settings.json`.** The project file
    is committed and shared with the team; syncing one person's permission
    allowlist into it would hand everyone else rules they never approved, in a
    file code review reads as policy. `settings.local.json` is gitignored and
    personal, which is exactly the scope of "my settings follow me".

  * **Arrays UNION; they are never replaced.** This is not a preference. The
    replace-semantics failure happened twice in one session on this very repo:
    a peer copy-over of `gate_lanes.yaml` silently dropped a gate entry, and the
    file's own footer warns about it ("MERGE this table, never REPLACE it"). A
    settings sync with replace semantics is that defect with a network hop —
    device B quietly loses the rule device A never had.

  * **`deny` and `ask` are one-way: a sync may ADD one, never drop one.** An
    allow rule going missing costs a prompt. A deny rule going missing costs the
    thing the deny existed to prevent, silently, on a machine whose owner
    believes it is still there. The asymmetry is deliberate; `prune_denies` has
    to be asked for explicitly and is never the default.

Environment:
  * ``AITHER_CLAUDE_SETTINGS_SYNC`` — ``true`` / ``false`` / ``auto`` (default
    ``auto``: on when a portal token resolves, off otherwise).
  * ``AITHER_PORTAL_URL`` — portal base (default ``https://api.aitherium.com``).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Portal namespace. Sibling of ``preferences.adk`` so the two never stomp.
PREF_NAMESPACE = "claude_code"

#: The file a pull writes. See the contract above — never ``settings.json``.
LOCAL_SETTINGS = ".claude/settings.local.json"

# The redaction and merge rules live in `awsettings.core` (its `claude` domain),
# the published brick every aw* surface syncs through. This module kept a private
# copy until 2026-09-22 and it had drifted: `sandbox` was missing from the synced
# keys, so the `sandbox.credentials` guard here could never run and a sandbox
# network allowlist never travelled. One implementation now; the underscored
# names stay importable for existing callers.
from awsettings.core import (  # noqa: E402
    SECRET_KEYS as _SECRET_KEYS,
    SECRET_SUBKEYS as _SECRET_SUBKEYS,
    SYNCED_KEYS as _SYNCED_KEYS,
    UNION_ARRAYS as _UNION_ARRAYS,
    _get,
    _set,
    merge,
    redact,
)

__all__ = [
    "LOCAL_SETTINGS", "PREF_NAMESPACE", "CouldNotRunError", "merge", "redact",
    "read_settings", "write_settings", "sync_enabled", "self_test",
    "_SECRET_KEYS", "_SECRET_SUBKEYS", "_SYNCED_KEYS", "_UNION_ARRAYS", "_get", "_set",
]


class CouldNotRunError(Exception):
    """No verdict is possible. Callers exit 2."""


def read_settings(root: Path) -> dict[str, Any]:
    """The local file, or {} when absent. A malformed file RAISES rather than
    reading as empty — silently treating unparseable settings as "no settings"
    is how a sync overwrites a file somebody was mid-edit on."""
    p = root / LOCAL_SETTINGS
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CouldNotRunError(f"{p} is not valid JSON: {exc}") from exc


def write_settings(root: Path, data: dict[str, Any]) -> Path:
    """Atomic write of the local settings file. Claude Code watches this path;
    a half-written file is briefly invalid JSON, which disables EVERY setting in
    it rather than the one being changed."""
    p = root / LOCAL_SETTINGS
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, p)
    return p


def sync_enabled() -> bool:
    v = (os.getenv("AITHER_CLAUDE_SETTINGS_SYNC") or "auto").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return bool(os.getenv("AITHER_PORTAL_TOKEN") or os.getenv("AITHER_SESSION_BEARER"))


def self_test() -> int:
    """Offline proof of the two properties that can do harm: redaction and merge."""
    problems: list[str] = []

    # --- redaction ---------------------------------------------------------
    src = {
        "env": {"OPENAI_API_KEY": "sk-real"},
        "apiKeyHelper": "/bin/print-my-token",
        "sandbox": {"enabled": True, "credentials": {"envVars": [{"name": "X"}]}},
        "permissions": {"allow": ["Bash(git *)"]},
        "someLocalExperiment": 1,
    }
    red = redact(src)
    if "env" in red or "apiKeyHelper" in red:
        problems.append("redact() let a credential key through")
    if "credentials" in red.get("sandbox", {}):
        problems.append("redact() left sandbox.credentials in the snapshot")
    if red.get("permissions", {}).get("allow") != ["Bash(git *)"]:
        problems.append("redact() dropped a permission rule it should carry")
    if "someLocalExperiment" in red:
        problems.append("redact() pushed an unknown local key instead of leaving it home")
    if src["env"]["OPENAI_API_KEY"] != "sk-real":
        problems.append("redact() MUTATED its input")

    # --- merge: union, not replace (the gate_lanes.yaml lesson) -------------
    local = {"permissions": {"allow": ["Bash(local-only *)"], "deny": ["Bash(rm -rf *)"]},
             "enabledMcpjsonServers": ["aitheros"],
             "env": {"SECRET": "keep-me"}}
    remote = {"permissions": {"allow": ["Bash(from-portal *)"]},
              "enabledMcpjsonServers": ["awsh"]}
    m = merge(local, remote)
    allow = m["permissions"]["allow"]
    if "Bash(local-only *)" not in allow or "Bash(from-portal *)" not in allow:
        problems.append(f"merge() did not UNION allow rules: {allow!r} — a device "
                        f"silently loses the rule the other one never had")
    if m["permissions"].get("deny") != ["Bash(rm -rf *)"]:
        problems.append("merge() dropped a local deny rule the remote did not carry")
    if sorted(m["enabledMcpjsonServers"]) != ["aitheros", "awsh"]:
        problems.append("merge() did not union enabledMcpjsonServers")
    if m.get("env", {}).get("SECRET") != "keep-me":
        problems.append("merge() lost a device-local secret it should never touch")
    if local["permissions"]["allow"] != ["Bash(local-only *)"]:
        problems.append("merge() MUTATED its local input")

    # --- merge: a portal may not push credentials DOWN ---------------------
    if merge({}, {"env": {"X": "y"}, "apiKeyHelper": "/evil"}).get("env") is not None:
        problems.append("merge() accepted an env block pushed from the portal")

    # --- deny/ask are one-way unless explicitly pruned ---------------------
    kept = merge({"permissions": {"deny": ["Bash(curl *)"]}}, {"permissions": {"deny": []}})
    if kept["permissions"]["deny"] != ["Bash(curl *)"]:
        problems.append("a deny rule was dropped by a default sync — that is the one "
                        "direction this must never go")
    pruned = merge({"permissions": {"deny": ["Bash(curl *)"]}},
                   {"permissions": {"deny": []}}, prune_denies=True)
    if pruned["permissions"]["deny"] != []:
        problems.append("prune_denies=True did not actually prune")

    # --- the target file is the personal one, never the committed one -------
    if LOCAL_SETTINGS != ".claude/settings.local.json":
        problems.append("the write target is not settings.local.json — syncing into "
                        "the committed settings.json hands the whole team one "
                        "person's permission rules")

    if problems:
        print("SELF-TEST FAILED:")
        for p in problems:
            print("  x " + p)
        return 1
    print("self-test ok: credentials never leave or arrive, arrays union instead of "
          "replacing, a deny is never dropped by default, inputs are not mutated, and "
          "the write target is the personal settings file")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test() if "--self-test" in sys.argv else 0)
