"""Plan B Ledger pack — regression tests.

Covers the loop the demo depends on: seed -> capture -> checkpoint sheet ->
reconcile with conflict detection, plus the Discord bot's construction
(intents, command registration, handlers) with the network login stubbed —
everything except bot.run() hitting Discord.
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest


@pytest.fixture()
def pack(tmp_path, monkeypatch):
    """Import the pack with an isolated data dir (env is read at import time)."""
    monkeypatch.setenv("PLANB_DATA_DIR", str(tmp_path / "planb-data"))
    for mod in [m for m in list(sys.modules) if "planb_ledger" in m]:
        del sys.modules[mod]
    from adk.toolpacks import planb_ledger  # noqa: F401
    ledger = importlib.import_module("adk.toolpacks.planb_ledger.ledger")
    brain = importlib.import_module("adk.toolpacks.planb_ledger.brain")
    sheet = importlib.import_module("adk.toolpacks.planb_ledger.sheet")
    bot = importlib.import_module("adk.toolpacks.planb_ledger.bot")
    return ledger, brain, sheet, bot


def _seeded(ledger):
    state = ledger.load_state()
    assert ledger.seed_demo(state)["seeded"] is True
    ledger.save_state(state)
    return ledger.load_state()


def test_seed_and_balance(pack):
    ledger, _, _, _ = pack
    state = _seeded(ledger)
    # 2450.00 - 1200 rent + 1850 pay - 87.34 - 42.10 = 2970.56
    assert ledger.balance_cents(state) == 297056
    assert ledger.fmt_cents(ledger.balance_cents(state)) == "$2,970.56"


def test_capture_fallback_bill_and_expense(pack):
    ledger, brain, _, _ = pack
    state = _seeded(ledger)
    got = brain.capture("paid the electric bill", state, endpoint="http://127.0.0.1:1")
    assert got["ok"] and got["proposal"]["bill_id"]
    assert got["proposal"]["amount_c"] == 14200
    got = brain.capture("spent 23.75 on lunch", state, endpoint="http://127.0.0.1:1")
    assert got["ok"] and got["proposal"]["amount_c"] == 2375
    assert got["proposal"]["type"] == "out" and got["proposal"]["category"] == "Food"
    got = brain.capture("got my paycheck 1850", state, endpoint="http://127.0.0.1:1")
    assert got["ok"] and got["proposal"]["type"] == "in"


def test_sheet_and_reconcile_conflict(pack):
    ledger, _, sheet, _ = pack
    state = _seeded(ledger)
    result = sheet.print_sheet(state)
    ledger.save_state(state)
    assert result["sheet_id"] == "PB-0001"
    html = (ledger.SHEETS_DIR / "PB-0001.html").read_text(encoding="utf-8")
    assert "PB-0001" in html and "Electric" in html

    # digital payment lands AFTER the print -> same bill ticked on paper = conflict
    state = ledger.load_state()
    internet = ledger.find_bill(state, "internet")
    ledger.add_entry(state, "Internet (bill)", internet["amount_c"], "out",
                     "Bills", bill_id=internet["id"])
    merged = ledger.reconcile(state, "PB-0001", ["electric", "internet"],
                              [{"desc": "coffee", "amount": "12"}])
    assert len(merged["added"]) == 2          # electric + coffee
    assert len(merged["conflicts"]) == 1      # internet, both faces
    assert "Internet" in merged["conflicts"][0]["bill"]
    # rent was already paid at print time -> ticking it again is a skip, not a dup
    merged2 = ledger.reconcile(state, "PB-0001", ["rent"], [])
    assert merged2["skipped"] and not merged2["added"]


def test_reconcile_unknown_sheet_fails_closed(pack):
    ledger, _, _, _ = pack
    state = _seeded(ledger)
    out = ledger.reconcile(state, "PB-9999", ["electric"], [])
    assert "error" in out and not state["entries"][-1]["source"] == "paper"


def test_bot_message_grammar(pack):
    _, _, _, bot = pack
    sheet_id, ticked, rows = bot.parse_reconcile_message(
        "pb-0003 paid: electric, internet; spent 34.50 groceries; got 100 refund")
    assert sheet_id == "PB-0003"
    assert ticked == ["electric", "internet"]
    assert rows[0]["amount"] == "34.50" and rows[0]["type"] == "out"
    assert rows[1]["type"] == "in"
    assert bot.parse_reconcile_message("nonsense") is None


def test_bot_constructs_and_registers_commands(pack, monkeypatch):
    """Everything main() does short of the network login: real intents, real
    command registration. bot.run stubbed; fails if any decorator blows up."""
    _, _, _, botmod = pack
    discord_commands = pytest.importorskip("discord.ext.commands")
    ran = {}

    def fake_run(self, token, *a, **kw):
        ran["token"] = token
        ran["commands"] = sorted(c.name for c in self.commands)

    monkeypatch.setattr(discord_commands.Bot, "run", fake_run)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token-not-real")
    botmod.main()
    assert ran["token"] == "test-token-not-real"
    assert ran["commands"] == ["bills", "brain", "help", "reconcile",
                               "seed", "sheet", "status"]


def test_bot_refuses_without_token(pack, monkeypatch):
    _, _, _, botmod = pack
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="[Tt]oken"):
        botmod.main()


def _mock_llamacpp(reply_content: str):
    """A real HTTP server speaking just enough OpenAI-compat for the brain."""
    import http.server
    import json as _json
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"data": [{"id": "bonsai-27b"}]}')

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = _json.dumps(
                {"choices": [{"message": {"content": reply_content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_bonsai_client_roundtrip(pack):
    """The llama.cpp client path over real HTTP: well-formed model reply wins."""
    ledger, brain, _, _ = pack
    state = _seeded(ledger)
    reply = ('{"kind": "expense", "desc": "Movie night", "amount": 18.50, '
             '"bill_name": null, "category": "Fun"}')
    srv, endpoint = _mock_llamacpp(reply)
    try:
        got = brain.capture("movies with sam", state, endpoint=endpoint)
        assert got["ok"] and got["brain"] == "bonsai-27b"
        assert got["proposal"]["amount_c"] == 1850
        assert got["proposal"]["category"] == "Fun"
        assert brain.brain_status(endpoint)["live"] is True
    finally:
        srv.shutdown()


def test_bonsai_garbage_falls_back(pack):
    """A live server answering garbage must degrade to the pattern brain."""
    ledger, brain, _, _ = pack
    state = _seeded(ledger)
    srv, endpoint = _mock_llamacpp("i am a helpful assistant and cannot do json")
    try:
        got = brain.capture("spent 9.99 on coffee", state, endpoint=endpoint)
        assert got["ok"] and got["brain"] == "fallback"
        assert got["proposal"]["amount_c"] == 999
    finally:
        srv.shutdown()


def test_bootstrap_model_pick_scales_to_ram(pack, monkeypatch):
    from adk.toolpacks.planb_ledger import bootstrap

    monkeypatch.setattr(bootstrap, "_ram_gb", lambda: 2.0)
    assert bootstrap.pick_model("auto") == "bonsai-1.7b"
    monkeypatch.setattr(bootstrap, "_ram_gb", lambda: 8.0)
    assert bootstrap.pick_model("auto") == "bonsai-27b"
    assert bootstrap.pick_model("bonsai-4b") == "bonsai-4b"
    with pytest.raises(SystemExit):
        bootstrap.pick_model("gpt-9")


def test_bootstrap_launches_from_binary_dir(pack, monkeypatch, tmp_path):
    """The cwd IS the fix: ggml resolves ggml-cuda.dll off the working dir, so
    launching from elsewhere silently drops to CPU (3.84 vs 21.30 t/s, measured)."""
    from adk.toolpacks.planb_ledger import bootstrap

    seen = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(bootstrap.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bootstrap, "endpoint_live", lambda *a, **k: True)
    monkeypatch.setattr(bootstrap, "_smoke", lambda port: True)
    binary = tmp_path / "llamacpp" / "llama-server.exe"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    model = tmp_path / "m.gguf"
    model.write_text("", encoding="utf-8")

    assert bootstrap.launch(binary, model, port=9999) is True
    assert seen["cwd"] == str(binary.parent)
    assert "-ngl" in seen["cmd"]  # offload every layer the build can take


def test_bootstrap_smoke_rejects_empty_completion(pack, monkeypatch):
    """A listening socket that emits no text is not a working brain."""
    from adk.toolpacks.planb_ledger import bootstrap

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def read(self):
            return json.dumps(self._p).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    empty = {"choices": [{"message": {"content": "", "reasoning_content": ""}}]}
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(empty))
    assert bootstrap._smoke(9999) is False

    # A reasoning model answering only in the think channel still counts.
    thinking = {"choices": [{"message": {"content": None,
                                         "reasoning_content": "thinking..."}}]}
    monkeypatch.setattr(bootstrap.urllib.request, "urlopen",
                        lambda *a, **k: FakeResp(thinking))
    assert bootstrap._smoke(9999) is True


def test_ledger_reexports_every_public_engine_function(pack):
    """`ledger.py` re-exports an EXPLICIT list, so a new engine function is
    invisible to bot/CLI/tools until someone remembers to add it — which is
    exactly what happened to `merge_entries`. Assert the list stays complete."""
    ledger, _, _, _ = pack
    engine = importlib.import_module("adk.toolpacks.planb_ledger.engine")
    public = {n for n in dir(engine)
              if not n.startswith("_") and callable(getattr(engine, n))
              and getattr(engine, n).__module__ == engine.__name__}
    missing = sorted(n for n in public if not hasattr(ledger, n))
    assert not missing, f"ledger.py does not re-export: {missing}"


def test_merge_is_idempotent_and_unions_by_id(pack):
    """Syncing twice must be a no-op — a sync unsafe to retry can't be automated."""
    ledger, _, _, _ = pack
    state = _seeded(ledger)
    incoming = [{"id": "remote01", "date": "2026-08-09", "desc": "Hardware store",
                 "amount_c": 4500, "type": "out", "category": "Home"}]
    first = ledger.merge_entries(state, incoming)
    assert len(first["added"]) == 1
    before = ledger.balance_cents(state)
    second = ledger.merge_entries(state, incoming)
    assert second["added"] == [] and len(second["skipped"]) == 1
    assert ledger.balance_cents(state) == before


def test_merge_holds_back_a_double_counted_bill(pack):
    """The same real payment entered on two faces must not be counted twice."""
    ledger, _, _, _ = pack
    state = _seeded(ledger)
    rent = ledger.find_bill(state, "Rent")          # seed already paid rent
    incoming = [{"id": "remote_rent", "date": "2026-08-09", "desc": "Rent (bill)",
                 "amount_c": rent["amount_c"], "type": "out", "category": "Bills",
                 "bill_id": rent["id"]}]
    before = ledger.balance_cents(state)
    out = ledger.merge_entries(state, incoming)
    assert out["added"] == [] and len(out["suspected_duplicates"]) == 1
    assert out["suspected_duplicates"][0]["bill_id"] == rent["id"]
    assert ledger.balance_cents(state) == before     # nothing double-counted
    # ...and force accepts it as a genuine second payment.
    forced = ledger.merge_entries(state, incoming, force=True)
    assert len(forced["added"]) == 1
    assert ledger.balance_cents(state) == before - rent["amount_c"]


def test_merge_rejects_malformed_rows_without_poisoning_the_ledger(pack):
    ledger, _, _, _ = pack
    state = _seeded(ledger)
    before = ledger.balance_cents(state)
    out = ledger.merge_entries(state, [
        {"date": "2026-08-09", "amount_c": 100, "type": "out"},          # no id
        {"id": "x1", "amount_c": "abc", "type": "out"},                   # bad amount
        {"id": "x2", "amount_c": 100, "type": "sideways"},                # bad type
    ])
    assert out["added"] == [] and len(out["skipped"]) == 3
    assert ledger.balance_cents(state) == before


def test_merge_keeps_the_journal_in_time_order(pack):
    ledger, _, _, _ = pack
    state = _seeded(ledger)
    ledger.merge_entries(state, [
        {"id": "old1", "ts": "2020-01-01T00:00:00", "date": "2020-01-01",
         "desc": "Ancient", "amount_c": 100, "type": "out"},
    ])
    stamps = [e.get("ts", "") for e in state["entries"]]
    assert stamps == sorted(stamps)
    assert state["entries"][0]["id"] == "old1"


def test_toolpack_registration(pack):
    from adk.toolpacks import planb_ledger

    class Reg:
        def __init__(self):
            self.fns = []

        def register(self, fn):
            self.fns.append(fn.__name__)

    reg = Reg()
    assert planb_ledger.register(reg) == 8
    assert {"planb_reconcile", "planb_sync"} <= set(reg.fns)


def test_sync_fails_closed_without_credentials(pack, monkeypatch):
    """Never an anonymous call: no URL or no token must return guidance."""
    from adk.toolpacks.planb_ledger import tools
    monkeypatch.delenv("PLANB_API_URL", raising=False)
    monkeypatch.delenv("PLANB_API_TOKEN", raising=False)
    assert "error" in tools.planb_sync()
    assert "fix" in tools.planb_sync(api_url="https://example.invalid")


def test_sync_offline_leaves_the_local_ledger_untouched(pack, monkeypatch):
    """Offline is the normal case for this product — it must not lose data."""
    ledger, _, _, _ = pack
    from adk.toolpacks.planb_ledger import tools
    state = _seeded(ledger)
    before = ledger.balance_cents(state)
    out = tools.planb_sync(api_url="http://127.0.0.1:1", token="t")
    assert "error" in out and "unchanged" in out["fix"]
    assert ledger.balance_cents(ledger.load_state()) == before
