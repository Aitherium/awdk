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

import copy
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

#: Array-valued keys that MERGE as a union rather than replacing.
_UNION_ARRAYS = (
    ("permissions", "allow"),
    ("permissions", "deny"),
    ("permissions", "ask"),
    ("permissions", "additionalDirectories"),
    ("enabledMcpjsonServers",),
    ("disabledMcpjsonServers",),
)

#: Keys whose VALUES are credentials or credential-fetching commands. Dropped
#: wholesale from any snapshot that leaves this machine.
_SECRET_KEYS = frozenset({
    "env",                    # arbitrary values, routinely tokens
    "apiKeyHelper",
    "proxyAuthHelper",
    "awsCredentialExport",
    "awsAuthRefresh",
    "gcpAuthRefresh",
    "otelHeadersHelper",
    "policyHelper",
})

#: Sub-object of `sandbox` that holds credential material.
_SECRET_SUBKEYS = {"sandbox": frozenset({"credentials"})}

#: Keys that are meaningful to sync. Anything else is left alone rather than
#: guessed at — an unknown key is more likely a local experiment than a
#: preference somebody wants pushed to every machine they own.
_SYNCED_KEYS = frozenset({
    "permissions",
    "enabledMcpjsonServers",
    "disabledMcpjsonServers",
    "enableAllProjectMcpServers",
    "hooks",
    "outputStyle",
    "statusLine",
    "alwaysThinkingEnabled",
    "autoCompactEnabled",
    "spinnerTipsEnabled",
    "todoFeatureEnabled",
    "attribution",
})


class CouldNotRunError(Exception):
    """No verdict is possible. Callers exit 2."""


# ---------------------------------------------------------------------------
# Pure functions. Everything below is offline-testable on purpose: the merge and
# the redaction are where this can silently do harm, and neither needs a portal.
# ---------------------------------------------------------------------------

def redact(settings: dict[str, Any]) -> dict[str, Any]:
    """Strip credential material and unsynced keys. Never mutates the input.

    A key is dropped by NAME, not by sniffing the value: a heuristic that looks
    for token-shaped strings passes every secret that does not look like one,
    and that failure is invisible until the secret is already published.
    """
    out: dict[str, Any] = {}
    for k, v in settings.items():
        if k in _SECRET_KEYS or k not in _SYNCED_KEYS:
            continue
        if k in _SECRET_SUBKEYS and isinstance(v, dict):
            v = {sk: sv for sk, sv in v.items() if sk not in _SECRET_SUBKEYS[k]}
        out[k] = copy.deepcopy(v)
    return out


def _get(d: dict[str, Any], path: tuple[str, ...]):
    cur: Any = d
    for seg in path:
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _set(d: dict[str, Any], path: tuple[str, ...], value) -> None:
    cur = d
    for seg in path[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[path[-1]] = value


def merge(local: dict[str, Any], remote: dict[str, Any], *,
          prune_denies: bool = False) -> dict[str, Any]:
    """Portal over local, with UNION on the array keys. Never mutates inputs.

    `prune_denies` is the only way to remove a deny/ask rule through a sync, and
    it is off by default: see the contract in the module docstring. A missing
    allow rule costs a prompt; a missing deny rule costs the thing it prevented.
    """
    out = copy.deepcopy(local)

    for k, v in remote.items():
        if k in _SECRET_KEYS:
            continue                      # a portal must not push credentials down
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            merged = dict(out[k])
            merged.update(v)
            out[k] = merged
        else:
            out[k] = copy.deepcopy(v)

    for path in _UNION_ARRAYS:
        lv, rv = _get(local, path), _get(remote, path)
        if lv is None and rv is None:
            continue
        lv = lv if isinstance(lv, list) else []
        rv = rv if isinstance(rv, list) else []
        if prune_denies and path[-1] in ("deny", "ask"):
            union = list(rv)
        else:
            union = list(lv)
            union += [x for x in rv if x not in union]
        _set(out, path, union)

    # Anything local-only and secret stays exactly as it was: the loop above
    # never reaches a key the remote does not carry, and redact() kept it out of
    # what we send. Stated because "it is preserved by omission" is the kind of
    # property that gets refactored away.
    for k in _SECRET_KEYS:
        if k in local:
            out[k] = copy.deepcopy(local[k])
    return out


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
