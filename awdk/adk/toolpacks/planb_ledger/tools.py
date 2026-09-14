"""Plan B Ledger pack — planb_* agent tools.

Same doctrine as every pack: fail-soft dict-returners, never raise. The core
lives in ledger.py/sheet.py/brain.py (relative imports, so the identical files
ship standalone in the consumer zip as package `planb`).
"""
from __future__ import annotations

import logging

from . import brain as _brain
from . import ledger as _ledger
from . import sheet as _sheet

logger = logging.getLogger("planb_ledger_pack")


def planb_status() -> dict:
    """Balance, bills roster with paid-state, and recent entries."""
    try:
        state = _ledger.load_state()
        return {
            "balance": _ledger.fmt_cents(_ledger.balance_cents(state)),
            "balance_c": _ledger.balance_cents(state),
            "bills": _ledger.bills_status(state),
            "recent_entries": state["entries"][-10:],
            "sheets": [c["id"] for c in state["checkpoints"]],
            "brain": _brain.brain_status(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def planb_capture(text: str, confirm: bool = True) -> dict:
    """Parse natural language ('paid electric', 'spent 34.50 on groceries')
    into a ledger entry via local bonsai-27b (llama.cpp) with deterministic
    fallback. confirm=True records it; confirm=False returns the proposal only."""
    try:
        state = _ledger.load_state()
        got = _brain.capture(text, state)
        if not got.get("ok") or got.get("needs_amount"):
            return got
        if not confirm:
            return got
        p = got["proposal"]
        entry = _ledger.add_entry(state, p["desc"], p["amount_c"], p["type"],
                                  p["category"], bill_id=p["bill_id"])
        _ledger.save_state(state)
        return {"ok": True, "recorded": entry, "brain": got["brain"],
                "balance": _ledger.fmt_cents(_ledger.balance_cents(state))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def planb_add_entry(desc: str, amount: str, type: str = "out",
                    category: str = "Other", date: str = "") -> dict:
    """Record an exact entry. amount like '34.50'; type 'in'|'out'."""
    try:
        state = _ledger.load_state()
        entry = _ledger.add_entry(state, desc, _ledger.parse_amount(amount),
                                  type, category, date=date or None)
        _ledger.save_state(state)
        return {"ok": True, "recorded": entry,
                "balance": _ledger.fmt_cents(_ledger.balance_cents(state))}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def planb_set_bills(bills: list) -> dict:
    """Replace the recurring-bills roster. bills: [{name, amount, due_day}]."""
    try:
        state = _ledger.load_state()
        roster = _ledger.set_bills(state, [
            {"name": b["name"], "amount_c": _ledger.parse_amount(str(b["amount"])),
             "due_day": b.get("due_day", 1)} for b in bills])
        _ledger.save_state(state)
        return {"ok": True, "bills": roster}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def planb_print_sheet() -> dict:
    """Create a checkpoint and render the printable Plan B continuity sheet.
    Returns {sheet_id, path, balance_at_print}. Pure-local HTML file."""
    try:
        state = _ledger.load_state()
        result = _sheet.print_sheet(state)
        _ledger.save_state(state)
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def planb_reconcile(sheet_id: str, ticked_bills: list | None = None,
                    rows: list | None = None, force: bool = False) -> dict:
    """Merge paper marks from a printed sheet back into the ledger.
    ticked_bills: bill names ticked on paper. rows: [{desc, amount, type?,
    category?, date?}]. Conflicts (paid on both faces) are caught and skipped
    unless force=True."""
    try:
        state = _ledger.load_state()
        result = _ledger.reconcile(state, sheet_id, ticked_bills or [],
                                   rows or [], force=force)
        if "error" not in result:
            _ledger.save_state(state)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def planb_sync(api_url: str = "", token: str = "", force: bool = False) -> dict:
    """Two-way sync between this local ledger and the tenant ledger on the server.

    Pushes local entries up and merges whatever comes back down, using the SAME
    union-by-id merge on both ends — so it is idempotent and safe to retry, and
    a bill already paid on the other face is reported as a suspected duplicate
    rather than counted twice.

    Fail-closed: no URL or no token returns an error dict, never an anonymous
    call. Reads ``PLANB_API_URL`` / ``PLANB_API_TOKEN`` when not passed.
    """
    import os

    url = (api_url or os.environ.get("PLANB_API_URL", "")).strip().rstrip("/")
    tok = (token or os.environ.get("PLANB_API_TOKEN", "")).strip()
    if not url:
        return {"error": "no API URL",
                "fix": "pass api_url=... or set PLANB_API_URL "
                       "(e.g. https://your-workspace.example.com)"}
    if not tok:
        return {"error": "no API token",
                "fix": "pass token=... or set PLANB_API_TOKEN with a session "
                       "bearer for that workspace"}
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed", "fix": "pip install httpx"}

    try:
        state = _ledger.load_state()
        resp = httpx.post(
            f"{url}/api/planb/sync",
            json={"entries": state["entries"], "force": bool(force)},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=60.0,
        )
        if resp.status_code == 401:
            return {"error": "server rejected the token (401)",
                    "fix": "re-mint a session bearer for that workspace"}
        if resp.status_code != 200:
            return {"error": f"sync failed: HTTP {resp.status_code}",
                    "detail": resp.text[:300]}
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — offline is normal for this product
        return {"error": f"could not reach {url}: {exc}",
                "fix": "the local ledger is unchanged; sync again when online"}

    pulled = _ledger.merge_entries(state, body.get("entries", []), force=force)
    _ledger.save_state(state)
    pushed = body.get("result", {})
    return {
        "ok": True,
        "pushed": len(pushed.get("added", [])),
        "pushed_duplicates": pushed.get("suspected_duplicates", []),
        "pulled": len(pulled["added"]),
        "pulled_duplicates": pulled["suspected_duplicates"],
        "balance": _ledger.fmt_cents(_ledger.balance_cents(state)),
    }


def planb_seed_demo() -> dict:
    """Load demo data (refuses if the ledger already has entries or bills)."""
    try:
        state = _ledger.load_state()
        result = _ledger.seed_demo(state)
        if result["seeded"]:
            _ledger.save_state(state)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
