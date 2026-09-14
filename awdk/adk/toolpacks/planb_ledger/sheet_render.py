"""Plan B Ledger — printable checkpoint sheet (PURE render, no I/O).

Renders the paper face: a structured worksheet the system prints and can read
back. The layout is designed for the MACHINE as much as the human — pre-printed
bill checkboxes, one-digit-per-cell amount boxes, category tick lists — so the
reconcile step is checkbox reads, not free handwriting OCR.

Every sheet carries a checkpoint ID (PB-NNNN). Reconcile diffs paper marks
against the ledger state AT PRINT TIME, which is what makes the merge sound.
"""
from __future__ import annotations

from . import engine as ledger

_ROWS = 10
_DIGITS = 6  # dollar digit cells; cents get 2 more

_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Georgia, 'Times New Roman', serif; color: #1a2233;
         background: #fff; padding: 28px 34px; max-width: 800px; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: baseline;
           border-bottom: 3px double #1a2233; padding-bottom: 10px; }
  h1 { font-size: 20px; letter-spacing: 1px; }
  .sheet-id { font-family: 'Courier New', monospace; font-size: 22px; font-weight: bold; }
  .meta { display: flex; justify-content: space-between; margin: 10px 0 18px;
          font-size: 13px; color: #444; }
  .balance { font-family: 'Courier New', monospace; font-size: 16px; font-weight: bold;
             color: #1a2233; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 2px;
       margin: 18px 0 8px; color: #555; }
  table { width: 100%; border-collapse: collapse; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; text-align: left;
       color: #666; border-bottom: 1px solid #999; padding: 3px 6px; }
  td { border-bottom: 1px solid #ccc; padding: 6px; font-size: 14px; vertical-align: middle; }
  .box { display: inline-block; width: 18px; height: 18px; border: 2px solid #1a2233;
         border-radius: 3px; vertical-align: middle; }
  .box.done { background: #1a2233; position: relative; }
  .box.done::after { content: '\\2713'; color: #fff; font-size: 14px;
                     position: absolute; left: 2px; top: -2px; }
  .digits { white-space: nowrap; }
  .digits span { display: inline-block; width: 22px; height: 26px;
                 border: 1px solid #888; border-left: none; text-align: center; }
  .digits span:first-child { border-left: 1px solid #888; }
  .digits .cents { background: #f2f2f2; }
  .dot { display: inline-block; width: 8px; text-align: center; font-weight: bold; }
  .cats { font-size: 10px; white-space: nowrap; }
  .cats i { font-style: normal; margin-right: 5px; }
  .cats .box { width: 11px; height: 11px; border-width: 1.5px; margin-right: 2px; }
  .io { font-size: 11px; white-space: nowrap; }
  .io .circle { display: inline-block; width: 16px; height: 16px; border: 1.5px solid #1a2233;
                border-radius: 50%; vertical-align: middle; margin: 0 2px; }
  .writein { border-bottom: 1px solid #999; display: inline-block; min-width: 160px;
             height: 18px; }
  footer { margin-top: 22px; border-top: 3px double #1a2233; padding-top: 8px;
           font-size: 11.5px; color: #555; line-height: 1.5; }
  footer b { font-family: 'Courier New', monospace; color: #1a2233; }
  @media print { body { padding: 0; } .noprint { display: none; } }
  .noprint { background: #fffbe6; border: 1px solid #e0c96e; border-radius: 6px;
             padding: 10px 14px; margin-bottom: 16px; font-family: system-ui, sans-serif;
             font-size: 13px; }
"""


def _digit_boxes() -> str:
    dollars = "".join("<span>&nbsp;</span>" for _ in range(_DIGITS))
    cents = "".join("<span class='cents'>&nbsp;</span>" for _ in range(2))
    return f"<span class='digits'>{dollars}</span><span class='dot'>.</span>" \
           f"<span class='digits'>{cents}</span>"


def _cat_boxes() -> str:
    cats = [c for c in ledger.CATEGORIES if c not in ("Bills", "Income")]
    return "<span class='cats'>" + "".join(
        f"<i><span class='box'></span>{c}</i>" for c in cats) + "</span>"


def render_sheet(state: dict, cp: dict) -> str:
    """Render the checkpoint sheet for a just-created checkpoint."""
    bills_rows = []
    for b in ledger.bills_status(state):
        pre_paid = b["id"] in cp["paid_bill_ids"]
        box = "<span class='box done'></span>" if pre_paid else "<span class='box'></span>"
        note = f"paid {b['paid_on']}" if pre_paid else f"due day {b['due_day']}"
        bills_rows.append(
            f"<tr><td style='width:34px'>{box}</td><td>{b['name']}</td>"
            f"<td style='text-align:right;font-family:monospace'>"
            f"{ledger.fmt_cents(b['amount_c'])}</td>"
            f"<td style='color:#666;font-size:12px'>{note}</td></tr>")

    txn_rows = []
    for _ in range(_ROWS):
        txn_rows.append(
            "<tr>"
            "<td style='width:70px;font-family:monospace;color:#aaa'>__ / __</td>"
            f"<td><span class='writein'></span></td>"
            f"<td>{_cat_boxes()}</td>"
            "<td class='io'><span class='circle'></span>IN "
            "<span class='circle'></span>OUT</td>"
            f"<td style='text-align:right'>{_digit_boxes()}</td>"
            "</tr>")

    printed = cp["printed_at"].replace("T", " ")
    return f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Plan B Sheet {cp['id']}</title><style>{_CSS}</style></head><body>
<div class='noprint'>&#128424;&#65039; Print this page (Ctrl+P), keep it with your checkbook.
When tech is back: tick marks &rarr; tell the bot
<b>!reconcile {cp['id']} paid: &lt;bills&gt;; spent &lt;amt&gt; &lt;what&gt;</b></div>
<header><h1>PLAN B LEDGER &mdash; CONTINUITY SHEET</h1>
<span class='sheet-id'>{cp['id']}</span></header>
<div class='meta'><span>Printed {printed}</span>
<span class='balance'>BALANCE AT PRINT: {ledger.fmt_cents(cp['balance_c'])}</span></div>
<h2>A &middot; Bills &mdash; tick the box when you pay</h2>
<table><tr><th></th><th>Bill</th><th style='text-align:right'>Amount</th><th>Status</th></tr>
{''.join(bills_rows)}</table>
<h2>B &middot; Transactions &mdash; one per row, one digit per box</h2>
<table><tr><th>Date</th><th>Description</th><th>Category</th><th>In/Out</th>
<th style='text-align:right'>Amount</th></tr>
{''.join(txn_rows)}</table>
<h2>C &middot; Running balance (pencil)</h2>
<table><tr><td style='height:26px'></td><td style='text-align:right'>{_digit_boxes()}</td></tr>
<tr><td style='height:26px'></td><td style='text-align:right'>{_digit_boxes()}</td></tr></table>
<footer>Sheet <b>{cp['id']}</b> is a checkpoint of your ledger at print time.
Work on paper while tech is down; every mark merges back when it returns &mdash;
conflicts (a bill paid on both faces) are caught, never double-counted.
One ledger, two faces. &nbsp;&bull;&nbsp; Plan B Ledger &mdash; local-first, no cloud.</footer>
</body></html>"""


