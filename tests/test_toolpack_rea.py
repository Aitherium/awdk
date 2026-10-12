"""rea toolpack: the ledger must fail closed (no network, no node)."""
from adk.toolpacks.rea import tools


def _o(**kw):
    base = dict(id="a", original_authority="process",
                verifier_authority="process", verifier_passed=True)
    base.update(kw)
    return base


def test_closes_when_authority_sufficient():
    assert tools.rea_ledger_close([_o(verifier_authority="packaged-process")])["status"] == "closed"


def test_weaker_verifier_stays_open():
    r = tools.rea_ledger_close([_o(verifier_authority="static")])
    assert r["status"] == "open" and r["open"] == ["a"]


def test_failed_or_unknown_never_closes():
    assert tools.rea_ledger_close([_o(verifier_passed=False)])["status"] == "open"
    assert tools.rea_ledger_close([_o(original_authority="bogus")])["status"] == "open"


def test_empty_is_unknown():
    assert tools.rea_ledger_close([])["status"] == "unknown"


def test_missing_path_fails_soft():
    assert tools.rea_analyze_javascript("/no/such/path", target_class="owned")["ok"] is False
    assert tools.rea_analyze_javascript("/x", target_class="")["refused"] is True
