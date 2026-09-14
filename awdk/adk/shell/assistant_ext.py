"""
AitherShell Extended Personal Assistant
========================================
Bookmarks, journal, habits, timer, standup, weather.
JSON files in ~/.aither/assistant/. No database.
"""

import json
import uuid
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

DATA_DIR = Path.home() / ".aither" / "assistant"
BOOKMARKS_FILE = DATA_DIR / "bookmarks.json"
JOURNAL_FILE = DATA_DIR / "journal.json"
HABITS_FILE = DATA_DIR / "habits.json"
TIMER_FILE = DATA_DIR / "timer.json"


def _load(path: Path) -> list:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(path: Path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ─── Bookmarks ──────────────────────────────────────────────────────

def bookmark_add(url: str, tags: List[str] = None) -> dict:
    bms = _load(BOOKMARKS_FILE)
    item = {"id": str(uuid.uuid4())[:8], "url": url, "tags": tags or [], "created": datetime.now().isoformat()}
    bms.append(item)
    _save(BOOKMARKS_FILE, bms)
    return item


def bookmark_list(tag: str = None) -> List[dict]:
    bms = _load(BOOKMARKS_FILE)
    if tag:
        bms = [b for b in bms if tag.lower() in [t.lower() for t in b.get("tags", [])]]
    return bms


def bookmark_search(query: str) -> List[dict]:
    bms = _load(BOOKMARKS_FILE)
    q = query.lower()
    return [b for b in bms if q in b.get("url", "").lower() or any(q in t.lower() for t in b.get("tags", []))]


# ─── Journal ────────────────────────────────────────────────────────

def journal_add(text: str, mood: str = None) -> dict:
    entries = _load(JOURNAL_FILE)
    item = {"id": str(uuid.uuid4())[:8], "text": text, "mood": mood, "created": datetime.now().isoformat()}
    entries.append(item)
    _save(JOURNAL_FILE, entries)
    return item


def journal_today() -> List[dict]:
    entries = _load(JOURNAL_FILE)
    today = date.today().isoformat()
    return [e for e in entries if e.get("created", "").startswith(today)]


def journal_list(days: int = 7) -> List[dict]:
    entries = _load(JOURNAL_FILE)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    return [e for e in entries if e.get("created", "") >= cutoff]


# ─── Habits ─────────────────────────────────────────────────────────

def habit_add(name: str, frequency: str = "daily") -> dict:
    habits = _load(HABITS_FILE)
    item = {
        "name": name, "frequency": frequency,
        "created": datetime.now().isoformat(),
        "checks": [], "last_checked": None,
    }
    habits.append(item)
    _save(HABITS_FILE, habits)
    return item


def habit_check(name: str) -> Optional[dict]:
    habits = _load(HABITS_FILE)
    for h in habits:
        if h["name"].lower() == name.lower():
            today = date.today().isoformat()
            if today not in h.get("checks", []):
                h.setdefault("checks", []).append(today)
            h["last_checked"] = datetime.now().isoformat()
            _save(HABITS_FILE, habits)
            return h
    return None


def habit_list() -> List[dict]:
    return _load(HABITS_FILE)


def habit_streak(name: str) -> int:
    habits = _load(HABITS_FILE)
    for h in habits:
        if h["name"].lower() == name.lower():
            checks = sorted(h.get("checks", []), reverse=True)
            if not checks:
                return 0
            streak = 0
            expected = date.today()
            for check_date_str in checks:
                check_date = date.fromisoformat(check_date_str)
                if check_date == expected:
                    streak += 1
                    expected -= timedelta(days=1)
                elif check_date < expected:
                    break
            return streak
    return 0


# ─── Timer / Pomodoro ───────────────────────────────────────────────

def timer_start(minutes: int, label: str = "focus") -> dict:
    timer = {
        "label": label, "minutes": minutes,
        "started": datetime.now().isoformat(),
        "ends_at": (datetime.now() + timedelta(minutes=minutes)).isoformat(),
    }
    _save(TIMER_FILE, timer)
    return timer


def timer_status() -> Optional[dict]:
    if not TIMER_FILE.exists():
        return None
    with open(TIMER_FILE, "r") as f:
        timer = json.load(f)
    if not timer or not timer.get("ends_at"):
        return None
    ends = datetime.fromisoformat(timer["ends_at"])
    now = datetime.now()
    if now >= ends:
        return {"label": timer["label"], "remaining": "DONE!", "expired": True}
    remaining = ends - now
    mins = int(remaining.total_seconds() // 60)
    secs = int(remaining.total_seconds() % 60)
    return {"label": timer["label"], "remaining": f"{mins}m {secs}s", "expired": False}


def timer_cancel():
    if TIMER_FILE.exists():
        TIMER_FILE.unlink()


# ─── Standup Generator ──────────────────────────────────────────────

def generate_standup() -> str:
    """Generate a standup from yesterday's completed todos + git log."""
    lines = ["STANDUP", "=" * 40, ""]

    # Yesterday's completed todos
    from adk.shell.assistant import todo_list
    all_todos = todo_list(show_done=True)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    completed = [t for t in all_todos if (t.get("completed_at") or "").startswith(yesterday)]
    lines.append("Yesterday:")
    if completed:
        for t in completed:
            lines.append(f"  - {t['text']}")
    else:
        lines.append("  (no completed todos)")

    # Git commits since yesterday
    try:
        r = subprocess.run(
            ["git", "log", "--since=yesterday", "--oneline", "--author-date-order"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            lines.append("")
            lines.append("Commits:")
            for line in r.stdout.strip().split("\n")[:10]:
                lines.append(f"  - {line}")
    except Exception:
        pass

    # Today's pending todos
    lines.append("")
    lines.append("Today:")
    pending = [t for t in todo_list() if not t.get("done")]
    if pending:
        for t in pending[:5]:
            pri = {"urgent": "!!!", "high": "!!", "normal": "", "low": "~"}.get(t.get("priority", ""), "")
            lines.append(f"  - {pri} {t['text']}")
    else:
        lines.append("  (no pending todos)")

    lines.append("")
    lines.append("Blockers:")
    lines.append("  (none listed)")

    return "\n".join(lines)


# ─── Weather ────────────────────────────────────────────────────────

async def get_weather(location: str = None) -> str:
    """Get weather from wttr.in."""
    import httpx
    loc = location or ""
    url = f"https://wttr.in/{loc}?format=3"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "aithershell/0.1"})
        return r.text.strip()
