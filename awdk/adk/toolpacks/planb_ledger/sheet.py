"""Plan B Ledger — sheet printing (I/O over the pure renderer)."""
from __future__ import annotations

from . import ledger
from .sheet_render import render_sheet  # noqa: F401 — re-exported


def print_sheet(state: dict) -> dict:
    """Create a checkpoint and write its printable sheet. Returns {sheet_id, path}."""
    cp = ledger.create_checkpoint(state)
    html = render_sheet(state, cp)
    ledger.SHEETS_DIR.mkdir(parents=True, exist_ok=True)
    path = ledger.SHEETS_DIR / f"{cp['id']}.html"
    path.write_text(html, encoding="utf-8")
    return {"sheet_id": cp["id"], "path": str(path),
            "balance_at_print": ledger.fmt_cents(cp["balance_c"])}
