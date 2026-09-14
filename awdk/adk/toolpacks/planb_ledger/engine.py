"""Plan B Ledger — the pure engine. No I/O, no storage, no imports beyond stdlib.

Every function here takes and returns a plain ``state`` dict, which is why the
SAME engine serves three very different stores:

  * the offline zip / adk pack  -> a local JSON file  (ledger.py)
  * the multi-tenant server     -> a SQL row per user (awkit-backend)
  * the browser                 -> localStorage / IndexedDB

A face may not fork this logic — the merge rules that make paper and digital
interchangeable have to be identical everywhere or two faces silently disagree.

What this does and does not buy you: every face applies the SAME rules, but each
still owns its own STORE (local JSON here, a per-tenant row on the server). So a
user on both the bot and the portal has two independent ledgers today. Making
them one is the sync slice, which reuses the checkpoint/reconcile merge below
instead of adding a second conflict model.

State shape::

    {"starting_balance_c": int,
     "bills":       [{id, name, amount_c, due_day}],
     "entries":     [{id, ts, date, desc, amount_c, type, category,
                      source, bill_id, sheet_id}],
     "checkpoints": [{id, seq, printed_at, balance_c, paid_bill_ids}],
     "next_seq":    int}

Money is integer cents. Entries are append-only: a correction is a new entry,
never a mutation — that is the checkbook discipline the product exists to keep.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

DEFAULT_STATE = {
    "starting_balance_c": 0,
    "bills": [],
    "entries": [],
    "checkpoints": [],
    "next_seq": 1,
}

CATEGORIES = ["Bills", "Food", "Auto", "Home", "Fun", "Income", "Other"]


def blank_state() -> dict:
    """A fresh, independent state dict."""
    return json.loads(json.dumps(DEFAULT_STATE))


def normalize(state: dict | None) -> dict:
    """Fill in any missing top-level keys — tolerates older stored states."""
    state = dict(state or {})
    for key, val in DEFAULT_STATE.items():
        state.setdefault(key, json.loads(json.dumps(val)))
    return state


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fmt_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def parse_amount(text: str) -> int:
    """'142', '142.50', '$1,200.00' -> cents. Raises ValueError on garbage."""
    clean = str(text).strip().replace("$", "").replace(",", "")
    value = round(float(clean) * 100)
    if value < 0:
        raise ValueError("amount must be positive; use type=out for spending")
    return int(value)


def balance_cents(state: dict) -> int:
    total = int(state.get("starting_balance_c", 0))
    for e in state["entries"]:
        total += e["amount_c"] if e["type"] == "in" else -e["amount_c"]
    return total


def add_entry(state: dict, desc: str, amount_c: int, etype: str = "out",
              category: str = "Other", date: str | None = None,
              source: str = "digital", bill_id: str | None = None,
              sheet_id: str | None = None) -> dict:
    if etype not in ("in", "out"):
        raise ValueError("type must be 'in' or 'out'")
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "date": date or _today(),
        "desc": str(desc).strip()[:120] or "(no description)",
        "amount_c": int(amount_c),
        "type": etype,
        "category": category if category in CATEGORIES else "Other",
        "source": source,
        "bill_id": bill_id,
        "sheet_id": sheet_id,
    }
    state["entries"].append(entry)
    return entry


def set_bills(state: dict, bills: list[dict]) -> list[dict]:
    """Replace the recurring-bills roster. bills: [{name, amount_c, due_day}]."""
    roster = []
    for b in bills:
        roster.append({
            "id": uuid.uuid4().hex[:8],
            "name": str(b["name"]).strip()[:60],
            "amount_c": int(b["amount_c"]),
            "due_day": max(1, min(31, int(b.get("due_day", 1)))),
        })
    state["bills"] = roster
    return roster


def find_bill(state: dict, name: str) -> dict | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for b in state["bills"]:
        if b["name"].lower() == needle:
            return b
    for b in state["bills"]:
        if needle in b["name"].lower():
            return b
    return None


def bill_paid_entry(state: dict, bill_id: str, month: str | None = None) -> dict | None:
    """Most recent payment entry for a bill in the given YYYY-MM (default: now)."""
    month = month or datetime.now().strftime("%Y-%m")
    for e in reversed(state["entries"]):
        if e.get("bill_id") == bill_id and e["date"].startswith(month):
            return e
    return None


def bills_status(state: dict) -> list[dict]:
    out = []
    for b in state["bills"]:
        paid = bill_paid_entry(state, b["id"])
        out.append({**b, "paid": bool(paid),
                    "paid_on": paid["date"] if paid else None,
                    "paid_source": paid["source"] if paid else None})
    return out


def create_checkpoint(state: dict) -> dict:
    seq = int(state.get("next_seq", 1))
    cp = {
        "id": f"PB-{seq:04d}",
        "seq": seq,
        "printed_at": _now_iso(),
        "balance_c": balance_cents(state),
        "paid_bill_ids": [b["id"] for b in bills_status(state) if b["paid"]],
    }
    state["checkpoints"].append(cp)
    state["next_seq"] = seq + 1
    return cp


def get_checkpoint(state: dict, sheet_id: str) -> dict | None:
    for cp in state["checkpoints"]:
        if cp["id"].lower() == str(sheet_id).strip().lower():
            return cp
    return None


def reconcile(state: dict, sheet_id: str, ticked_bills: list[str],
              rows: list[dict], force: bool = False) -> dict:
    """Merge a marked-up paper sheet back into the ledger.

    ticked_bills: bill names ticked on paper (newly-paid marks).
    rows: [{desc, amount, type?, category?, date?}] hand-written rows.
    Conflict: a bill ticked on paper that ALSO got a digital payment after the
    checkpoint was printed — same real-world payment recorded twice. Skipped
    unless force=True.

    This same merge is what makes offline node/browser sync work: paper is
    simply the hardest client (no clock, no network, human handwriting).
    """
    cp = get_checkpoint(state, sheet_id)
    if cp is None:
        known = [c["id"] for c in state["checkpoints"]]
        return {"error": f"unknown sheet '{sheet_id}'", "known_sheets": known}

    added, conflicts, skipped = [], [], []

    for name in ticked_bills:
        bill = find_bill(state, name)
        if bill is None:
            skipped.append({"bill": name, "reason": "no bill by that name"})
            continue
        if bill["id"] in cp["paid_bill_ids"]:
            skipped.append({"bill": bill["name"],
                            "reason": "already paid when the sheet was printed"})
            continue
        # Bill was UNPAID when the sheet was printed (else the branch above
        # skips), so any payment on record now landed after the checkpoint —
        # comparing timestamps here would miss a same-second race.
        digital = bill_paid_entry(state, bill["id"])
        if digital and not force:
            conflicts.append({
                "bill": bill["name"],
                "paper": "ticked paid on sheet " + cp["id"],
                "digital": f"already recorded {digital['date']} "
                           f"({fmt_cents(digital['amount_c'])}, {digital['source']})",
                "resolution": "skipped paper mark (same payment); "
                              "re-run with force=True if it was a second payment",
            })
            continue
        entry = add_entry(state, f"{bill['name']} (bill)", bill["amount_c"], "out",
                          "Bills", source="paper", bill_id=bill["id"], sheet_id=cp["id"])
        added.append(entry)

    for row in rows:
        try:
            amount_c = row["amount_c"] if "amount_c" in row else parse_amount(row["amount"])
        except (KeyError, ValueError) as exc:
            skipped.append({"row": row, "reason": f"bad amount: {exc}"})
            continue
        entry = add_entry(state, row.get("desc", "(paper entry)"), amount_c,
                          row.get("type", "out"), row.get("category", "Other"),
                          date=row.get("date"), source="paper", sheet_id=cp["id"])
        added.append(entry)

    return {"sheet": cp["id"], "added": added, "conflicts": conflicts,
            "skipped": skipped, "balance_c": balance_cents(state),
            "balance": fmt_cents(balance_cents(state))}


def merge_entries(state: dict, incoming: list[dict], force: bool = False) -> dict:
    """Union `incoming` entries into `state`. The sync half of the product.

    Entries are append-only and carry a uuid, so unioning by id is sound and
    IDEMPOTENT — syncing twice is a no-op, which matters because a sync that is
    unsafe to retry is a sync nobody can run automatically.

    The one real hazard is the SAME hazard paper has: one real-world payment
    entered on two faces becomes two entries with different ids, and a naive
    union double-counts it. So a bill payment arriving for a bill this ledger
    already shows paid in that month is reported as a suspected duplicate and
    held back, exactly like a paper tick that collides with a digital payment.
    `force=True` accepts it as a genuine second payment.

    Returns {added, skipped, suspected_duplicates, balance_c}.
    """
    known = {e["id"] for e in state["entries"]}
    added, skipped, dupes = [], [], []

    for raw in incoming:
        entry = dict(raw)
        eid = entry.get("id")
        if not eid:
            skipped.append({"entry": raw, "reason": "no id"})
            continue
        if eid in known:
            skipped.append({"id": eid, "reason": "already present"})
            continue
        if entry.get("type") not in ("in", "out"):
            skipped.append({"id": eid, "reason": "bad type"})
            continue
        try:
            entry["amount_c"] = int(entry["amount_c"])
        except (KeyError, TypeError, ValueError):
            skipped.append({"id": eid, "reason": "bad amount_c"})
            continue

        bill_id = entry.get("bill_id")
        if bill_id and not force:
            month = str(entry.get("date", ""))[:7]
            existing = bill_paid_entry(state, bill_id, month or None)
            if existing is not None:
                dupes.append({
                    "id": eid,
                    "bill_id": bill_id,
                    "desc": entry.get("desc", ""),
                    "collides_with": existing["id"],
                    "resolution": "held back — this bill already shows paid that "
                                  "month; re-run with force=True if it really was "
                                  "a second payment",
                })
                continue

        entry.setdefault("ts", entry.get("date", "") or _now_iso())
        entry.setdefault("source", "synced")
        entry.setdefault("category", "Other")
        entry.setdefault("sheet_id", None)
        state["entries"].append(entry)
        known.add(eid)
        added.append(entry)

    # Keep the journal in time order so every face reads the same history.
    state["entries"].sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
    return {"added": added, "skipped": skipped, "suspected_duplicates": dupes,
            "balance_c": balance_cents(state)}


def seed_demo(state: dict) -> dict:
    """Demo data: a realistic month in progress. Refuses a non-empty ledger."""
    if state["entries"] or state["bills"]:
        return {"seeded": False, "reason": "ledger already has data"}
    state["starting_balance_c"] = 245000
    set_bills(state, [
        {"name": "Rent", "amount_c": 120000, "due_day": 1},
        {"name": "Electric", "amount_c": 14200, "due_day": 12},
        {"name": "Internet", "amount_c": 8000, "due_day": 15},
        {"name": "Car insurance", "amount_c": 11800, "due_day": 20},
        {"name": "Phone", "amount_c": 6500, "due_day": 22},
    ])
    rent = find_bill(state, "Rent")
    add_entry(state, "Rent (bill)", 120000, "out", "Bills", bill_id=rent["id"])
    add_entry(state, "Paycheck", 185000, "in", "Income")
    add_entry(state, "Groceries — HEB", 8734, "out", "Food")
    add_entry(state, "Gas", 4210, "out", "Auto")
    return {"seeded": True, "balance": fmt_cents(balance_cents(state))}
