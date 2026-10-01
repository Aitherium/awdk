"""Session-focus records for daemon-run harnesses that have no hooks.

Claude Code sessions get a focus record from ``.claude/hooks/session-focus.py``
(its Stop hook parses the transcript). codex, gemini, aider and opencode have no
hook surface, so the harness session writes the SAME record shape from its own
normalized events, and the daemon reads the newest one back at spawn so the next
session starts knowing where the last one stopped.

Record: ``<root>/<project-key>/<session_id>.json`` with ``first_ask``,
``last_ask``, ``turns``, ``files``, ``report``, ``next``, ``session_id``,
``transcript``, ``project``, ``updated``. ``root`` is ``$AITHER_FOCUS_DIR`` or
``~/.aither/focus``, read at call time. Everything here is fail-open: a focus
record is a convenience and must never break a turn.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

MAX_ASK = 400
MAX_REPORT = 900
MAX_FILES = 25
#: How old a record may be and still be offered to a new session.
RECENT_SECONDS = 24 * 3600

# Kept in step with .claude/hooks/session-focus.py (that script is not importable
# from the installed package, so the few lines it shares are repeated here).
_SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9-]{10,}|[ps]k_live_[A-Za-z0-9]{10,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|(?i:password|passwd|pwd|secret|aws_secret_access_key)\s*[=:]\s*[^\s,;]+)"
)
_NEXT = re.compile(r"(?im)^[ \t>*_-]*next[ *_]*:[ *_]*(\S.*)$|^#+[ \t]*next[ \t]*\n+[ \t-]*(\S.*)$")
_CONTEXT_FRAME = re.compile(r"<session-context>.*?</session-context>", re.S)


def focus_root() -> Path:
    """The records root: ``$AITHER_FOCUS_DIR`` or ``~/.aither/focus``."""
    return Path(os.environ.get("AITHER_FOCUS_DIR") or Path.home() / ".aither" / "focus")


def project_key(project_dir: str) -> str:
    """Directory name for a project; identical to the Claude Code hook's key."""
    return re.sub(r"[^A-Za-z0-9]+", "-", project_dir or "unknown").strip("-").lower() or "unknown"


def clean(text: str, limit: int) -> str:
    """Collapse whitespace, drop a framed session context, redact secrets, cap length."""
    text = _CONTEXT_FRAME.sub(" ", text or "")
    text = _SECRET.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def next_step(report: str) -> str:
    """The last ``NEXT: <step>`` line or ``## Next`` heading body in ``report``."""
    hits = _NEXT.findall(report or "")
    return clean(hits[-1][0] or hits[-1][1], MAX_ASK) if hits else ""


def write_record(
    *,
    session_id: str,
    project: str,
    asks: list[str],
    files: list[str],
    report: str,
    transcript: str = "",
    root: Optional[Path] = None,
) -> Optional[Path]:
    """Write (atomically replace) one focus record. Returns its path, or None.

    Args:
        session_id: The session the record belongs to (file stem).
        project: The session's working directory; keys the record.
        asks: Every human prompt of the session, already cleaned, in order.
        files: Paths the session edited, in first-seen order.
        report: The latest assistant prose (uncleaned; NEXT is read from it).
        transcript: Path to the session's event transcript.
        root: Records root; defaults to :func:`focus_root`.
    """
    if not session_id or not (asks or files or report):
        return None
    rec: dict[str, Any] = {
        "first_ask": asks[0] if asks else "",
        "last_ask": asks[-1] if len(asks) > 1 else "",
        "turns": len(asks),
        "files": list(files)[-MAX_FILES:],
        "report": clean(report, MAX_REPORT),
        "next": next_step(report),
        "session_id": session_id,
        "transcript": transcript,
        "project": project,
        "updated": int(time.time()),
    }
    out = (root or focus_root()) / project_key(project) / f"{session_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    return out


def latest_record(
    project: str,
    *,
    max_age: float = RECENT_SECONDS,
    exclude: str = "",
    root: Optional[Path] = None,
) -> dict[str, Any]:
    """The newest record for ``project`` updated within ``max_age`` seconds, or {}."""
    d = (root or focus_root()) / project_key(project)
    if not d.is_dir():
        return {}
    cutoff = time.time() - max_age
    best: dict[str, Any] = {}
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("session_id") == exclude:
            continue
        updated = float(rec.get("updated") or 0)
        if updated >= cutoff and updated > float(best.get("updated") or 0):
            best = rec
    return best


def resume_line(project: str, *, root: Optional[Path] = None) -> str:
    """One line naming the previous session's next step, or "" when there is none.

    ``AITHER_FOCUS_INJECT=0`` disables it.
    """
    if os.environ.get("AITHER_FOCUS_INJECT", "1") == "0":
        return ""
    try:
        nxt = str(latest_record(project, root=root).get("next") or "").strip()
    except OSError:
        return ""
    return f"Previous session in this project stopped at: {nxt}" if nxt else ""
