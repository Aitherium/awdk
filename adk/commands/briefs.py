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
import re
import sys
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


def read_index() -> "tuple[dict, str]":
    """(index, why): ``why`` is "" when the index was read. Never raises.

    A missing index and one that is not a JSON object both come back with a
    reason, so a caller can tell "no notebooks" from "could not look" and exit
    2 for the second instead of printing an empty list.
    """
    idx = _briefs_dir() / "index.json"
    if not idx.is_file():
        return {}, "no notebook index on this host yet (%s)" % idx
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, "could not read %s: %s" % (idx, exc)
    if not isinstance(data, dict):
        return {}, "%s is not a JSON object" % idx
    return data, ""


_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _brief_title(session_id: str, archive: str) -> str:
    """The first line of the session's brief, so a row says what the work was.

    Briefs open with ``EXECUTIVE-BRIEF: <title>``; "" when none was written.
    """
    candidates = []
    if _SESSION_RE.match(session_id) and ".." not in session_id:
        candidates.append(_briefs_dir() / ("%s.md" % session_id))
    if archive:
        candidates.append(Path(archive))
    for path in candidates:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                line = fh.readline(400).strip().lstrip("#").strip()
        except OSError:
            continue
        if line.upper().startswith("EXECUTIVE-BRIEF:"):
            line = line.split(":", 1)[1].strip()
        if line:
            return line[:160]
    return ""


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


def notebooks_from_index(index: "dict | None" = None) -> "list[dict]":
    """Every notebook id this host recorded, newest first.

    The index is the ONLY thing that still knows a notebook exists when the
    platform is unreachable, so `adk notebook list --local` and the offline
    fallback in the CLI both read it from here instead of re-deriving the
    shape a second time. Pass ``index`` when it was already read (read_index).
    """
    rows: "list[dict]" = []
    source = _load_index() if index is None else index
    for session_id, entry in (source or {}).items():
        if not isinstance(entry, dict):
            continue
        notebook = (entry.get("surfaces") or {}).get("notebook") or {}
        notebook_id = str(notebook.get("id") or "")
        if not notebook_id:
            continue
        surfaces = entry.get("surfaces") or {}
        brief = str((surfaces.get("archive") or {}).get("path", "") or "")
        rows.append({
            "session": session_id,
            "created": str(entry.get("created", "")),
            "notebook": notebook_id,
            "at": str(notebook.get("at", "")),
            # What the desk's "Open transcript" and an agent reading the brief
            # offline need; both are host paths the index already carries.
            # session_to_notebook writes it beside the notebook id; older rows
            # carry it at the top level. Read both (10-29: 7 of 11 rows were blank).
            "log_path": str(notebook.get("log_path") or entry.get("log_path", "") or ""),
            "brief": brief,
            "title": _brief_title(str(session_id), brief),
        })
    rows.sort(key=lambda row: row["created"], reverse=True)
    return rows


#: Where a notebook opens in AitherOne (the same link the desk's View uses).
VIEW_URL = "https://aitherium.com/workspace/one?tab=notebooks&id=%s"


def _printable(text: str) -> str:
    """Titles are free text; a console that cannot encode a character gets a ?."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, "replace").decode(encoding, "replace")


def print_notebooks(rows: "list[dict]") -> None:
    """The table shared by `adk notebook list --local` and the offline fallback."""
    if not rows:
        print("  No notebooks recorded on this host yet.")
        return
    print()
    print("  %-10s %-16s %-12s %s" % ("Session", "Created", "Notebook", "Title"))
    for row in rows[:50]:
        print("  %-10s %-16s %-12s %s" % (row["session"][:8],
                                          row["created"][:16],
                                          row["notebook"],
                                          _printable(str(row.get("title") or "")[:70])))
    print()
    print("  view   " + VIEW_URL % rows[0]["notebook"])
    # A run re-executes every agent cell; look before running anything.
    print("  look   adk notebook run <id> --dry-run   (what a replay would execute; runs nothing)")


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
