"""Executive briefs access: list, show.

The briefs delivery plane writes one report per session to
~/.aither/briefs/<session-id>.md plus a machine index. This command reads
the same store the stop hook writes, so an agent can answer "what did the
sessions do" without hunting transcripts:

  adk briefs            - list the recorded briefs (newest first)
  adk briefs show <id>  - print one brief in full

The store is host-local by design (the hook runs on the owner's box); on a
machine without it the command says so plainly rather than guessing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _briefs_dir() -> Path:
    root = os.environ.get("AITHER_BRIEFS_DIR")
    if root:
        return Path(root)
    return Path(os.path.expanduser("~")) / ".aither" / "briefs"


def _load_index() -> dict:
    idx = _briefs_dir() / "index.json"
    if not idx.is_file():
        return {}
    try:
        return json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cmd_briefs_list(args: Any) -> int:
    """List recorded briefs, newest first, with their delivery surfaces."""
    index = _load_index()
    if not index:
        print("  No briefs recorded yet (the stop hook writes them as "
              "sessions close).")
        return 0
    rows = sorted(index.items(), key=lambda kv: str(kv[1].get("created", "")),
                  reverse=True)[:20]
    print()
    print("  %-10s %-18s %-10s %-8s" % ("Session", "Created", "Notebook",
                                        "Discord"))
    for session_id, entry in rows:
        surfaces = entry.get("surfaces", {})
        created = str(entry.get("created", ""))[:16]
        nb = surfaces.get("notebook", {}).get("id", "—")
        card = surfaces.get("discord", {}).get("card", "—")
        print("  %-10s %-18s %-10s %-8s" % (session_id[:8], created, nb,
                                            card))
    print()
    print("  adk briefs show <session-id> — read one brief in full")
    return 0


def cmd_briefs_show(args: Any) -> int:
    """Print one brief's report file in full."""
    session_id = str(getattr(args, "brief_id", "") or "").strip()
    if not session_id:
        print("  usage: adk briefs show <session-id>", file=os.sys.stderr)
        return 2
    path = _briefs_dir() / ("%s.md" % session_id)
    if not path.is_file():
        print("  No brief recorded for %s." % session_id)
        return 1
    print(path.read_text(encoding="utf-8", errors="replace"))
    return 0
