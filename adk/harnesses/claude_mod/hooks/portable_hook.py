"""The plugin's portable hooks: each one is a silent no-op when its brick is missing.

Run BY PATH (the plugin cache holds only this directory, not the adk package):

    python portable_hook.py relay-inbox     # PostToolUse: awrelay in-turn inbox via hookgate
    python portable_hook.py awm-recall      # UserPromptSubmit: awm recall into context
    python portable_hook.py awvoice-reply   # Stop: speak the final reply (awvoice's own opt-in)

Contract (every mode): stdlib only, exit 0 on every path, nothing on stdout unless the
brick itself produced context, and nothing at all when the brick is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RELAY_INTERVAL_S = "20"
TIMEOUT_S = 8.0
RECALL_LIMIT = "5"
QUERY_CHARS = 200


def _run(cmd: list[str], stdin: str, timeout: float = TIMEOUT_S) -> str:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def hookgate_path() -> Path | None:
    """awrelay's stdlib-only gate, located without importing the awrelay package."""
    try:
        spec = importlib.util.find_spec("awrelay")
    except (ImportError, ValueError):
        return None
    for loc in (spec.submodule_search_locations or []) if spec else []:
        gate = Path(loc) / "hookgate.py"
        if gate.is_file():
            return gate
    return None


def relay_inbox(raw: str) -> str:
    gate = hookgate_path()
    if gate is None:
        return ""
    return _run([sys.executable, "-S", "-I", str(gate), "--min-interval", RELAY_INTERVAL_S], raw)


def awm_recall(raw: str) -> str:
    scope = os.environ.get("AWM_SCOPE", "").strip()
    exe = shutil.which("awm")
    if not scope or exe is None:
        return ""
    try:
        prompt = str((json.loads(raw or "{}") or {}).get("prompt") or "")
    except (ValueError, AttributeError):
        prompt = ""
    query = " ".join(prompt.split())[:QUERY_CHARS]
    if not query:
        return ""
    return _run([exe, "recall", "--scope", scope, "--query", query, "--limit", RECALL_LIMIT], "")


def awvoice_reply(raw: str) -> str:
    exe = shutil.which("awvoice")
    if exe is None:
        return ""
    _run([exe, "reply"], raw)
    return ""  # a Stop hook's stdout is not context; awvoice speaks, it does not print


MODES = {"relay-inbox": relay_inbox, "awm-recall": awm_recall, "awvoice-reply": awvoice_reply}


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    fn = MODES.get(args[0]) if args else None
    if fn is None:
        return 0
    try:
        raw = sys.stdin.read() if stdin_text is None else stdin_text
    except (OSError, ValueError):
        raw = ""
    try:
        out = fn(raw)
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        return 0
    if out.strip():
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
