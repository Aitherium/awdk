"""
AitherShell Personal Assistant
==============================

Local-first personal assistant features: mail, todos, calendar, reminders.
Data stored in ~/.aither/assistant/ as JSON files.
Syncs to AitherOS services when available.
"""

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict

DATA_DIR = Path.home() / ".aither" / "assistant"
TODOS_FILE = DATA_DIR / "todos.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
NOTES_FILE = DATA_DIR / "notes.json"


def _ensure():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load(path: Path) -> list:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(path: Path, data: list):
    _ensure()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ─── Todos ──────────────────────────────────────────────────────────

def todo_add(text: str, priority: str = "normal", due: str = "") -> dict:
    """Add a todo item."""
    todos = _load(TODOS_FILE)
    item = {
        "id": str(uuid.uuid4())[:8],
        "text": text,
        "priority": priority,  # low, normal, high, urgent
        "done": False,
        "due": due,
        "created": datetime.now().isoformat(),
        "completed_at": None,
    }
    todos.append(item)
    _save(TODOS_FILE, todos)
    return item


def todo_list(show_done: bool = False) -> List[dict]:
    """List todos."""
    todos = _load(TODOS_FILE)
    if not show_done:
        todos = [t for t in todos if not t.get("done")]
    # Sort: urgent first, then by created
    priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    todos.sort(key=lambda t: (priority_order.get(t.get("priority", "normal"), 2), t.get("created", "")))
    return todos


def todo_done(todo_id: str) -> Optional[dict]:
    """Mark a todo as done."""
    todos = _load(TODOS_FILE)
    for t in todos:
        if t["id"] == todo_id or t["text"].lower().startswith(todo_id.lower()):
            t["done"] = True
            t["completed_at"] = datetime.now().isoformat()
            _save(TODOS_FILE, todos)
            return t
    return None


def todo_remove(todo_id: str) -> bool:
    """Remove a todo."""
    todos = _load(TODOS_FILE)
    original = len(todos)
    todos = [t for t in todos if t["id"] != todo_id]
    if len(todos) < original:
        _save(TODOS_FILE, todos)
        return True
    return False


def todo_clear_done() -> int:
    """Clear completed todos."""
    todos = _load(TODOS_FILE)
    remaining = [t for t in todos if not t.get("done")]
    cleared = len(todos) - len(remaining)
    _save(TODOS_FILE, remaining)
    return cleared


# ─── Reminders ──────────────────────────────────────────────────────

def reminder_add(text: str, when: str) -> dict:
    """Add a reminder. `when` can be: '5m', '1h', '2d', 'tomorrow', or ISO datetime."""
    reminders = _load(REMINDERS_FILE)
    trigger_at = _parse_when(when)
    item = {
        "id": str(uuid.uuid4())[:8],
        "text": text,
        "trigger_at": trigger_at.isoformat(),
        "created": datetime.now().isoformat(),
        "fired": False,
    }
    reminders.append(item)
    _save(REMINDERS_FILE, reminders)
    return item


def reminder_list() -> List[dict]:
    """List pending reminders."""
    reminders = _load(REMINDERS_FILE)
    now = datetime.now().isoformat()
    return [r for r in reminders if not r.get("fired") and r.get("trigger_at", "") > now]


def reminder_check() -> List[dict]:
    """Check for due reminders (fires them)."""
    reminders = _load(REMINDERS_FILE)
    now = datetime.now()
    due = []
    for r in reminders:
        if not r.get("fired") and r.get("trigger_at"):
            trigger = datetime.fromisoformat(r["trigger_at"])
            if trigger <= now:
                r["fired"] = True
                due.append(r)
    if due:
        _save(REMINDERS_FILE, reminders)
    return due


def _parse_when(when: str) -> datetime:
    """Parse relative or absolute time strings."""
    when = when.strip().lower()
    now = datetime.now()
    if when == "tomorrow":
        return now + timedelta(days=1)
    if when == "tonight":
        return now.replace(hour=20, minute=0, second=0)
    if when.endswith("m"):
        return now + timedelta(minutes=int(when[:-1]))
    if when.endswith("h"):
        return now + timedelta(hours=int(when[:-1]))
    if when.endswith("d"):
        return now + timedelta(days=int(when[:-1]))
    # Try ISO parse
    return datetime.fromisoformat(when)


# ─── Notes ──────────────────────────────────────────────────────────

def note_add(text: str, tags: List[str] = None) -> dict:
    """Add a quick note."""
    notes = _load(NOTES_FILE)
    item = {
        "id": str(uuid.uuid4())[:8],
        "text": text,
        "tags": tags or [],
        "created": datetime.now().isoformat(),
    }
    notes.append(item)
    _save(NOTES_FILE, notes)
    return item


def note_list(tag: str = None, limit: int = 20) -> List[dict]:
    """List notes, optionally filtered by tag."""
    notes = _load(NOTES_FILE)
    if tag:
        notes = [n for n in notes if tag.lower() in [t.lower() for t in n.get("tags", [])]]
    return notes[-limit:]


def note_search(query: str) -> List[dict]:
    """Search notes by text."""
    notes = _load(NOTES_FILE)
    q = query.lower()
    return [n for n in notes if q in n.get("text", "").lower()]


# ─── Mail (delegates to Genesis/MCP) ───────────────────────────────

async def mail_check(client, limit: int = 10) -> List[dict]:
    """Check inbox via Genesis."""
    try:
        c = await client._get_client()
        resp = await c.get(f"{client.url}/mail/inbox", params={"limit": limit}, timeout=10.0)
        if resp.status_code == 200:
            return resp.json().get("messages", [])
    except Exception:
        pass
    return []


async def mail_send(client, to: str, subject: str, body: str) -> bool:
    """Send email via Genesis."""
    try:
        c = await client._get_client()
        resp = await c.post(f"{client.url}/mail/send", json={
            "to": to, "subject": subject, "body": body
        }, timeout=15.0)
        return resp.status_code == 200
    except Exception:
        return False


async def mail_read(client, email_id: str) -> Optional[dict]:
    """Read a specific email."""
    try:
        c = await client._get_client()
        resp = await c.get(f"{client.url}/mail/{email_id}", timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None
