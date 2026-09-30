"""``adk home report``: the device-signed data-boundary report (adk.home.report).

The report must (1) say where the model runs from the config, not a guess; (2) carry
the receipts verdict exactly as ``adk.receipts.check`` gives it, including TAMPERED;
(3) count egress events from the audit log inside the window only; (4) never copy a
receipt's argument/result preview; (5) verify 0 when untouched, 1 when edited, and 2
when unsigned or signed by a key this machine does not trust.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")

from adk import receipts  # noqa: E402
from adk.home import cli as home_cli  # noqa: E402
from adk.home import config as hc  # noqa: E402
from adk.home import models  # noqa: E402
from adk.home import report as hr  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
SECRET_ARG = "call-mom-about-the-surprise-party"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "agent-home"
    monkeypatch.setenv(hc.HOME_ENV, str(root))
    monkeypatch.setenv("AITHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AITHER_AIR_GAP_CONFIG", str(tmp_path / "data" / "air_gap.yaml"))
    monkeypatch.setenv("AITHER_RECEIPTS_PATH", str(root / "actions.jsonl"))
    monkeypatch.setenv("AITHER_NODE_ID", "test-node")
    monkeypatch.delenv("AITHER_AIR_GAP", raising=False)
    for var in (receipts.KEY_ENV, receipts.PUBKEY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(receipts, "_home_dir", lambda: root)
    monkeypatch.setattr(receipts, "_awseal_private_key", lambda: None)
    hc.init_home(name="pip")
    return root


def _audit(tmp_path, when: datetime, detail: str) -> None:
    path = tmp_path / "data" / "compliance" / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": when.isoformat(), "action": "air_gap_violation",
             "actor": "air_gap_enforcer",
             "metadata": {"subsystem": "network_egress", "detail": detail,
                          "action_taken": "logged"}}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": event, "sig": ""}) + "\n")


def _build(home, days=30):
    cfg = hc.load_config()
    return hr.build_report(cfg, home / "actions.jsonl", home_cli.air_gap_path(),
                           days=days, now=NOW)


# ── model boundary ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider,url,boundary", [
    ("bonsai", "", "on-device"),
    ("ollama", "", "on-device"),
    ("awnode", "", "on-device"),
    ("llamacpp", "http://192.168.1.20:8080/v1", "local-network"),
    ("deepseek", "", "cloud"),
])
def test_model_boundary_from_config(provider, url, boundary):
    cfg = models.choose_model(provider, base_url=url)
    assert hr.model_boundary(cfg)["boundary"] == boundary


# ── receipts + egress composition ───────────────────────────────────────────────

def test_report_counts_receipts_and_windowed_egress_without_previews(home, tmp_path):
    receipts.append("tool", "send_sms", {"body": SECRET_ARG}, {"ok": True},
                    approval="owner:yes:ab12")
    receipts.append("tool", "send_sms", {"body": "again"}, {"ok": True}, approval="auto")
    receipts.append("tool", "web_fetch", {"url": "x"}, {"error": "tainted"},
                    approval="refused:egress-tainted")
    _audit(tmp_path, NOW - timedelta(days=2), "connect to 203.0.113.9:443")
    _audit(tmp_path, NOW - timedelta(days=90), "connect to 198.51.100.7:443")  # outside
    rep = _build(home)
    # the receipts were written "now" (wall clock); widen the window check to them
    rcpt = hr.receipts_summary(home / "actions.jsonl", NOW - timedelta(days=3650),
                               datetime.now(timezone.utc) + timedelta(minutes=1))
    assert rcpt["verify"]["code"] == 0, rcpt["verify"]
    assert rcpt["rows_total"] == 3 and rcpt["rows_in_window"] == 3
    assert rcpt["tools"] == {"send_sms": 2, "web_fetch": 1}
    assert rcpt["refused_in_window"] == 1
    assert rep["egress_events_total"] == 1
    assert "203.0.113.9" in rep["violations"][0]["detail"]
    assert SECRET_ARG not in json.dumps(rep)
    assert rep["model"]["boundary"] == "on-device"  # init default is a local preset
    assert rep["egress_policy"]["mode"] == "disabled"


def test_report_carries_a_tampered_receipts_verdict(home):
    receipts.append("tool", "a", {}, {}, approval="auto")
    receipts.append("tool", "b", {}, {}, approval="auto")
    log = home / "actions.jsonl"
    lines = log.read_bytes().split(b"\n")
    log.write_bytes(lines[1] + b"\n")  # first row deleted -> seq/chain broken
    rep = _build(home)
    assert rep["receipts"]["verify"]["code"] == 1
    assert rep["receipts"]["verify"]["verdict"] == "TAMPERED"


def test_report_with_no_receipts_is_cannot_judge_not_intact(home):
    rep = _build(home)
    assert rep["receipts"]["verify"]["code"] == 2


# ── signature ───────────────────────────────────────────────────────────────────

def test_signed_with_the_receipts_device_key_and_verifies(home):
    row = receipts.append("tool", "a", {}, {}, approval="auto")
    rep = _build(home)
    integ = rep["integrity"]
    assert integ["signed"] is True and integ["algorithm"] == "Ed25519"
    assert integ["key_id"] == row["key_id"]  # same device key as the receipts
    assert hr.verify_report(rep)[0] == 0


def test_an_edited_report_is_tampered(home):
    rep = _build(home)
    rep["model"]["boundary"] = "on-device-honest"
    code, reason = hr.verify_report(rep)
    assert code == 1, reason


def test_a_resigned_hash_with_a_bad_signature_is_tampered(home):
    rep = _build(home)
    rep["egress_events_total"] = 0
    rep = hr.sign_report(rep)  # re-hash with the real key: valid again
    assert hr.verify_report(rep)[0] == 0
    rep["integrity"]["signature"] = "00" * 64
    assert hr.verify_report(rep)[0] == 1


def test_a_foreign_key_is_cannot_judge_until_pinned(home, tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = Ed25519PrivateKey.generate()
    pem = tmp_path / "other.pem"
    pem.write_bytes(other.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    rep = hr.build_report(hc.load_config(), home / "actions.jsonl",
                          home_cli.air_gap_path(), now=NOW, key_path=str(pem))
    code, reason = hr.verify_report(rep)
    assert code == 2 and "does not trust" in reason
    # the embedded key is NOT trusted by itself; pinning it out of band is
    assert hr.verify_report(rep, pubkey=rep["integrity"]["public_key"])[0] == 0


def test_no_key_is_unsigned_and_cannot_judge(home, monkeypatch):
    monkeypatch.setattr(receipts, "_signing_key", lambda *a, **k: None)
    rep = _build(home)
    assert rep["integrity"]["signed"] is False and rep["integrity"]["signature"] == ""
    assert hr.verify_report(rep)[0] == 2


# ── CLI ─────────────────────────────────────────────────────────────────────────

def test_cli_writes_pdf_and_signed_json_then_verifies(home, tmp_path, capsys):
    pytest.importorskip("fpdf")
    receipts.append("tool", "a", {}, {}, approval="auto")
    pdf = tmp_path / "out" / "report.pdf"
    assert home_cli.main(["report", "--pdf", str(pdf)]) == 0
    assert pdf.read_bytes()[:5] == b"%PDF-"
    side = pdf.with_suffix(".json")
    assert json.loads(side.read_text(encoding="utf-8"))["kind"] == hr.REPORT_KIND
    assert home_cli.main(["report", "--verify", str(side)]) == 0
    data = json.loads(side.read_text(encoding="utf-8"))
    data["agent"] = "someone-else"
    side.write_text(json.dumps(data), encoding="utf-8")
    assert home_cli.main(["report", "--verify", str(side)]) == 1
    assert home_cli.main(["report", "--verify", str(tmp_path / "missing.json")]) == 2
    out = capsys.readouterr().out
    assert "TAMPERED" in out


def test_cli_without_fpdf_is_a_setup_error(home, tmp_path, monkeypatch, capsys):
    def _no_pdf(_rep):
        raise ImportError("fpdf2 is required")

    monkeypatch.setattr(hr, "render_pdf", _no_pdf)
    assert home_cli.main(["report", "--pdf", str(tmp_path / "r.pdf")]) == 2
    assert "awdk[pdf]" in capsys.readouterr().err


def test_pdf_renders_long_keys_and_non_latin_text(home):
    pytest.importorskip("fpdf")
    cfg = hc.load_config()
    cfg.name = "Pip – the élf \U0001f3e0"
    rep = hr.build_report(cfg, home / "actions.jsonl", home_cli.air_gap_path(), now=NOW)
    assert hr.render_pdf(rep)[:5] == b"%PDF-"
