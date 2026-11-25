"""hookd POST /pdp: the awgov PDP route, evaluated in-process.

Covers the P1 shadow contract (a verdict is computed and audited, no decision is
returned), the enforce mapping, the principal fallback and the X-Hookd gate. The
server binds an OS-assigned port; nothing here touches the daemon's :8369.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for _pkg_name in ("awgov", "awbac"):
    for _candidate in _ROOT.glob(f"*/packages/{_pkg_name}"):
        _pkg = str(_candidate)
        if _candidate.is_dir() and _pkg not in sys.path:
            sys.path.insert(0, _pkg)

from adk.harnesses import hookd  # noqa: E402


def _post(port: int, query: str, payload: dict, headers: dict):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/pdp%s" % (port, query),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _payload(command: str, session: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}, "session_id": session,
            "cwd": str(_ROOT)}


def _rows(audit: Path) -> list:
    if not audit.is_file():
        return []
    return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.fixture()
def pdp(tmp_path, monkeypatch):
    """An ephemeral hookd with every store under tmp_path; nothing touches ~/.aither."""
    monkeypatch.setenv("AWGOV_HOME", str(tmp_path / "awgov"))
    monkeypatch.setattr(hookd, "STATE_DIR", tmp_path / "hookd")  # the per-request _log
    srv, stats = hookd.ephemeral_server(1)
    if srv is None:
        pytest.fail("hookd could not bind an ephemeral port")
    yield srv.server_address[1], tmp_path / "awgov" / "audit.jsonl", stats
    srv.shutdown()
    srv.server_close()


def test_awgov_is_importable():
    import awgov

    assert callable(awgov.evaluate) and callable(awgov.hook_response)
    assert callable(awgov.load_policy)


def test_shadow_never_blocks_but_audits(pdp):
    port, audit, _ = pdp
    code, resp = _post(port, "?mode=shadow", _payload("rm -rf /c/x/.git", "s-shadow"),
                       {"X-Hookd": "1"})
    assert code == 200 and resp == {}
    rows = _rows(audit)
    assert rows and rows[-1]["decision"] == "deny" and rows[-1]["rule_id"] == "G1"


def test_enforce_denies_a_git_delete_and_does_not_deny_a_read(pdp):
    port, _, _ = pdp
    code, resp = _post(port, "?mode=enforce", _payload("rm -rf /c/x/.git", "s-enforce"),
                       {"X-Hookd": "1"})
    assert code == 200
    out = resp["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "G1" in out["permissionDecisionReason"]
    code, resp = _post(port, "?mode=enforce", _payload("git status", "s-enforce"),
                       {"X-Hookd": "1"})
    assert code == 200
    assert resp.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_bad_principal_header_falls_back_to_owner_tty(pdp):
    port, audit, _ = pdp
    code, resp = _post(port, "?mode=shadow", _payload("git status", "s-principal"),
                       {"X-Hookd": "1", "X-Aither-Principal": "Admin"})
    assert code == 200 and resp == {}
    assert _rows(audit)[-1]["principal"] == "owner-tty"


def test_resolve_principal_shapes():
    assert hookd.resolve_principal(
        {"X-Aither-Principal": "agent:atlas"}, {}) == "agent:atlas"
    assert hookd.resolve_principal(
        {"X-Aither-Principal": "tainted:router"}, {}) == "tainted:router"
    assert hookd.resolve_principal(
        {"X-Aither-Principal": "agent:Bad Name"}, {}) == "owner-tty"
    assert hookd.resolve_principal({}, {}) == "owner-tty"


def test_missing_hookd_header_is_403(pdp):
    port, _, _ = pdp
    code, _ = _post(port, "?mode=shadow", _payload("git status", "s-no-header"), {})
    assert code == 403


def test_classifier_denial_is_audited_and_never_answered(pdp):
    port, audit, _ = pdp
    payload = {"hook_event_name": "PermissionDenied", "tool_name": "Bash",
               "tool_input": {"command": "x"}, "session_id": "s-denied"}
    code, resp = _post(port, "?mode=shadow", payload, {"X-Hookd": "1"})
    assert code == 200 and resp == {}
    assert _rows(audit)[-1]["decision"] == "classifier_denied"


def test_import_failure_is_shadow_silent_and_enforce_fail_closed(pdp, monkeypatch):
    """A missing/broken awgov: shadow still answers {}, enforce denies only on fail=closed."""
    port, _, _ = pdp
    monkeypatch.setitem(hookd._PDP, "loaded", True)
    monkeypatch.setitem(hookd._PDP, "api", None)
    code, resp = _post(port, "?mode=shadow", _payload("git status", "s-import"),
                       {"X-Hookd": "1"})
    assert code == 200 and resp == {}
    code, resp = _post(port, "?mode=enforce", _payload("git status", "s-import"),
                       {"X-Hookd": "1"})
    assert code == 200 and resp == {}
    code, resp = _post(port, "?mode=enforce&fail=closed",
                       _payload("git status", "s-import"), {"X-Hookd": "1"})
    assert code == 200
    assert resp["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_health_reports_the_pdp_counters(pdp):
    port, _, _ = pdp
    _post(port, "?mode=shadow", _payload("rm -rf /c/x/.git", "s-health"),
          {"X-Hookd": "1"})
    with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=15) as resp:
        health = json.loads(resp.read())
    assert health["pdp_requests"] >= 1
    assert health["pdp_denies_shadow"] >= 1
    assert health["pdp_errors"] == 0


def test_enforce_raises_a_card_for_a_needs_owner_deny(pdp):
    """A B2 deny the owner cannot answer is worse than no PDP: /pdp must raise the
    awask card itself (the route, not only awgov.hook) and name it in the reason."""
    port, _audit, _stats = pdp
    code, resp = _post(port, "?mode=enforce", _payload("npm publish", "s-card"),
                       {"X-Hookd": "1"})
    assert code == 200
    out = resp["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "needs owner" in out["permissionDecisionReason"]
    assert "card: d-" in out["permissionDecisionReason"]
