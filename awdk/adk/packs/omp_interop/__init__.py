"""Interop with Oh My Pi (omp) sessions — read its history, speak its tool names.

Derived from work in https://github.com/can1357/oh-my-pi (MIT). See NOTICE.

Why this pack exists: an omp session recorded with `externalThinking` ON already
contains raw chain-of-thought, sitting in the arguments of its `think` tool
calls. That makes an omp history the cheapest high-quality reasoning corpus
available — it is a by-product of work someone was doing anyway, rather than
probe prompts we had to invent.

The schema is DISCOVERED, not assumed
-------------------------------------
omp keeps its history in SQLite under ``~/.omp``. This pack was written without
a local omp install to read, so it does not hardcode a table layout it cannot
verify. :func:`omp_session_import` introspects ``sqlite_master``, scores each
table for the columns it needs, and when nothing matches it returns
``ok=False`` with the tables it actually found.

That distinction is the whole point. An importer that returns an empty list on
an unrecognised schema is indistinguishable from one pointed at a real database
with no traces in it — and the two call for opposite responses. Every function
here reports "I could not look" separately from "I looked and there was
nothing".
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("omp_interop_pack")

PACK_ID = "omp-interop"

#: omp's scratchpad tool is named `think`; ours is `deep_think`. Both are read.
THINK_TOOL_NAMES = ("think", "deep_think")

#: omp tool name -> the adk built-in that does the same job. Used to translate a
#: recorded omp session into something an adk agent's history can represent.
#: Only unambiguous pairs are listed — a wrong mapping produces a corpus that
#: teaches the wrong tool for the job, which is worse than an unmapped name.
OMP_TO_ADK_TOOLS: Dict[str, str] = {
    "read": "file_read",
    "write": "file_write",
    "edit": "file_edit",
    "list": "file_list",
    "glob": "file_list",
    "grep": "file_search",
    "bash": "shell_exec",
    "eval": "python_exec",
    "web_search": "web_search",
    "web_fetch": "web_fetch",
    "ast": "code_symbols",
    "task": "swarm_code",
}

#: Column names we need, in priority order, keyed by the role they play.
_COLUMN_HINTS: Dict[str, tuple] = {
    "role": ("role", "author", "sender", "kind", "type"),
    "content": ("content", "text", "body", "message", "data", "payload"),
    "tool": ("tool", "tool_name", "name", "function", "function_name"),
    "args": ("arguments", "args", "input", "params", "parameters", "payload", "data"),
    "session": ("session_id", "session", "conversation_id", "thread_id"),
}

_TOOL_NAMES = [
    "omp_session_import",
    "omp_tool_map",
    "omp_locate",
]


def _default_omp_root() -> Path:
    return Path(os.environ.get("OMP_HOME") or (Path.home() / ".omp"))


def omp_locate(root: str = "") -> Dict[str, Any]:
    """Find omp's SQLite databases. Reports absence as a fact, not an error."""
    base = Path(root) if root else _default_omp_root()
    if not base.exists():
        return {
            "ok": True, "installed": False, "root": str(base), "databases": [],
            "reason": "no omp directory — omp is not installed for this user",
        }
    databases = sorted(str(p) for p in base.rglob("*.db") if p.is_file())
    return {
        "ok": True,
        "installed": True,
        "root": str(base),
        "databases": databases,
        "reason": "" if databases else "omp directory exists but holds no .db files",
    }


def _tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return [r[0] for r in rows]


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    # Table name comes from sqlite_master, not from a caller, so it cannot be
    # attacker-chosen; quoted anyway because PRAGMA takes no bind parameter.
    safe = table.replace('"', '""')
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{safe}")').fetchall()]


def _match_column(columns: List[str], role: str) -> Optional[str]:
    lowered = {c.lower(): c for c in columns}
    for hint in _COLUMN_HINTS[role]:
        if hint in lowered:
            return lowered[hint]
    return None


def _score_table(columns: List[str]) -> int:
    """How plausibly this table holds tool calls. Needs a tool name AND args."""
    if not _match_column(columns, "tool") or not _match_column(columns, "args"):
        return 0
    score = 2
    for role in ("role", "content", "session"):
        if _match_column(columns, role):
            score += 1
    return score


def _extract_thoughts(raw_args: Any) -> str:
    """Pull the scratchpad string out of a recorded tool-call argument blob."""
    if isinstance(raw_args, (bytes, bytearray)):
        raw_args = raw_args.decode("utf-8", errors="replace")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except (json.JSONDecodeError, ValueError):
            # A truncated argument still holds real reasoning; recover it when
            # the field name is visible rather than discard the trace.
            match = re.search(r'"thoughts"\s*:\s*"(.*)', raw_args, re.DOTALL)
            return match.group(1).strip() if match else ""
    if isinstance(raw_args, dict):
        for key in ("thoughts", "thought", "text", "content"):
            value = raw_args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def omp_session_import(
    db_path: str = "",
    limit: int = 200,
    min_chars: int = 40,
) -> Dict[str, Any]:
    """Extract scratchpad reasoning traces from an omp session database.

    Returns ``{"ok": True, "traces": [...], "scanned": N}`` on success. On an
    unrecognised schema returns ``ok=False`` with ``reason="unknown_schema"``
    and the tables it found, so a wrong guess is visible instead of silent.
    """
    if not db_path:
        located = omp_locate()
        candidates = located.get("databases") or []
        if not candidates:
            return {
                "ok": False, "reason": "no_database",
                "detail": located.get("reason", ""), "traces": [],
            }
        db_path = candidates[0]

    path = Path(db_path)
    if not path.exists():
        return {"ok": False, "reason": "no_database", "detail": str(path), "traces": []}

    try:
        # Read-only: this is someone's live session history, and a writer would
        # also take a lock on a database omp may be using right now.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"ok": False, "reason": "cannot_open", "detail": str(exc), "traces": []}

    try:
        tables = _tables(conn)
        scored = sorted(
            ((_score_table(_columns(conn, t)), t) for t in tables), reverse=True,
        )
        if not scored or scored[0][0] == 0:
            return {
                "ok": False,
                "reason": "unknown_schema",
                "detail": "no table carries both a tool name and tool arguments",
                "tables_found": tables,
                "traces": [],
            }

        _, table = scored[0]
        columns = _columns(conn, table)
        tool_col = _match_column(columns, "tool")
        args_col = _match_column(columns, "args")
        session_col = _match_column(columns, "session")

        selected = [tool_col, args_col] + ([session_col] if session_col else [])
        quoted = ", ".join(f'"{c}"' for c in selected)
        placeholders = ", ".join("?" for _ in THINK_TOOL_NAMES)
        query = (
            f'SELECT {quoted} FROM "{table}" '
            f'WHERE "{tool_col}" IN ({placeholders}) LIMIT ?'
        )
        rows = conn.execute(query, (*THINK_TOOL_NAMES, int(limit))).fetchall()
    except sqlite3.Error as exc:
        return {
            "ok": False, "reason": "query_failed", "detail": str(exc), "traces": [],
        }
    finally:
        conn.close()

    traces: List[Dict[str, Any]] = []
    for row in rows:
        thoughts = _extract_thoughts(row[1])
        if len(thoughts) < min_chars:
            continue
        traces.append({
            "tool": row[0],
            "session": row[2] if session_col and len(row) > 2 else "",
            "thoughts": thoughts,
            "source": str(path),
        })

    return {
        "ok": True,
        "table": table,
        "scanned": len(rows),
        "traces": traces,
        # Present and zero is a real answer; it means the sessions carry no
        # scratchpad calls, i.e. externalThinking was off when they were made.
        "reason": "" if traces else "no scratchpad tool calls in this database",
    }


def omp_tool_map(name: str = "") -> Dict[str, Any]:
    """Translate an omp tool name to its adk equivalent, or list the whole map."""
    if not name:
        return {"ok": True, "map": dict(OMP_TO_ADK_TOOLS)}
    mapped = OMP_TO_ADK_TOOLS.get(name.strip().lower())
    if not mapped:
        # Unmapped is reported, never guessed. A plausible-looking wrong mapping
        # teaches the wrong tool for the job.
        return {"ok": True, "name": name, "mapped": None, "reason": "no unambiguous adk equivalent"}
    return {"ok": True, "name": name, "mapped": mapped}


def register(registry) -> int:
    """Register the pack's tools. One bad tool never sinks the pack."""
    registered = 0
    for tool_name in _TOOL_NAMES:
        fn = globals().get(tool_name)
        if not callable(fn):
            logger.debug("omp_interop: missing tool %s", tool_name)
            continue
        try:
            registry.register(fn)
            registered += 1
        except Exception as exc:  # noqa: BLE001 - a pack must not sink an agent
            logger.debug("omp_interop: skip tool %s: %s", tool_name, exc)

    logger.info("omp-interop pack registered %d tools", registered)
    return registered
