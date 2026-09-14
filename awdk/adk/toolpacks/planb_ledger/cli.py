"""Plan B Ledger — CLI face (no Discord needed).

python -m planb.cli status|seed|capture "..."|sheet|reconcile "PB-0001 paid: ..."
Also the demo driver: `python -m planb.cli demo` runs the whole loop.
"""
from __future__ import annotations

import sys
import webbrowser

from . import bot as botmod
from . import brain, ledger, sheet


def _status() -> None:
    print(botmod.status_text(ledger.load_state()))


def _seed() -> None:
    state = ledger.load_state()
    result = ledger.seed_demo(state)
    if result["seeded"]:
        ledger.save_state(state)
        print(f"Seeded demo ledger — balance {result['balance']}")
    else:
        print(f"Not seeded: {result['reason']}")


def _capture(text: str) -> None:
    state = ledger.load_state()
    print(botmod.handle_capture(text, state, None))


def _sheet(open_browser: bool = True) -> None:
    state = ledger.load_state()
    result = sheet.print_sheet(state)
    ledger.save_state(state)
    print(f"Sheet {result['sheet_id']} -> {result['path']} "
          f"(balance at print {result['balance_at_print']})")
    if open_browser:
        webbrowser.open(result["path"])


def _reconcile(text: str) -> None:
    parsed = botmod.parse_reconcile_message(text)
    if parsed is None:
        print('Usage: reconcile "PB-0001 paid: electric; spent 34.50 groceries"')
        return
    sheet_id, ticked, rows = parsed
    state = ledger.load_state()
    result = ledger.reconcile(state, sheet_id, ticked, rows)
    if "error" not in result:
        ledger.save_state(state)
    print(botmod.reconcile_report(result))


def _demo() -> None:
    print("=== PLAN B LEDGER — 60-second demo ===\n")
    print(f"[brain] {brain.brain_status()['brain']}\n")
    _seed()
    print("\n--- 1. digital face: status ---")
    _status()
    print("\n--- 2. natural-language capture ---")
    _capture("spent 23.75 on lunch")
    print("\n--- 3. print the Plan B sheet (checkpoint) ---")
    _sheet(open_browser=True)
    state = ledger.load_state()
    last_sheet = state["checkpoints"][-1]["id"]
    print("\n--- 4. meanwhile, a DIGITAL payment lands after the print ---")
    _capture("paid the internet bill")
    print("\n--- 5. tech comes back: reconcile the paper marks ---")
    print('    (paper says: electric AND internet paid, plus $12 coffee)')
    _reconcile(f"{last_sheet} paid: electric, internet; spent 12 coffee")
    print("\n    ^ note the CONFLICT: internet was paid on both faces — "
          "caught, not double-counted.")
    print("\n--- final state ---")
    _status()


def main() -> None:
    # Windows consoles default to cp1252, which cannot print the status glyphs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "status":
        _status()
    elif cmd == "seed":
        _seed()
    elif cmd == "capture" and len(args) > 1:
        _capture(" ".join(args[1:]))
    elif cmd == "sheet":
        _sheet()
    elif cmd == "reconcile" and len(args) > 1:
        _reconcile(" ".join(args[1:]))
    elif cmd == "demo":
        _demo()
    elif cmd == "brain":
        from . import bootstrap
        ok = bootstrap.bootstrap(args[1] if len(args) > 1 else "auto")
        sys.exit(0 if ok else 1)
    else:
        print("planb: status | seed | capture <text> | sheet | "
              "reconcile <sheet spec> | brain [model] | demo")


if __name__ == "__main__":
    main()
