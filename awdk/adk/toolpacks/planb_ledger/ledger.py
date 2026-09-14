"""Plan B Ledger — the LOCAL store (a plain JSON file the owner can read).

All ledger LOGIC lives in ``engine.py`` and is re-exported here, so the offline
zip, the server and the browser run byte-identical merge rules. This module adds
only the two things the server does differently: where the state lives and how
it is written.

State: ``~/.aither/planb/ledger.json`` (override with ``PLANB_DATA_DIR``) —
plain JSON on purpose. It is the owner's financial record; they must be able to
read, diff, back up and hand-edit it without this software.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from .engine import (  # noqa: F401 — re-exported as the module's public surface
    CATEGORIES,
    DEFAULT_STATE,
    add_entry,
    balance_cents,
    bill_paid_entry,
    bills_status,
    blank_state,
    create_checkpoint,
    find_bill,
    fmt_cents,
    get_checkpoint,
    merge_entries,
    normalize,
    parse_amount,
    reconcile,
    seed_demo,
    set_bills,
)

DATA_DIR = Path(os.environ.get("PLANB_DATA_DIR", str(Path.home() / ".aither" / "planb")))
STATE_FILE = DATA_DIR / "ledger.json"
SHEETS_DIR = DATA_DIR / "sheets"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return blank_state()
    try:
        return normalize(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return blank_state()


def save_state(state: dict) -> None:
    """Atomic write — a crash mid-save must never corrupt the ledger."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
