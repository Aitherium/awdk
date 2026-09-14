"""Plan B Ledger — Discord face.

The chat interface: talk to your ledger in plain language, get the printable
Plan B sheet as a file, reconcile paper marks with one message. Runs entirely
on the owner's machine — the only network is Discord itself and (optionally)
localhost llama.cpp.

Token comes from ~/.aither/planb/config.json (written by the install wizard)
or the DISCORD_BOT_TOKEN env var. It is never logged.

Run:  python -m planb.bot        (standalone zip install)
      python -m adk.toolpacks.planb_ledger.bot   (monorepo)
"""
from __future__ import annotations

import io
import json
import re

from . import brain, ledger, sheet

CONFIG_FILE = ledger.DATA_DIR / "config.json"

HELP = """**Plan B Ledger** — one ledger, two faces. Just tell me what happened:
> `paid the electric bill` · `spent 34.50 on groceries` · `got my paycheck 1850`

**Commands**
`!status` — balance, bills, recent entries
`!sheet` — print a Plan B continuity sheet (checkpoint) as an HTML file
`!reconcile PB-0001 paid: electric, internet; spent 34.50 groceries` — merge paper marks
`!bills` — recurring bills roster
`!seed` — load demo data (only on an empty ledger)
`!brain` — which brain is answering (bonsai-27b local, or pattern fallback)"""


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _entry_line(e: dict) -> str:
    arrow = "🟢 +" if e["type"] == "in" else "🔴 −"
    face = "✍️" if e["source"] == "paper" else "⌨️"
    return f"{face} {e['date']}  {arrow}{ledger.fmt_cents(e['amount_c'])}  " \
           f"{e['desc']}  ·  {e['category']}"


def status_text(state: dict) -> str:
    lines = [f"**Balance: {ledger.fmt_cents(ledger.balance_cents(state))}**", "", "**Bills**"]
    for b in ledger.bills_status(state):
        mark = "☑" if b["paid"] else "☐"
        paid = f" — paid {b['paid_on']} ({b['paid_source']})" if b["paid"] else \
               f" — due day {b['due_day']}"
        lines.append(f"{mark} {b['name']}  {ledger.fmt_cents(b['amount_c'])}{paid}")
    recent = state["entries"][-6:]
    if recent:
        lines += ["", "**Recent**"] + [_entry_line(e) for e in reversed(recent)]
    return "\n".join(lines)


def parse_reconcile_message(text: str) -> tuple[str, list[str], list[dict]] | None:
    """'PB-0001 paid: a, b; spent 34.50 groceries; got 100 refund' -> parts."""
    m = re.match(r"\s*(PB-\d+)\s*(.*)", text.strip(), re.IGNORECASE)
    if not m:
        return None
    sheet_id, rest = m.group(1).upper(), m.group(2)
    ticked: list[str] = []
    rows: list[dict] = []
    for seg in [s.strip() for s in rest.split(";") if s.strip()]:
        low = seg.lower()
        if low.startswith("paid:") or low.startswith("paid "):
            names = seg.split(":", 1)[1] if ":" in seg else seg[5:]
            ticked += [n.strip() for n in names.split(",") if n.strip()]
        elif low.startswith(("spent", "got", "received", "income")):
            am = re.search(r"\$?(\d[\d,]*(?:\.\d{1,2})?)", seg)
            if not am:
                continue
            desc = seg.replace(am.group(0), "")
            desc = re.sub(r"^(spent|got|received|income)\b", "", desc, flags=re.I).strip()
            rows.append({"desc": desc or "(paper entry)", "amount": am.group(1),
                         "type": "in" if low.startswith(("got", "received", "income"))
                         else "out"})
    return sheet_id, ticked, rows


def reconcile_report(result: dict) -> str:
    if "error" in result:
        return f"❌ {result['error']} (known: {', '.join(result.get('known_sheets', []))})"
    lines = [f"**Reconciled sheet {result['sheet']}** — new balance {result['balance']}"]
    for e in result["added"]:
        lines.append(f"  ➕ {_entry_line(e)}")
    for c in result["conflicts"]:
        lines.append(f"  ⚠️ **Conflict — {c['bill']}**: paper says {c['paper']}, "
                     f"but {c['digital']} → {c['resolution']}")
    for s in result["skipped"]:
        lines.append(f"  ⏭️ skipped {s.get('bill') or s.get('row')}: {s['reason']}")
    if not (result["added"] or result["conflicts"] or result["skipped"]):
        lines.append("  (nothing to merge)")
    return "\n".join(lines)


def handle_capture(text: str, state: dict, endpoint: str | None) -> str:
    got = brain.capture(text, state, endpoint=endpoint)
    if not got.get("ok"):
        return f"❓ {got.get('error', 'could not parse that')} — try `!help`"
    p = got["proposal"]
    if got.get("needs_amount"):
        return f"❓ How much was “{p['desc']}”? Say it with the amount, " \
               f"e.g. `{text.strip()} 25.00`"
    entry = ledger.add_entry(state, p["desc"], p["amount_c"], p["type"],
                             p["category"], bill_id=p["bill_id"])
    ledger.save_state(state)
    bal = ledger.fmt_cents(ledger.balance_cents(state))
    return f"✅ Recorded {_entry_line(entry)}\n**Balance: {bal}**  ·  " \
           f"_brain: {got['brain']}_"


def main() -> None:
    try:
        import discord
        from discord.ext import commands
    except ImportError:
        raise SystemExit("discord.py missing — run the installer (install.ps1 / install.sh) "
                         "or: pip install discord.py httpx")

    import os
    cfg = load_config()
    token = cfg.get("discord_token") or os.environ.get("DISCORD_BOT_TOKEN", "")
    endpoint = cfg.get("llm_endpoint") or brain.DEFAULT_ENDPOINT
    if not token:
        raise SystemExit("No Discord token. Run the installer, or set DISCORD_BOT_TOKEN.")

    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    @bot.event
    async def on_ready() -> None:
        print(f"[planb] logged in as {bot.user} — brain: "
              f"{brain.brain_status(endpoint)['brain']}")

    @bot.command(name="help")
    async def _help(ctx: "commands.Context") -> None:
        await ctx.send(HELP)

    @bot.command(name="status")
    async def _status(ctx: "commands.Context") -> None:
        await ctx.send(status_text(ledger.load_state()))

    @bot.command(name="bills")
    async def _bills(ctx: "commands.Context") -> None:
        state = ledger.load_state()
        rows = [f"• {b['name']} — {ledger.fmt_cents(b['amount_c'])} (due day {b['due_day']})"
                for b in state["bills"]] or ["(no bills yet — `!seed` for demo data)"]
        await ctx.send("**Recurring bills**\n" + "\n".join(rows))

    @bot.command(name="seed")
    async def _seed(ctx: "commands.Context") -> None:
        state = ledger.load_state()
        result = ledger.seed_demo(state)
        if result["seeded"]:
            ledger.save_state(state)
            await ctx.send(f"🌱 Demo ledger loaded — balance {result['balance']}. `!status`")
        else:
            await ctx.send(f"⏭️ Not seeded: {result['reason']}")

    @bot.command(name="brain")
    async def _brain(ctx: "commands.Context") -> None:
        st = brain.brain_status(endpoint)
        live = "🟢 live" if st["live"] else "🟡 offline — deterministic fallback active"
        await ctx.send(f"Brain: **{st['brain']}** ({st['endpoint']}) {live}")

    @bot.command(name="sheet")
    async def _sheet(ctx: "commands.Context") -> None:
        state = ledger.load_state()
        result = sheet.print_sheet(state)
        ledger.save_state(state)
        html = (ledger.SHEETS_DIR / f"{result['sheet_id']}.html").read_bytes()
        await ctx.send(
            f"🖨️ **Plan B sheet {result['sheet_id']}** — balance at print "
            f"{result['balance_at_print']}. Open the file, print it, keep it with "
            f"the checkbook. When tech is back: "
            f"`!reconcile {result['sheet_id']} paid: <bills>; spent <amt> <what>`",
            file=discord.File(io.BytesIO(html), filename=f"{result['sheet_id']}.html"))

    @bot.command(name="reconcile")
    async def _reconcile(ctx: "commands.Context", *, text: str = "") -> None:
        parsed = parse_reconcile_message(text)
        if parsed is None:
            await ctx.send("Usage: `!reconcile PB-0001 paid: electric, internet; "
                           "spent 34.50 groceries; got 100 refund`")
            return
        sheet_id, ticked, rows = parsed
        state = ledger.load_state()
        result = ledger.reconcile(state, sheet_id, ticked, rows)
        if "error" not in result:
            ledger.save_state(state)
        await ctx.send(reconcile_report(result))

    @bot.event
    async def on_message(message: "discord.Message") -> None:
        if message.author.bot:
            return
        content = (message.content or "").strip()
        if content.startswith("!"):
            await bot.process_commands(message)
            return
        is_dm = message.guild is None
        mentioned = bot.user is not None and bot.user in message.mentions
        in_planb = getattr(message.channel, "name", "") in ("planb", "plan-b", "ledger")
        if content and (is_dm or mentioned or in_planb):
            clean = re.sub(r"<@!?\d+>", "", content).strip()
            if clean:
                state = ledger.load_state()
                await message.channel.send(handle_capture(clean, state, endpoint))

    bot.run(token)


if __name__ == "__main__":
    main()
